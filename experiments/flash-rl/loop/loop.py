"""Automated Flash GRPO loop (2026-10-15, user: "그렇게 넣어서 루프 돌려봐, 그룹은 2개").

Per update k (policy = current adapter):
  1. serve: two vLLM LoRA servers (Marlin MoE), one per KT GPU, both serving the current adapter
  2. rollout: 2 train cases in parallel (one per server), 4 episodes per case run concurrently on one restored DB
  3. judge: each group finalized as soon as its rollout ends (Claude Opus 5.5, rubric v2; user decision)
  4. train: servers stopped; general_update_runner on both GPUs (all turns of every non-flat group, revised drift gate)
  5. export: released adapter -> vLLM LoRA dir; next policy version
Every `eval_every` updates (and once for the base before the first loop update): validation6 x 2 runs.
State is file based (state.json) and every phase is resumable; never kills processes it did not start.
"""
from __future__ import annotations

import argparse, datetime, hashlib, json, os, random, re, shlex, signal, socket, subprocess, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path("/home/swlee/projects/rca-model-lab-mcp")
WORK = Path(__file__).resolve().parent
PY = str(ROOT / ".venv/bin/python")
SYS_PY = "/usr/bin/python3"   # finalize imports torch (rca_lab.train.grpo); the repo .venv has none (Codex used /usr/bin/python too)
CONFIG = json.loads((WORK / "config.json").read_text())
KEY = CONFIG["kt"]["key"]
SSH = ["ssh", "-q", "-i", KEY, "-p", str(CONFIG["kt"]["port"]), "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
       "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "ServerAliveInterval=30", CONFIG["kt"]["host"]]
KST = datetime.timezone(datetime.timedelta(hours=9))


def log(msg):
    line = f"[{datetime.datetime.now(KST).strftime('%m-%d %H:%M:%S')} KST] {msg}"
    with (WORK / "loop.log").open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def read_state():
    p = WORK / "state.json"
    return json.loads(p.read_text()) if p.exists() else None


def write_state(state):
    tmp = WORK / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(WORK / "state.json")
    try:
        record_metrics(state)
    except Exception as exc:  # dashboard data must never stop the loop
        log(f"metrics write failed: {exc}")


def remote(cmd, check=True, timeout=600):
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"remote failed ({r.returncode}): {cmd[:200]} :: {r.stderr[-800:]}")
    return r.stdout


def scp_to(local, remote_path):
    subprocess.run(["scp", "-q", "-i", KEY, "-P", str(CONFIG["kt"]["port"]), "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null", str(local), f"{CONFIG['kt']['host']}:{remote_path}"], check=True, timeout=900)


# ---------------------------------------------------------------- serving
_TUNNEL_LOCK = threading.Lock()


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def tunnel():
    with _TUNNEL_LOCK:
        _tunnel()


_TUNNEL_PROC = None


def _running(pid):
    """os.kill(pid, 0) also succeeds on a zombie: 2026-10-28 an exited, never-reaped tunnel ssh kept the pidfile 'alive' for a day
    and every caller skipped reconnecting (ctxpair: 'servers not ready in 25 min' with both servers up)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


def _tunnel():
    global _TUNNEL_PROC
    if _TUNNEL_PROC is not None:
        _TUNNEL_PROC.poll()   # reap our own exited forward so it never lingers as a zombie
    ports = [s["port"] for s in CONFIG["servers"]]
    if all(port_open(p) for p in ports):   # ours, or a forward opened outside this loop (e.g. tunnel_keeper.sh)
        return
    pidfile = WORK / "tunnel.pid"
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text())
        except ValueError:
            pid = None
        if pid and _running(pid):   # alive but not forwarding (hung after the KT proxy dropped it): replace it
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    log("tunnel down; reconnecting")
    args = ["ssh", "-q", "-i", KEY, "-p", str(CONFIG["kt"]["port"]), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes", "-N"]
    for p in ports:
        args += ["-L", f"127.0.0.1:{p}:127.0.0.1:{p}"]
    proc = subprocess.Popen(args + [CONFIG["kt"]["host"]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    _TUNNEL_PROC = proc
    pidfile.write_text(str(proc.pid))
    time.sleep(3)


def keep_tunnel():
    """KT proxy closes the forward from its side mid-eval (2026-10-23: 4 original12 reference cases lost); re-open it every 10 s."""
    def run():
        while True:
            try:
                tunnel()
            except Exception as exc:
                log(f"tunnel keeper: {str(exc)[:200]}")
            time.sleep(10)
    threading.Thread(target=run, name="tunnel-keeper", daemon=True).start()


def served_models(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=10) as r:
            return [m["id"] for m in json.load(r)["data"]]
    except Exception:
        return None


SERVED_CHECKPOINT = None


def checkpoint_path():
    """Container-local copy verified against the HF manifest sha256 when present, else the NFS original. Cold NFS reads run at
    ~83 MB/s and the container memory cgroup (480 GiB) keeps evicting the page cache, so trainer load took ~12 min (2026-10-15).
    The copy lives on the container overlay and disappears on a container restart; the marker check makes that fall back."""
    local = CONFIG.get("checkpoint_local")
    if local and remote(f"test -f {local}/.copy-verified.json && echo yes || echo no", check=False).strip() == "yes":
        return local
    return CONFIG["checkpoint"]


def start_servers(policy):
    """policy: {'version', 'lora_dir' or None}. One server per GPU; served LoRA name = CONFIG['lora_name']."""
    global SERVED_CHECKPOINT
    stop_servers()
    SERVED_CHECKPOINT = checkpoint_path()
    for s in CONFIG["servers"]:
        lora = f"--lora-modules {CONFIG['lora_name']}={policy['lora_dir']}" if policy.get("lora_dir") else ""
        cmd = CONFIG["serve_command"].format(gpu=s["gpu"], port=s["port"], lora=lora, log=f"{CONFIG['remote_work']}/server-gpu{s['gpu']}.log")
        cmd = cmd.replace(CONFIG["checkpoint"], SERVED_CHECKPOINT)   # byte-identical verified copy
        out = remote(f"mkdir -p {CONFIG['remote_work']} && cd {CONFIG['remote_work']} && "
                     f"setsid nohup bash -c {shlex.quote(cmd)} > /dev/null 2>&1 < /dev/null & echo $!")
        log(f"server gpu{s['gpu']} launch requested (wrapper pid {out.strip()})")
    tunnel()
    want = CONFIG["lora_name"] if policy.get("lora_dir") else CONFIG["base_model_name"]
    deadline = time.time() + 1500
    while time.time() < deadline:
        ready = [want in (served_models(s["port"]) or []) for s in CONFIG["servers"]]
        if all(ready):
            log(f"servers ready ({want}, policy {policy['version']})")
            return want
        time.sleep(15)
        tunnel()
    raise RuntimeError("servers not ready in 25 min; see remote server logs")


def stop_servers():
    # only servers launched by this loop: vllm processes whose command line carries our marker port + model path
    # bracket the first char so pgrep does not match the remote shell running this very command (it would kill itself
    # before reaching kill -9); vLLM ignores SIGTERM while the engine is still initializing, so escalate after 20 s
    marker = CONFIG["serve_marker"]
    pat = shlex.quote(f"[{marker[0]}]{marker[1:]}")
    # each server was started under its own setsid, so signal the whole process group (EngineCore children carry no marker)
    groups = f"$(for p in $(pgrep -f {pat}); do ps -o pgid= -p $p; done | sort -u)"
    remote(f"G={groups}; for g in $G; do [ \"$g\" -gt 1 ] && kill -TERM -- -$g 2>/dev/null; done; "
           f"for i in $(seq 20); do pgrep -f {pat} >/dev/null || break; sleep 1; done; "
           f"for g in $G; do [ \"$g\" -gt 1 ] && kill -KILL -- -$g 2>/dev/null; done; true", check=False)
    for _ in range(60):
        apps = remote("nvidia-smi --query-compute-apps=pid --format=csv,noheader", check=False).strip()
        if not apps:
            return
        time.sleep(5)
    raise RuntimeError("GPUs still busy after stopping our servers (unknown process?)")


# ---------------------------------------------------------------- rollouts
PUBLIC_FAMILIES = [
    ("openrca2", "configs/teacher/openrca2-all-split.yaml", "data/external/openrca2/captures"),
    ("tt4", "configs/teacher/rcaeval-tt4-split.yaml", "data/external/rcaeval/captures"),
    ("ob4", "configs/teacher/rcaeval-ob4-split.yaml", "data/external/rcaeval/captures"),
]


def public_contract(case):
    if "-openrca2-" in case:
        return f"configs/eval/openrca2-{case.split('-openrca2-')[1].split('-v3-')[0]}.yaml"
    return f"configs/eval/rcaeval-{case.split('-rcaeval-')[1].split('-v3-')[0]}.yaml"


def teardown_project(project):
    """Bring a replay DB set down and close its replay lease. A `prepare` restore leaves the lease `retained`; a `--reuse-db`
    run tears the DB down without touching the lease, so a later restore of the same project would refuse. Marking it
    `finished` once no resources remain lets replay_lifecycle reclaim it on the next acquire."""
    subprocess.run(["docker", "compose", "-f", str(COMPOSE), "-p", project, "down", "-v"], cwd=ROOT, capture_output=True, text=True)
    sys.path.insert(0, str(ROOT / "scripts"))
    import replay_lifecycle as rl
    lease, _ = rl._paths(project)
    record = rl._read_json(lease)
    if record and record.get("status") == "retained" and not rl._finished_record_has_resources(project):
        record.update(status="finished", updated_at=time.time())
        rl._write_atomic(lease, record)


def prefetch_restores(state, cur):
    """While update k trains (GPUs busy, this host idle), fix update k+1's cases and restore their capture DB sets with the
    case runner's `prepare` mode (DB kept). The rollout then reuses them (`--prerestored`) instead of restoring (~4 min)."""
    k_next = cur["k"] + 1
    old = state.get("prefetch") or {}
    if old.get("k") == k_next:
        return
    for e in old.get("specs", []):   # e.g. a replay update shifted the numbering: release the stale DB sets
        teardown_project(e["project"])
    if old:
        log(f"u{k_next}: discarded prefetch made for u{old.get('k')}")
    data = current_data()
    sim = {**state, "history": state["history"] + [{"cases": cur.get("cases") or [], "flat_groups": cur.get("flat_groups") or []}]}
    carry = state.get("carry") or {}
    need = slot_ids()
    for e in carry.get("groups", []):   # carried groups fill slots of their type first
        s = next((x for x in need if slot_type(x) == slot_type(e["slot"])), None)
        if s:
            need.remove(s)
    specs = pick_candidates(sim, data, candidate_plan(need), exclude={e["case"] for e in carry.get("groups", [])})
    if not specs:
        return
    pdir = WORK / "prefetch" / f"u{k_next:03d}"
    pdir.mkdir(parents=True, exist_ok=True)
    prep = json.loads(Path(data["local_preparation"]).read_text())
    entries = []
    for spec in specs:
        case = spec["case"]
        prefix = "frl" + hashlib.sha256(group_id(k_next, case, variant_tag(spec)).encode()).hexdigest()[:12]
        split, contract, root = ((spec["split"], spec["contract"], spec["cases_root"]) if spec.get("public") else
                                 (prep["split_source"], prep.get("partition_contract", "configs/eval/train-family-v2-expanded-20261012.yaml"),
                                  "/data/eval-cases"))
        cdir = pdir / "prefetch" / case   # the case runner writes to <out-root>/<run-name>/<case>
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "prefetch.json").write_text(json.dumps({"project_prefix": prefix, "case": case}))
        cmd = [PY, "scripts/mcp_case_run.py", "--runner", "prepare", "--run-name", "prefetch", "--case", case, "--partition", "train",
               "--split", split, "--cases-root", root, "--contract", contract, "--out-root", str(pdir), "--project-prefix", prefix,
               "--runs", "1", "--mcp-binary", mcp_binary()]
        with open(pdir / f"{case}.prefetch.log", "w") as sink:
            subprocess.Popen(cmd, cwd=ROOT, stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)
        entries.append({**spec, "project": f"{prefix}-{short_case(case)}", "dir": str(cdir)})
    state["prefetch"] = {"k": k_next, "data_version": data["version"], "specs": entries, "pick_logic": PICK_LOGIC}
    write_state(state)
    log(f"u{k_next}: prefetching restores for {[e['case'] for e in entries]}")


PICK_LOGIC = 4   # 3: solved-only masks, no curriculum; 4: collector-coverage + turn-budget variants (2026-10-21)


def take_prefetch(state, cur, k, need=None):
    """Prefetched specs for this update (consumed once); stale or unusable prefetches, and those for slots already filled
    by carried groups, are torn down."""
    pf = state.pop("prefetch", None)
    if not pf:
        return None, None
    # a prefetch drawn by older picking rules (no evidence masks / public curriculum) is redrawn (2026-10-19)
    usable = pf["k"] == k and pf["data_version"] == cur["data"]["version"] and pf.get("pick_logic") == PICK_LOGIC
    if need is not None:
        types = {slot_type(x) for x in need}
        for e in [e for e in pf["specs"] if slot_of(e) not in types]:
            teardown_project(e["project"])
        pf["specs"] = [e for e in pf["specs"] if slot_of(e) in types]
    ready = {}
    for e in pf["specs"]:
        if usable:
            plog = Path(e["dir"]).parent.parent / f"{e['case']}.prefetch.log"
            for _ in range(60):   # a prepare still running (rare: training takes far longer than a restore)
                text = plog.read_text(errors="replace") if plog.exists() else ""
                if (Path(e["dir"]) / "connections.json").exists() or "Traceback" in text or "FAILED" in text:
                    break
                time.sleep(10)
        if usable and (Path(e["dir"]) / "connections.json").exists():
            ready[e["case"]] = e["dir"]
        else:
            teardown_project(e["project"])
    if not usable:
        log(f"u{k}: discarded stale prefetch for u{pf['k']}")
        return None, None
    specs = [{key: v for key, v in e.items() if key not in ("project", "dir")} for e in pf["specs"]]
    return specs, ready


def legacy_data():
    """Data version used before per-update pinning existed (Polestar split v2: train43 / validation6 / sealed12)."""
    return {"version": "v2", "local_preparation": str(ROOT / "outputs/flash-next-rl-pilot-20261014/preparation.json"),
            "remote_preparation": CONFIG["preparation"], "remote_split_source": CONFIG["split_source"], "runtime": CONFIG["runtime"]}


def current_data():
    """Data version for a new update; switched by editing config 'data' (applies from the next rollout, never mid-update)."""
    return dict(CONFIG.get("data") or legacy_data())


def group_is_flat(group_path):
    """Dynamic sampling (2026-10-16, user): every episode shares the joint outcome (all solved or all failed) and the rewards
    differ by less than min_reward_spread, i.e. only by the length/evidence terms. Such a group carries almost no
    advantage signal (u8: spreads 0.037 and 0.076 against 0.66 for u2's f25-h), so it is replaced instead of trained."""
    key = (str(group_path), os.stat(group_path).st_mtime_ns)   # group files carry token ids (tens of MB): read once
    if key not in _FLAT:
        eps = json.loads(Path(group_path).read_text())["episodes"]
        _FLAT[key] = ({e["joint_success"] for e in eps}, [e["reward"] for e in eps])
    outcomes, rewards = _FLAT[key]
    return len(outcomes) == 1 and max(rewards) - min(rewards) < CONFIG["dynamic_sampling"]["min_reward_spread"]


_FLAT = {}


def flat_outcomes(state):
    """Variants (rollout dir names: case, or case__m-<mask>) whose group was flat within the cooldown window, with the
    shared joint outcome (True = all solved: too easy for the policy right now; False = all failed: too hard)."""
    if not CONFIG.get("dynamic_sampling"):
        return {}
    out = {}
    for u in state["history"][-CONFIG["dynamic_sampling"]["flat_cooldown_updates"]:]:
        for g in (u.get("groups") or []) + (u.get("flat_groups") or []):
            if Path(g).exists() and group_is_flat(g):
                out[Path(g).parent.name] = next(iter(_FLAT[(str(g), os.stat(g).st_mtime_ns)][0]))
    return out


_MASKS = {}


def mask_table():
    """Evidence-masking augmentation (2026-10-19, user: train the with/without versions of a case so the policy learns
    several reasoning paths; capture data untouched): case -> evidence sources that may be hidden (data-v4 table)."""
    em = CONFIG.get("evidence_mask") or {}
    if not em.get("table"):
        return {}
    path = WORK / em["table"]
    key = (str(path), os.stat(path).st_mtime_ns)
    if key not in _MASKS:
        _MASKS[key] = json.loads(path.read_text())["allowed"]
    # optional allowlist of mask kinds (config evidence_mask.allowed_masks)
    keep = em.get("allowed_masks")
    return {c: [m for m in ms if keep is None or m in keep] for c, ms in _MASKS[key].items()}


def extra_solved_list(cur):
    """Extra all-solved groups of this update (older state stored a single dict)."""
    ex = cur.get("extra_solved")
    return [ex] if isinstance(ex, dict) else list(ex or [])


_VARIANTS = {}


def variant_options(case):
    """Training variants of a solved case (2026-10-21, user): collector-coverage masks and a turn budget, from the
    per-case table (build_coverage_table.py) — [("mask", collector), ("budget", turns)]."""
    vc = CONFIG.get("variants") or {}
    if not vc.get("table"):
        return [("mask", m) for m in mask_table().get(case, [])]
    path = WORK / vc["table"]
    key = (str(path), os.stat(path).st_mtime_ns)
    if key not in _VARIANTS:
        _VARIANTS[key] = json.loads(path.read_text())["cases"]
    row = _VARIANTS[key].get(case) or {}
    kinds = vc.get("kinds", ["mask", "budget"])
    out = [("mask", m) for m in row.get("masks", [])] if "mask" in kinds else []
    if "budget" in kinds and row.get("turn_budget"):
        out.append(("budget", int(row["turn_budget"])))
    return out


def variant_tag(spec):
    """Suffix of a variant's rollout dir / group id: __m-<collector> (mask) or __b<turns> (turn budget)."""
    if spec.get("mask"):
        return f"__m-{spec['mask']}"
    if spec.get("turn_budget"):
        return f"__b{spec['turn_budget']}"
    return ""


def variant(spec):
    """Rollout dir / group name of a candidate: the case, plus its variant tag."""
    return spec["case"] + variant_tag(spec)


_SPLITS = {}


def load_split(path):
    """Public split YAMLs are large (seconds to parse); oversampling picks several cases per update, so cache by mtime."""
    import yaml
    key = (str(path), os.stat(path).st_mtime_ns)
    if key not in _SPLITS:
        _SPLITS[key] = yaml.safe_load(Path(path).read_text())
    return _SPLITS[key]


_OUTCOME_LINE = re.compile(r"\] u(\d+): (\S+) rewards \[[^\]]*\] joint \[([^\]]*)\]")


def observed_outcomes():
    """(update index, variant name, [joint outcomes]) of every judged rollout group, from the loop log's judge lines."""
    out = []
    try:
        text = (WORK / "loop.log").read_text(errors="replace")
    except OSError:
        return out
    for m in _OUTCOME_LINE.finditer(text):
        out.append((int(m.group(1)), m.group(2), [x.strip() == "True" for x in m.group(3).split(",") if x.strip()]))
    return out


def informative_weights(state, pool):
    """2026-10-28 uncertainty-aware case sampling (Codex topic 7): weight = expected chance that a fresh group of
    group_size episodes has both joint outcomes, under a Beta(1,1) prior updated with every judged group of the case
    (kept, flat or surplus), each observation discounted by decay^(updates since). Unseen cases keep the prior (0.6 for
    4 episodes); nothing is hard-skipped here, the exploration floor in pick_cases draws uniformly."""
    cfg = CONFIG.get("case_sampling") or {}
    decay, size, k_now = cfg.get("decay", 0.8), CONFIG["group_size"], state["k"]
    post = {c: [1.0, 1.0] for c in pool}
    for k, name, joints in observed_outcomes():
        if name in post and k <= k_now:
            w = decay ** (k_now - k)
            post[name][0] += w * sum(joints); post[name][1] += w * (len(joints) - sum(joints))
    def informative(a, b):
        ep = eq = 1.0   # E[p^G], E[(1-p)^G] under Beta(a, b)
        for i in range(size):
            ep *= (a + i) / (a + b + i); eq *= (b + i) / (a + b + i)
        return 1 - ep - eq
    return [max(informative(*post[c]), 1e-3) for c in pool]


def draw_case(state, rng, pool, kind):
    """One case from `pool`: uniform, or (case_sampling.mode == 'uncertainty') informative-weighted with an exploration floor."""
    cfg = CONFIG.get("case_sampling") or {}
    if cfg.get("mode") != "uncertainty" or rng.random() < cfg.get("explore", 0.2):
        return rng.choice(pool)
    return rng.choices(pool, weights=informative_weights(state, pool))[0]


def pick_cases(state, n, data=None, exclude=(), only=None, draw=0):
    """Per update: one Polestar train case + one public train case (user 2026-10-15), never recently used ones nor recently
    flat ones. Public cases come only from the train partition; the monitoring held-out set is excluded explicitly.
    `only` ('polestar' / 'public') draws a single replacement for a dropped flat group; `draw` varies the seed."""
    import yaml
    prep = json.loads(Path((data or legacy_data())["local_preparation"]).read_text())
    hist = {c for u in state["history"][-CONFIG["avoid_recent_updates"]:] for c in (u.get("cases") or [])} | set(exclude)
    flat = flat_outcomes(state)
    recent = hist | set(flat)
    rng = random.Random(1000 + len(state["history"]) + 7919 * state.get("resample", 0) + 104729 * draw)
    specs = []
    if only in (None, "polestar"):
        keep_out = set(CONFIG.get("train_exclude", {}).get("cases", []))   # evaluation sets that must stay unseen by RL
        pool = [c for c in sorted(prep["train_cases"]) if c not in keep_out]
        vc = CONFIG.get("variants") or CONFIG.get("evidence_mask") or {}
        rate = vc.get("rate", 0)
        spec_of = lambda c, v: {"case": c, "public": False, **({"mask": v[1]} if v[0] == "mask" else {"turn_budget": v[1]})}
        open_masks = {c: [v for v in variant_options(c) if variant(spec_of(c, v)) not in flat] for c in pool} if rate else {}
        # `rate` of the Polestar candidates are masked variants, drawn among cases with an open mask; an all-solved case
        # (flat True) stays usable there, its masked variant being the harder version of the same case
        solved_only = vc.get("mode") == "solved_only"
        # 2026-10-20 (user): masked variants only for cases the policy currently solves (flat all-solved); random masking
        # of unsolved cases made the training distribution too hard (2/12 successes per update, u29-u30)
        masked = [c for c in pool if c not in hist and open_masks.get(c) and (flat.get(c) is True if solved_only else flat.get(c, True))]
        if masked and rng.random() < rate:
            case = rng.choice(masked)
            specs.append(spec_of(case, rng.choice(open_masks[case])))
        else:
            specs.append({"case": draw_case(state, rng, [c for c in pool if c not in recent] or pool, "polestar"), "public": False})
    if only in (None, "public"):
        # eval cases never enter RL: the public monitor and (2026-10-28) the frozen development panel (any eval track)
        monitor = {it["case"] for spec in CONFIG["eval"]["tracks"].values() for it in spec["items"]}
        fam, split_path, root = PUBLIC_FAMILIES[(len(state["history"]) + state.get("resample", 0) + draw) % len(PUBLIC_FAMILIES)]
        split = load_split(ROOT / split_path)
        held = {c for k, v in split.items() if k != "train" and isinstance(v, list) for c in v} | monitor
        pool = [c for c in sorted(split["train"]) if c not in held and c not in recent]
        weights = public_fault_weights(state) if CONFIG.get("public_curriculum") else None
        case = rng.choices(pool, weights=[weights.get(fault_type(ROOT / root, c), weights["__default__"]) for c in pool])[0] if weights else draw_case(state, rng, pool, "public")
        specs.append({"case": case, "public": True, "split": str(ROOT / split_path), "cases_root": str(ROOT / root), "contract": public_contract(case)})
    return specs[:n]


_FAULT, _MECH = {}, {}


def fault_type(cases_root, case):
    """Injected fault type of a public train capture, from its own metadata (RCAEval source.fault; OpenRCA title
    '<service> <FaultType>[ + <service> <FaultType>]')."""
    key = (str(cases_root), case)
    if key not in _FAULT:
        meta = json.loads((Path(cases_root) / case / "meta.json").read_text())
        ft = (meta.get("source") or {}).get("fault")
        if not ft:
            title = (meta.get("scenario_metadata") or {}).get("title", "")
            ft = "+".join(sorted({part.split()[-1] for part in title.split(" + ") if part.split()})) or "unknown"
        _FAULT[key] = ft
    return _FAULT[key]


def rollout_mechanism_outcomes(group_path):
    """[(root_correct, mechanism_score)] of one finished rollout group, from the small per-episode files."""
    out_dir = Path(group_path).parent
    if group_path not in _MECH:
        rows = []
        for sp in out_dir.glob("episodes/*/*/run*/score.json"):
            score = json.loads(sp.read_text())
            rows.append((score.get("root_f1") == 1, mechanism_score(sp.parent)))
        _MECH[group_path] = rows
    return _MECH[group_path]


def public_fault_weights(state):
    """Public curriculum (2026-10-19, user): weight public train cases by how often the policy names the right root but
    misses its mechanism, per injected fault type: w = floor + (misses + 1) / (root hits + 2), over all rollout history."""
    cfg = CONFIG.get("public_curriculum") or {}
    hits, misses = {}, {}
    for u in state["history"]:
        for g in (u.get("groups") or []) + (u.get("flat_groups") or []):
            name = Path(g).parent.name.split("__m-")[0]
            if not any(f"-{fam}-" in name for fam, _, _ in PUBLIC_FAMILIES) or not Path(g).exists():
                continue
            root = next(r for fam, _, r in PUBLIC_FAMILIES if f"-{fam}-" in name)
            try:
                ft = fault_type(ROOT / root, name)
            except FileNotFoundError:
                continue
            for root_ok, mech in rollout_mechanism_outcomes(g):
                if root_ok:
                    hits[ft] = hits.get(ft, 0) + 1
                    misses[ft] = misses.get(ft, 0) + (mech != 1)
    floor = cfg.get("floor", 0.5)
    return {ft: floor + (misses.get(ft, 0) + 1) / (n + 2) for ft, n in hits.items()} | {"__default__": floor + 0.5}


SLOT_TYPES = ("polestar", "public")


def slot_ids():
    """Slots of one update: CONFIG['slots'] lists their types, a type repeated for several groups (2026-10-18, user: one
    Polestar + two public). A repeated type gets ids 'public#2', ...; the first keeps the bare name (older state uses it)."""
    from collections import Counter
    seen, ids = Counter(), []
    for t in CONFIG.get("slots") or SLOT_TYPES[:CONFIG["groups_per_update"]]:
        seen[t] += 1
        ids.append(t if seen[t] == 1 else f"{t}#{seen[t]}")
    return ids


def slot_type(slot):
    return slot.split("#")[0]


def candidate_plan(need):
    """Candidates to roll per type for the open slots: candidates_by_type (or candidates_per_slot per slot), scaled to the
    share of that type's slots still open, never fewer than the open slots."""
    import math
    from collections import Counter
    ds = CONFIG.get("dynamic_sampling") or {}
    total, open_ = Counter(map(slot_type, slot_ids())), Counter(map(slot_type, need))
    plan = {}
    for t, n in open_.items():
        full = (ds.get("candidates_by_type") or {}).get(t, ds.get("candidates_per_slot", 1) * total[t])
        plan[t] = max(n, math.ceil(full * n / total[t]))
    return plan


def slot_of(spec):
    return "public" if spec.get("public") else "polestar"


def pick_candidates(state, data, plan, exclude=(), draw=0):
    """Oversampling (2026-10-16, user): roll several cases per open slot at once and train on informative ones, instead of
    discovering a flat group only after a full rollout. `plan` = {type: count}; order [polestar0, public0, polestar1, ...]."""
    used, out = set(exclude), []
    for j in range(max(plan.values(), default=0)):
        for t in SLOT_TYPES:
            if plan.get(t, 0) > j:
                spec = pick_cases(state, 1, data, exclude=used, only=t, draw=draw * 101 + j)[0]
                used.add(spec["case"]); out.append(spec)
    return out


def take_carry(state, cur, k):
    """Informative groups left over from the previous update (oversampling) fill slots without a new rollout; the trainer
    admits groups sampled by the start policy or its parent (runtime fast9+)."""
    c = state.pop("carry", None)
    if not c:
        return
    pol = state["policy"]
    if c["behavior"] not in (pol["version"], pol.get("parent")) or c["data_version"] != cur["data"]["version"]:
        log(f"u{k}: carried groups {[e['case'] for e in c['groups']]} dropped (sampled by {c['behavior']}, policy {pol['version']})")
        return
    kept = cur.setdefault("kept", {})
    for e in c["groups"]:
        free = [s for s in slot_ids() if slot_type(s) == slot_type(e["slot"]) and s not in kept]
        if free:
            kept[free[0]] = {**e, "behavior": c["behavior"], "step_age": step_age(state, c["behavior"])}
            cur.setdefault("funnel", []).append({"case": e["case"], "slot": slot_type(e["slot"]), "outcome": "carried_in",
                                                 "step_age": kept[free[0]]["step_age"]})
            log(f"u{k}: slot {free[0]} uses carried group {e['case']} (sampled by {c['behavior']}, "
                f"stale steps {0 if c['behavior'] == pol['version'] else 1})")


COMPOSE = ROOT / "tools/rca-mcp/replay/eval-dbs.compose.yml"


def short_case(case):
    return case.split("-v3-")[0].replace("case-", "")


def group_id(k, case, tag=""):
    """Rollout group id; also seeds the replay DB project prefix (run.py: 'frl' + sha256(group id)[:12]). Includes the
    capture hash: with oversampling two cases of one family (e.g. two ob4 captures) run in the same update, and a
    family-only id gave both the same DB project (2026-10-16 u10: 'unreclaimed replay lease exists')."""
    return f"loop-u{k:03d}-{short_case(case)}-{case.rsplit('-', 1)[-1]}" + tag.replace("__m-", "-m").replace("__b", "-b")


def run_with_prejudge(udir, case, out, cmd):
    """Run one group's rollout; judge each episode as soon as its score.json lands (score.json is written last), so the
    judge caches finalize needs are mostly filled by the time the slowest episode finishes. Returns the rollout exit code."""
    judged, futs = set(), []
    log_path = udir / f"rollout-{case}.log"
    def prejudge(run_dir):
        r = subprocess.run([SYS_PY, str(ROOT / "outputs/flash-next-rl-pilot-20261014/rollout/prejudge.py"), str(run_dir),
                            "--judge-provider", CONFIG["judge"]["provider"], "--judge-model", CONFIG["judge"]["model"]]
                           + (["--depth"] if (CONFIG.get("reward") or {}).get("depth_weight") is not None else [])
                           + ["--judge-rubric", judge_rubric(), "--depth-rubric", depth_rubric()],
                           cwd=ROOT, capture_output=True, text=True)
        with open(udir / f"prejudge-{case}.log", "a") as fh:
            fh.write(f"--- {run_dir.name} exit {r.returncode}\n{r.stdout[-2000:]}{r.stderr[-2000:]}\n")
    def submit_new(pool):
        for sp in sorted((out / "episodes").glob("**/run*/score.json")):
            if sp.parent not in judged:
                judged.add(sp.parent); futs.append(pool.submit(prejudge, sp.parent))
    with open(log_path, "w") as sink, ThreadPoolExecutor(max_workers=CONFIG["group_size"]) as pool:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=sink, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            time.sleep(10)
            submit_new(pool)
        submit_new(pool)
        for f in futs:
            f.result()
    return proc.returncode


def rollout_and_judge(k, spec, server, model, policy_version, udir, data=None, prerestored=None):
    case, name = spec["case"], variant(spec)
    out = udir / "rollouts" / name
    gid = group_id(k, case, variant_tag(spec))
    manifest = udir / f"serving-{name}.json"
    manifest.write_text(json.dumps({"serve_command": CONFIG["serve_command"], "gpu": server["gpu"], "lora_name": model,
                                    "policy_version": policy_version, "moe_backend": "marlin",
                                    **({"model_path": SERVED_CHECKPOINT} if SERVED_CHECKPOINT else {})}, sort_keys=True))
    if not (out / "group.evidence-v2.json").exists():
        if not (out / "manifest.json").exists() or json.loads((out / "manifest.json").read_text()).get("status") != \
                "captured_requires_complete_group_judging_and_policy_identity_verification":
            if out.exists():
                failed = udir / "failed" / f"{name}-{int(time.time())}"; failed.parent.mkdir(parents=True, exist_ok=True); out.rename(failed)
                # a crashed attempt may have left its DB set up; the retry's restore would refuse the existing project
                teardown_project("frl" + hashlib.sha256(gid.encode()).hexdigest()[:12] + "-" + short_case(case))
            log(f"u{k}: rollout {name} on gpu{server['gpu']} start")
            pre = (prerestored or {}).get(case)
            r = run_with_prejudge(udir, name, out, [PY, str(ROOT / "outputs/flash-next-rl-pilot-20261014/rollout/run.py"), "--case", case,
                                "--policy-version", policy_version, "--endpoint", f"http://127.0.0.1:{server['port']}/v1", "--model", model,
                                "--group-id", gid, "--group-size", str(CONFIG["group_size"]), "--parallel-runs", str(CONFIG["group_size"]),
                                "--seed", str(42000 + 97 * k), "--out", str(out), "--serving-manifest", str(manifest),
                                "--preparation", (data or legacy_data())["local_preparation"]]
                               + (["--public-split", spec["split"], "--public-contract", spec["contract"], "--public-cases-root", spec["cases_root"]]
                                  if spec.get("public") else [])
                               + (["--prerestored", pre] if pre else [])
                               + (["--evidence-mask", spec["mask"]] if spec.get("mask") else []) + rev_args()
                               + (["--max-turns", str(spec["turn_budget"])] if spec.get("turn_budget") else [])
                               + ["--mcp-binary", mcp_binary()])
            if pre:   # the case runner tore the prefetched DB set down; close its retained lease
                teardown_project("frl" + hashlib.sha256(gid.encode()).hexdigest()[:12] + "-" + short_case(case))
            if r != 0:
                raise RuntimeError(f"rollout {name} failed (exit {r}); see {udir}/rollout-{name}.log")
        log(f"u{k}: judge {name}")
        shw = (CONFIG.get("reward") or {}).get("success_hallucination_weight")   # rca-evidence-v3 (2026-10-19, user)
        r = subprocess.run([SYS_PY, str(ROOT / "outputs/flash-next-rl-pilot-20261014/rollout/finalize.py"), str(out),
                            "--judge-provider", CONFIG["judge"]["provider"], "--judge-model", CONFIG["judge"]["model"]]
                           + (["--success-hallucination-weight", str(shw)] if shw is not None else [])
                           # rca-evidence-v4 (2026-10-19, user: "기전 비율을 좀더 높여"): mechanism-led partial credit
                           + sum((["--partial-root-weight", str(rw["partial_root_weight"]), "--partial-mechanism-weight",
                                   str(rw["partial_mechanism_weight"])] for rw in [CONFIG.get("reward") or {}]
                                  if rw.get("partial_mechanism_weight") is not None), [])
                           # rca-evidence-v5 (2026-10-19, user: explain the target and its cause precisely): causal depth bonus
                           + (["--depth-weight", str(CONFIG["reward"]["depth_weight"])]
                              if (CONFIG.get("reward") or {}).get("depth_weight") is not None else [])
                           # rca-evidence-v6 (2026-10-20, user): fabricated joint successes capped
                           + (["--fabricated-success-cap", str(CONFIG["reward"]["fabricated_success_cap"])]
                              if (CONFIG.get("reward") or {}).get("fabricated_success_cap") is not None else [])
                           + ["--judge-rubric", judge_rubric(), "--depth-rubric", depth_rubric()]
                           # rca-evidence-v7 (2026-10-20, user): per-major-claim penalty on joint successes
                           + (["--major-claim-penalty", str(CONFIG["reward"]["major_claim_penalty"])]
                              if (CONFIG.get("reward") or {}).get("major_claim_penalty") is not None else []),
                           cwd=ROOT, capture_output=True, text=True)
        (udir / f"finalize-{name}.log").write_text(r.stdout[-20000:] + "\n--- stderr ---\n" + r.stderr[-20000:])
        if r.returncode != 0:
            raise RuntimeError(f"finalize {name} failed: {r.stderr[-600:]}")
    g = json.loads((out / "group.evidence-v2.json").read_text())
    rewards = [e["reward"] for e in g["episodes"]]
    failures = sum(1 for e in g["episodes"] if e.get("model_failure"))
    log(f"u{k}: {name} rewards {[round(x, 3) for x in rewards]} joint {[e['joint_success'] for e in g['episodes']]}")
    return out / "group.evidence-v2.json", rewards, failures


# ---------------------------------------------------------------- training
def train(k, state, groups, udir, data=None):
    data = data or legacy_data()
    rdir = f"{CONFIG['remote_work']}/u{k:03d}"
    out = f"{CONFIG['adapter_root']}/u{k:03d}"
    remote(f"mkdir -p {rdir}/groups")
    names = []
    for g in groups:
        name = f"{rdir}/groups/{Path(g).parent.name}.json"; scp_to(g, name); names.append(name)
    rt = data["runtime"]
    start = state["policy"].get("adapter") or "none"
    ckpt = checkpoint_path()
    env = ("CUDA_VISIBLE_DEVICES=0,1 HF_HUB_OFFLINE=1 OMP_NUM_THREADS=8 PYTHONPATH=/home/work/models/flash-rl-kernels-20261014/python "
           "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True")
    cmd = (f"cd {rt} && {env} {CONFIG['train_python']} -u general_update_runner.py --groups {' '.join(names)} "
           f"--policy-version {state['policy']['version']} --start-adapter {start} --output {out} --checkpoint {ckpt} "
           f"--repo-src {rt}/src --preparation {data['remote_preparation']} --split-source {data['remote_split_source']} "
           f"--learning-rate {state.get('learning_rate', CONFIG['learning_rate'])} --checkpoint-every 10"
           + (" --tree" if data.get("tree") else "")   # exact prefix-sharing schedule (runtime fast6+)
           + (" --per-group-steps" if data.get("per_group_steps") else "")   # 2026-10-24 (user): one step per group (fast17+)
           + (f" --groups-per-step {int(data['groups_per_step'])}" if data.get("groups_per_step") else "")   # 2026-10-26 (user): N groups per step (fast18+)
           + (f" --speed {data['speed']}" if data.get("speed") else "")   # 2026-10-28: speedpatch flags (fast19+), sweep adopted 'stack'
           + (f" --optimizer {data['optimizer']}" if data.get("optimizer") else ""))   # 2026-10-28: adamw|muon|hybrid (fast20+)
    if not remote(f"test -f {out}/progress.json && echo yes || echo no", check=False).strip() == "yes":
        remote(f"mkdir -p {out} && setsid nohup bash -c {shlex.quote(cmd + f' > {out}/training.log 2>&1')} > /dev/null 2>&1 < /dev/null &")
        log(f"u{k}: training launched ({len(names)} groups, checkpoint {ckpt})")
    last = None
    gone = 0
    marker = shlex.quote(f"[g]eneral_update_runner.py --groups {rdir}/groups")
    while True:
        time.sleep(60)
        raw = remote(f"cat {out}/progress.json 2>/dev/null || echo '{{}}'; pgrep -f {marker} >/dev/null && echo __ALIVE__ || echo __GONE__",
                     check=False)
        alive = "__ALIVE__" in raw
        raw = raw.replace("__ALIVE__", "").replace("__GONE__", "")
        try:
            p = json.loads(raw)
        except json.JSONDecodeError:
            p = {}
        st = p.get("status")
        # 2026-10-15: a runner that exits without a terminal status (crash, kill, or a code path that skips the
        # release) used to leave the loop polling forever; after 3 minutes without a runner it is a failed update
        gone = 0 if alive or st in ("passed", "blocked_drift_budget", "failed") else gone + 1
        if gone >= 3:
            p = dict(p, status="failed", error=f"runner exited without a terminal status (last status {st!r}, "
                                               f"completed {p.get('completed_turns')}/{p.get('turns')})")
            log(f"u{k}: {p['error']}")
            (udir / "train-progress.json").write_text(json.dumps(p, indent=2))
            return p, out
        if (st, p.get("completed_turns")) != last and (p.get("completed_turns") or 0) % 20 == 0:
            log(f"u{k}: training {st} {p.get('completed_turns')}/{p.get('turns')}")
        last = (st, p.get("completed_turns"))
        if st in ("passed", "blocked_drift_budget", "failed"):
            (udir / "train-progress.json").write_text(json.dumps(p, indent=2))
            return p, out


def export(k, adapter_dir):
    lora_dir = f"{CONFIG['lora_root']}/u{k:03d}"
    remote(f"cd {Path(CONFIG['exporter']).parent} && {CONFIG['train_python']} {CONFIG['exporter']} {adapter_dir}/adapter.pt {lora_dir}", timeout=1800)
    return lora_dir


# ---------------------------------------------------------------- evaluation
def full_public_track():
    """Every public held-out case (OpenRCA2 public_compare + RCAEval held-out services), 1 run each."""
    import yaml
    items = []
    for src in CONFIG["eval"]["full_public"]:
        split = yaml.safe_load((ROOT / src["split"]).read_text())
        items += [{"case": c, "cases_root": src["cases_root"], "contract": public_contract(c), "split": src["split"]}
                  for c in sorted(split[src["partition"]])]
    return {"runs": 1, "items": items}


def prejudge(edir, case):
    """Judge one finished eval case while the rest of the track still runs (2026-10-23, user: "미리미리 채점하면안되나").
    Every verdict is cached per run (mechanism verdicts under a per-run lock), so the track's final pass only reads caches."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from mcp_judge_runs import judge_run
        import importlib.util
        spec = importlib.util.spec_from_file_location("eval_evidence", WORK / "eval_evidence.py")
        ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
        for sp in sorted((edir / "shards").glob(f"*/{case}/run*/score.json")):
            judge_run(sp.parent, CONFIG["judge"]["model"], judge_rubric(), False, CONFIG["judge"]["provider"])
            ev.judge(sp.parent)   # evidence, causal depth (joint runs) and decisive evidence
    except Exception as exc:   # the final pass retries anything left unjudged
        log(f"prejudge {case}: {str(exc)[:200]}")


def scored_runs(edir, case):
    return len(list((edir / "shards").glob(f"*/{case}/run*/score.json")))


def set_aside_unscored(edir, case):
    """Keep a failed episode's files (responses.jsonl is appended to) out of the retry's run dir."""
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for rd in (edir / "shards").glob(f"*/{case}/run*"):
        if rd.is_dir() and rd.name[3:].isdigit() and not (rd / "score.json").exists():
            rd.rename(rd.with_name(f"{rd.name}.failed-{stamp}"))


EVAL_RETRIES = 2


def evaluate(tag, model, state, tracks=None, policy_version=None, merge=False, full_items=None):
    """Eval tracks on the served policy (default: validation6 x 2 runs + public held-out 12 x 1 run); Claude judge rubric v2."""
    results = {"tag": tag, "model": model, "policy_version": policy_version or state["policy"]["version"], "tracks": {},
               "prompt_revision": prompt_rev(), "epoch": epoch_key()}
    for track, spec in (tracks or CONFIG["eval"]["tracks"]).items():
        edir = WORK / "eval" / tag / track
        edir.mkdir(parents=True, exist_ok=True)
        def run(pair, spec=spec, edir=edir, track=track):
            idx, item = pair
            s = CONFIG["servers"][idx % len(CONFIG["servers"])]
            cmd = [PY, "scripts/mcp_case_run.py", "--runner", "student", "--student-tools", "harness", "--run-name", f"{tag}-{track}",
                   "--out-root", str(edir / "shards"), "--case", item["case"], "--cases-root", item["cases_root"], "--split", item["split"],
                   "--contract", item["contract"], "--project-prefix", f"fle{track[0]}{idx:02d}{tag[-4:]}", "--runs", str(spec["runs"]),
                   "--parallel-runs", str(spec["runs"]), "--max-turns", "60", "--endpoint", f"http://127.0.0.1:{s['port']}/v1",
                   "--model", model, "--max-prompt-tokens", "0", "--max-output-tokens", "32768", "--tool-result-max-chars", "6000",
                   "--observation-policy", "legacy", "--no-guided", "--mcp-binary", mcp_binary()] \
                + (["--allow-sealed"] if spec.get("allow_sealed") else []) + rev_args()
            # 2026-10-23 (user): a case whose runs did not all score is retried (tunnel re-checked) and, if still short, failed
            for attempt in range(1 + EVAL_RETRIES):
                if attempt:
                    log(f"eval {tag} [{track}]: {item['case']} scored {scored_runs(edir, item['case'])}/{spec['runs']}; retry {attempt}/{EVAL_RETRIES}")
                    tunnel(); time.sleep(30)
                set_aside_unscored(edir, item["case"])
                r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
                with open(edir / f"{item['case']}.log", "a") as sink:
                    sink.write(f"=== attempt {attempt + 1}\n" + r.stdout[-10000:] + "\n--- stderr ---\n" + r.stderr[-10000:] + "\n")
                if scored_runs(edir, item["case"]) >= spec["runs"]:
                    break
            jpool.submit(prejudge, edir, item["case"])
            return item["case"], (r.returncode or 1) if scored_runs(edir, item["case"]) < spec["runs"] else r.returncode
        # fill the servers' episode slots (2 x max-num-seqs 4): 8 single-run cases or 4 two-run cases at a time
        with ThreadPoolExecutor(max_workers=4) as jpool, \
                ThreadPoolExecutor(max_workers=max(1, CONFIG["eval"].get("episode_slots", 8) // spec["runs"])) as pool:
            codes = list(pool.map(run, enumerate(spec["items"])))
        roots = sorted({p.parent.parent.parent for p in (edir / "shards").glob("**/run*/score.json")})
        if roots:
            subprocess.run([PY, "scripts/mcp_judge_runs.py", *map(str, roots), "--provider", CONFIG["judge"]["provider"],
                            "--model", CONFIG["judge"]["model"], "--rubric", judge_rubric()], cwd=ROOT, capture_output=True, text=True)
        hit = strict = total = 0
        for sp in (edir / "shards").glob("**/run*/score.json"):
            s = json.loads(sp.read_text()); total += 1
            h = s.get("root_f1") == 1; hit += h
            strict += bool(h and mechanism_score(sp.parent) == 1)
        failed = [c for c, rc in codes if rc != 0]
        if merge:   # a repair re-ran only some items: failures are the cases of this track still short of runs
            failed = sorted({i["case"] for i in (full_items or {}).get(track, spec["items"])
                             if scored_runs(edir, i["case"]) < spec["runs"]})
        results["tracks"][track] = {"total": total, "root_hit": hit, "root_plus_mechanism": strict, "failed_cases": failed}
        log(f"eval {tag} [{track}]: root {hit}/{total}, root+mechanism {strict}/{total}" + (f", FAILED cases {failed}" if failed else ""))
        try:   # 2026-10-19 (user): evidence quality next to correctness — measurement only, never a gate
            import importlib.util
            spec = importlib.util.spec_from_file_location("eval_evidence", WORK / "eval_evidence.py")
            ev = importlib.util.module_from_spec(spec); spec.loader.exec_module(ev)
            e = ev.summarise(edir)
            results["tracks"][track]["evidence"] = {k: e[k] for k in ("judged", "judge_failed", "clean_joint", "joint_with_major", "path_mean", "halluc_mean", "major_rate", "numeric_unverified_rate")}
            log(f"eval {tag} [{track}]: clean root+mechanism (no major fabricated claim) {e['clean_joint']}/{total}, major-claim rate {e['major_rate']}, right target + extra cause {e.get('right_target_extra_cause')}, turns {e.get('mean_turns')}, output tokens {e.get('mean_output_tokens')}, causal depth {e.get('joint_mean_depth')} (deep {e.get('deep_joint')}), decisive evidence cited {e.get('decisive_recall')} / reached {e.get('decisive_observed')}")
        except Exception as exc:
            log(f"eval {tag} [{track}]: evidence summary skipped ({str(exc)[:200]})")
    rpath = WORK / "eval" / tag / "result.json"
    if merge and rpath.exists():
        old = json.loads(rpath.read_text()); old["tracks"].update(results["tracks"]); results = {**old, "tracks": old["tracks"]}
    rpath.write_text(json.dumps(results, indent=2))
    return results



def rollback(state, reason):
    """Revert to the last known-good policy and halve the learning rate (user: guard against lr side effects)."""
    good = state.get("good_policy")
    lr = state.get("learning_rate", CONFIG["learning_rate"]) / 2
    log(f"ROLLBACK: {reason}; policy {state['policy']['version']} -> {good['version'] if good else 'unchanged'}; lr -> {lr:g}")
    if good:
        state["policy"] = dict(good)
    state["learning_rate"] = lr
    state.setdefault("rollbacks", []).append({"k": state["k"], "reason": reason, "lr": lr, "to": state["policy"]["version"]})
    state["k"] += 1          # never reuse an update directory whose rollouts came from the reverted policy
    state.pop("current", None)
    write_state(state)


def rollback_on_failures(state, cur):
    """Two consecutive updates whose rollouts fail (no submission / format) far above the first update's rate."""
    rates = [h.get("model_failure_rate") for h in state["history"] if h.get("model_failure_rate") is not None]
    base = rates[0] if rates else cur["model_failure_rate"]
    limit = max(0.375, (base or 0) + 0.25)
    bad_now = (cur["model_failure_rate"] or 0) >= limit
    bad_prev = bool(rates) and rates[-1] >= limit and len(rates) > 1
    if bad_now and bad_prev:
        rollback(state, f"rollout model-failure rate {cur['model_failure_rate']:.2f} and {rates[-1]:.2f} >= {limit:.2f}")
        return True
    return False


def check_eval_regression(state, result):
    """Rollback guard, relaxed 2026-10-21 (user): the old rule (validation -2 / public -3 from the best so far) fired on
    judge noise — the max over noisy 20-run evals is a biased bar, and under mechanism rubric v3 the u30 "regression"
    (14) re-scored 18, above its reference. Now the bar is this epoch's start-policy reference, and a drop must be large
    once or repeat in consecutive evaluations (CONFIG regression, defaults below)."""
    rg = {"validation_drop": 4, "validation_drop_repeat": 3, "public_drop_repeat": 3, **(CONFIG.get("regression") or {})}
    vkey = "validation10" if "validation10" in CONFIG["eval"]["tracks"] else "validation"
    refs = track_refs(state)
    pkey = "dev24" if "dev24" in CONFIG["eval"]["tracks"] else "public"   # 2026-10-28: the dev panel replaces public12
    ref_p = refs.get(pkey, {}).get("root_plus_mechanism"); ref_v = refs.get(vkey, {}).get("root_plus_mechanism")
    v = result["tracks"].get(vkey, {}).get("root_plus_mechanism"); pb = result["tracks"].get(pkey, {}).get("root_plus_mechanism")
    prev = next((e for e in reversed(state["evals"][:-1]) if same_rev(e) and vkey in e.get("tracks", {})), None)
    pv = prev["tracks"][vkey]["root_plus_mechanism"] if prev else None
    # 2026-10-24 (user, fewer eval runs): an eval with fewer runs than the reference is put on the reference's scale
    ref_total = refs.get(vkey, {}).get("total")
    def on_ref_scale(count, ev):
        total = (ev or {}).get("tracks", {}).get(vkey, {}).get("total")
        return count * ref_total / total if count is not None and ref_total and total and total != ref_total else count
    v, pv = on_ref_scale(v, result), on_ref_scale(pv, prev)
    pp = prev["tracks"].get(pkey, {}).get("root_plus_mechanism") if prev else None
    if ref_v is not None and v is not None and v >= ref_v and (ref_p is None or pb is None or pb >= ref_p - 1):
        state["good_policy"] = dict(state["policy"]); write_state(state)
        return False
    reasons = []
    if ref_v is not None and v is not None:
        if v <= ref_v - rg["validation_drop"]:
            reasons.append(f"{vkey} {v} vs reference {ref_v} (-{rg['validation_drop']} or more)")
        elif v <= ref_v - rg["validation_drop_repeat"] and pv is not None and pv <= ref_v - rg["validation_drop_repeat"]:
            reasons.append(f"{vkey} {v} and previous {pv} vs reference {ref_v} (-{rg['validation_drop_repeat']} twice)")
    if ref_p is not None and pb is not None and pb <= ref_p - rg["public_drop_repeat"] and pp is not None and pp <= ref_p - rg["public_drop_repeat"]:
        reasons.append(f"{pkey} {pb} and previous {pp} vs reference {ref_p} (-{rg['public_drop_repeat']} twice)")
    if reasons:
        rollback(state, "evaluation regression: " + "; ".join(reasons))
        return True
    return False


def record_metrics(state):
    """metrics.json for the dashboard: per-update rollout rewards/success, training stats, evaluations."""
    rows = []
    for h in state["history"]:
        rewards = [x for r in (h.get("rewards") or {}).values() for x in r]
        rel = h.get("release") or {}
        rows.append({"update": h["k"], "result": h.get("result"), "cases": h.get("cases"),
                     "mean_reward": sum(rewards) / len(rewards) if rewards else None,
                     "success_rate": (sum(1 for x in rewards if x >= .7) / len(rewards)) if rewards else None,
                     "rewards": h.get("rewards"), "grad_norm": rel.get("preclip_gradient_norm"),
                     "model_failure_rate": h.get("model_failure_rate"), "learning_rate": h.get("learning_rate"),
                     "policy_version": h.get("policy_version"), "train_status": h.get("train_status"), "replay": bool(h.get("replay")),
                     "ess": {g: [round(e["summary"]["raw_normalized_ess"], 4) for e in r.get("episodes", [])]
                             for g, r in (rel.get("reports") or {}).items()} if rel.get("reports") else None})
    (WORK / "metrics.json").write_text(json.dumps({"updated_kst": datetime.datetime.now(KST).isoformat(), "updates": rows,
                                                   "evals": state["evals"], "policy": state["policy"], "rollbacks": state.get("rollbacks", []),
                                                   "learning_rate": state.get("learning_rate", CONFIG["learning_rate"]),
                                                   "current": state.get("current"), "milestone_reference": state.get("milestone_reference"),
                                                   "milestone_evals": state.get("milestone_evals", []),
                                                   "track_references": state.get("track_references", {}),
                                                   "prompt_revision": prompt_rev(), "epoch": epoch_key(),
                                                   "track_references_by_rev": state.get("track_references_by_rev", {}),
                                                   "milestone_reference_by_rev": state.get("milestone_reference_by_rev", {})},
                                                  ensure_ascii=False, indent=2, default=str))

_CODE_STAMP = None


def code_stamp():
    return tuple(os.stat(WORK / f).st_mtime_ns for f in ("loop.py", "config.json"))


def wait_if_paused():
    """Hold at a phase boundary while WORK/PAUSE exists (operator GPU windows, e.g. validating a new trainer)."""
    noted = False
    while (WORK / "PAUSE").exists():
        if not noted:
            log("paused at phase boundary (remove PAUSE to continue)"); noted = True
        time.sleep(30)
    if noted:
        log("resumed")


def reload_if_changed():
    """Re-exec at a phase boundary when loop.py/config.json changed, so edits apply without losing in-flight work."""
    wait_if_paused()
    if _CODE_STAMP is not None and code_stamp() != _CODE_STAMP:
        log("loop code/config changed; re-executing at phase boundary")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def measure_new_track_references(state):
    """A track added after the baseline (e.g. validation_new4 with split v3) is first measured on the start policy, once."""
    base = state["evals"][0]["tracks"] if state["evals"] and same_rev(state["evals"][0]) else {}
    missing = {t: sp for t, sp in CONFIG["eval"]["tracks"].items() if t not in base and t not in track_refs(state)}
    if not missing:
        return
    ref = CONFIG["initial_policy"]
    model = start_servers(ref)
    tag = "reference-" + "-".join(missing) + (f"-e{epoch_key()}" if epoch_key() != "1" else "")
    r = evaluate(tag, model, state, tracks=missing, policy_version=ref["version"])
    track_refs(state).update(r["tracks"]); write_state(state)


def updated_count(state):
    return sum(1 for h in state["history"] if h.get("result") == "updated")


def prompt_rev():
    """Student prompt revision of rollouts and evaluations (2026-10-19, user: conditions that exposed a defect go into the
    mechanism, not into causes). Scores are compared only within one revision: each revision has its own start-policy
    references (track and milestone), measured on first use."""
    return int(CONFIG.get("prompt_revision", 1))


def context_policy():
    """Student history view (rca_lab.mcp.context). 2026-10-28 user: "budget" (append-only between compactions) replaces
    the per-turn rolling8 compaction that broke prefix reuse; a new comparison epoch, references re-measured."""
    return CONFIG.get("context_policy", "rolling8")


def rev_args():
    return (["--prompt-revision", str(prompt_rev())] if prompt_rev() != 1 else []) \
        + (["--context-policy", context_policy()] if context_policy() != "rolling8" else [])


def judge_rubric():
    """Mechanism judge rubric of rewards and evaluations (2026-10-21: v3 defines the trigger; load is a condition)."""
    return (CONFIG.get("judge") or {}).get("rubric", "v2")


def depth_rubric():
    return (CONFIG.get("judge") or {}).get("depth_rubric", "rca-causal-depth-v1")


def mechanism_score(run_dir):
    """Score of the configured judge rubric's own verdict file for one run (None when not judged)."""
    sys.path.insert(0, str(ROOT / "src"))
    from rca_lab.mcp.rl_reward import judge_cache_path
    jp = judge_cache_path(Path(run_dir), CONFIG["judge"]["provider"], model=CONFIG["judge"]["model"], rubric=judge_rubric())
    if not jp.exists():
        return None
    d = json.loads(jp.read_text())
    return d.get("score") if d.get("model") == CONFIG["judge"]["model"] and d.get("rubric", "v1") == judge_rubric() else None


def mcp_binary():
    """rca-mcp binary of rollouts and evaluations (2026-10-20: read_timeseries bucket label fix, see its provenance.json)."""
    return str(ROOT / CONFIG.get("mcp_binary", "outputs/sealed-sonnet55-20261012/runtime/rca-mcp"))


def epoch_key():
    """Comparison epoch: prompt revision, plus the tool binary tag once the tools changed. Scores compare only within an
    epoch; each epoch measures its own start-policy references on first use."""
    return (str(prompt_rev()) + (f"-{CONFIG['mcp_tag']}" if CONFIG.get("mcp_tag") else "")
            + (f"-j{judge_rubric()[1:]}" if judge_rubric() != "v2" else "")
            + (f"-c{context_policy()}" if context_policy() != "rolling8" else ""))


def same_rev(entry):
    return entry.get("epoch", str(int(entry.get("prompt_revision", 1)))) == epoch_key()


def track_refs(state):
    """Start-policy references of the regular eval tracks for the current prompt revision (revision 1 keeps its key)."""
    if epoch_key() == "1":
        return state.setdefault("track_references", {})
    return state.setdefault("track_references_by_rev", {}).setdefault(epoch_key(), {})


def milestone_ref(state):
    if epoch_key() == "1":
        return state.get("milestone_reference")
    return (state.get("milestone_reference_by_rev") or {}).get(epoch_key())


def ensure_parent(state):
    """Policies released before parent tracking existed: the parent is the previous released policy (or the initial one)."""
    pol = state["policy"]
    if pol.get("parent") or pol["version"] == CONFIG["initial_policy"]["version"]:
        return
    released = [h["policy_version"] for h in state["history"] if h.get("result") == "updated" and h.get("policy_version")]
    if released and released[-1] == pol["version"]:
        pol["parent"] = released[-2] if len(released) > 1 else CONFIG["initial_policy"]["version"]
        write_state(state)
        log(f"policy {pol['version']}: parent recorded as {pol['parent']}")


def queue_replay(state, k):
    """Replay update (2026-10-15, user): groups already sampled by the current policy or its parent (one step stale)
    are trained without new rollouts. The queue arrives as WORK/replay-queue.json (operator) and is consumed once."""
    qf = WORK / "replay-queue.json"
    if qf.exists() and not state.get("replay"):
        state["replay"] = json.loads(qf.read_text()); qf.rename(qf.with_suffix(".consumed.json")); write_state(state)
    rp = state.get("replay")
    if not rp or state.get("current"):
        return
    pol = state["policy"]
    state.pop("replay")
    if rp["behavior"] not in (pol["version"], pol.get("parent")):
        log(f"u{k}: replay of {rp['cases']} dropped: behavior {rp['behavior']} is more than one step behind {pol['version']}")
        write_state(state)
        return
    state["current"] = {"k": k, "phase": "train", "groups": rp["groups"], "rewards": rp["rewards"], "cases": rp["cases"],
                        "data": rp["data"], "replay": True, "model_failure_rate": None}
    write_state(state)
    log(f"u{k}: replay update on {rp['cases']} (behavior {rp['behavior']}, stale steps {0 if rp['behavior'] == pol['version'] else 1})")


def extra_track(name):
    """Milestone-only tracks (2026-10-18, user): the Polestar sealed set and the original 12-scenario set, measured with a
    start-policy reference. Report only; never used to tune the loop."""
    import yaml
    cfg = CONFIG["eval"]["milestone_extra"][name]
    cases = cfg.get("cases") or yaml.safe_load(Path(cfg["split"]).read_text())[cfg["partition"]]
    return {"runs": cfg["runs"], "allow_sealed": bool(cfg.get("allow_sealed")),   # the case runner refuses sealed cases otherwise
            "items": [{"case": c, "cases_root": cfg["cases_root"], "contract": cfg["contract"], "split": cfg["split"]} for c in sorted(cases)]}


def run_milestone(state):
    """Full public held-out eval at milestone update counts (plus the sealed set where configured); the start policy is
    measured once per track as the reference."""
    done = updated_count(state)   # successful optimizer steps, not attempts
    if done not in CONFIG["eval"].get("milestones", []) or any(m["at"] == done for m in state.get("milestone_evals", [])):
        return
    full = {"public_full": full_public_track()}
    for name, cfg in (CONFIG["eval"].get("milestone_extra") or {}).items():
        if done in cfg.get("at", []):
            full[name] = extra_track(name)
    log(f"milestone {done}: " + ", ".join(f"{t} ({len(v['items'])} cases x {v['runs']})" for t, v in full.items()))
    ref = CONFIG["initial_policy"]
    need = {t: v for t, v in full.items() if not (milestone_ref(state) or {}).get("tracks", {}).get(t)}
    if need:
        model = start_servers(ref)
        sfx = f"-e{epoch_key()}" if epoch_key() != "1" else ""
        res = evaluate(("milestone-reference" if "public_full" in need else "milestone-reference-extra") + sfx, model, state,
                       tracks=need, policy_version=ref["version"])
        holder = state if epoch_key() == "1" else state.setdefault("milestone_reference_by_rev", {})
        mr = holder.setdefault("milestone_reference" if epoch_key() == "1" else epoch_key(), {**res, "tracks": {}})
        mr["tracks"].update(res["tracks"]); write_state(state)
    model = start_servers(state["policy"])
    res = evaluate(f"milestone-u{done:03d}" + (f"-e{epoch_key()}" if epoch_key() != "1" else ""), model, state, tracks=full)
    state.setdefault("milestone_evals", []).append({"at": done, **res}); write_state(state)


def milestone_track_spec(name):
    return full_public_track() if name == "public_full" else extra_track(name)


def repair_milestones(state):
    """Re-run the cases a milestone track lost (2026-10-23, user: the start-policy original12 reference lost 4 cases to a tunnel
    drop) into the same eval dir, then re-total the track from every scored run. The policy side is repaired only while the
    measured policy is still the current one."""
    sides = []
    mr = milestone_ref(state)
    if mr:
        sides.append(("reference", mr, CONFIG["initial_policy"]))
    if state.get("milestone_evals"):
        last = state["milestone_evals"][-1]
        if last.get("policy_version") == state["policy"]["version"] and last.get("epoch") == epoch_key():
            sides.append(("policy", last, state["policy"]))
    switched = False
    for side, res, pol in sides:
        tag = res["tag"]
        todo, full = {}, {}
        for track, got in res["tracks"].items():
            spec = milestone_track_spec(track)
            edir = WORK / "eval" / tag / track
            missing = [i for i in spec["items"] if scored_runs(edir, i["case"]) < spec["runs"]]
            if missing:
                todo[track] = {**spec, "items": missing}; full[track] = spec["items"]
        if not todo:
            continue
        log(f"milestone repair ({side}, {tag}): " + ", ".join(f"{t} {len(v['items'])} cases" for t, v in todo.items()))
        model = start_servers(pol); switched = True
        fixed = evaluate(tag, model, state, tracks=todo, policy_version=pol["version"], merge=True, full_items=full)
        res["tracks"].update({t: fixed["tracks"][t] for t in todo}); write_state(state)
    if switched and state.get("current") and state["current"].get("phase") != "serve":   # in-flight update expects the policy served
        state["current"]["model"] = start_servers(state["policy"]); write_state(state)


# ---------------------------------------------------------------- main loop
def mark(cur, name):
    """Wall-clock mark of an update phase (2026-10-28 ledger, Codex topic 7: attribute cycle time and discarded work)."""
    cur.setdefault("times", {}).setdefault(name, time.time())


def step_age(state, behavior):
    """Optimizer steps between the policy that sampled a group and the current policy (an update can hold several steps)."""
    pol = state["policy"]
    if behavior == pol["version"]:
        return 0
    steps = {h.get("policy_version"): (h.get("release") or {}).get("optimizer_steps") for h in state["history"]}
    a, b = pol.get("optimizer_steps"), steps.get(behavior)
    return a - b if isinstance(a, int) and isinstance(b, int) else None


def finish(state, entry):
    """Append an update to history and write its ledger: candidate funnel, behaviour age, gate outcomes, stage times."""
    state["history"].append(entry)
    try:
        k = entry.get("k")
        udir = WORK / f"u{k:03d}"
        prog = {}
        if (udir / "train-progress.json").exists():
            prog = json.loads((udir / "train-progress.json").read_text())
        te = prog.get("tree_episodes") or []
        rel = entry.get("release") or prog.get("release") or {}
        led = {"k": k, "result": entry.get("result"), "reason": entry.get("reason"), "context_policy": context_policy(),
               "learning_rate": entry.get("learning_rate"), "times": entry.get("times"), "funnel": entry.get("funnel"),
               "kept": {s: {x: e.get(x) for x in ("case", "slot", "behavior", "step_age", "rewards")} for s, e in (entry.get("kept") or {}).items()},
               "extra_solved": [e.get("case") for e in entry.get("extra_solved") or []],
               "failed_candidates": entry.get("failed_candidates"), "draws": entry.get("draws"),
               "train": {"status": entry.get("train_status") or prog.get("status"), "optimizer_steps": rel.get("optimizer_steps"),
                         "steps_this_update": rel.get("steps_this_update"), "preclip_gradient_norms": rel.get("preclip_gradient_norms"),
                         "failed_groups": rel.get("failed_groups"),
                         "group_steps": {g: {"passed": v.get("passed"), "min_ess": min([a.get("raw_normalized_ess", 1) for a in v.get("alarms", [])], default=None)}
                                         for g, v in (prog.get("group_steps") or {}).items()},
                         "elapsed_seconds": prog.get("elapsed_seconds"), "computed_tokens": sum(e.get("computed_tokens", 0) for e in te),
                         "per_turn_tokens": sum(e.get("per_turn_tokens", 0) for e in te)}}
        (udir / "ledger.json").write_text(json.dumps(led, indent=2))
    except Exception as exc:   # bookkeeping must never stop the loop
        log(f"ledger for u{entry.get('k')} not written ({str(exc)[:200]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-updates", type=int, default=CONFIG["max_updates"])
    args = ap.parse_args()
    state = read_state() or {"policy": CONFIG["initial_policy"], "history": [], "evals": [], "k": CONFIG["first_update_index"]}
    write_state(state)
    log(f"loop start: policy {state['policy']['version']}, next update u{state['k']}")
    global _CODE_STAMP
    _CODE_STAMP = code_stamp()
    keep_tunnel()
    while len(state["history"]) < args.max_updates:
        reload_if_changed()
        pa = WORK / "PAUSE_AFTER"   # operator: hold the loop (GPUs free) once update N is finished (2026-10-15: Gemma window)
        if pa.exists() and not state.get("current") and state["k"] > int(pa.read_text().strip()):
            pa.unlink(); (WORK / "PAUSE").touch()
            log(f"update u{state['k'] - 1} finished: pausing with GPUs free (PAUSE_AFTER)")
            wait_if_paused()
            reload_if_changed()   # edits made while paused take effect before anything else runs
        ensure_parent(state)
        run_milestone(state)
        repair_milestones(state)
        k = state["k"]; udir = WORK / f"u{k:03d}"; udir.mkdir(exist_ok=True)
        queue_replay(state, k)
        cur = state.setdefault("current", {"k": k, "phase": "serve"})
        if cur["phase"] == "serve":
            model = start_servers(state["policy"]); cur.update(model=model, phase="baseline" if not state["evals"] else "rollout"); write_state(state)
        model = cur.get("model") or (CONFIG["lora_name"] if state["policy"].get("lora_dir") else CONFIG["base_model_name"])
        reload_if_changed()
        if cur["phase"] == "baseline":
            state["evals"].append(evaluate(f"eval-u{k:03d}-start", model, state)); state["good_policy"] = dict(state["policy"])
            cur["phase"] = "rollout"; write_state(state)
        reload_if_changed()
        if cur["phase"] == "rollout":
            mark(cur, "rollout_start")
            cur.setdefault("data", current_data())
            ds = CONFIG.get("dynamic_sampling")
            kept = cur.setdefault("kept", {})   # slot id -> informative group for this update; persisted for restarts
            if not cur.get("picked"):
                take_carry(state, cur, k)
                need = [s for s in slot_ids() if s not in kept]
                specs, cur["prerestored"] = take_prefetch(state, cur, k, need)
                specs = list(specs or [])
                used = {e["case"] for e in kept.values()} | {x["case"] for x in specs}
                for t, want in candidate_plan(need).items():   # top up a prefetch made under a smaller plan
                    have = sum(1 for x in specs if slot_of(x) == t)
                    if have < want:
                        extra = pick_candidates(state, cur["data"], {t: want - have}, exclude=used, draw=0)
                        used |= {x["case"] for x in extra}; specs += extra
                by = {t: [x for x in specs if slot_of(x) == t] for t in SLOT_TYPES}   # interleave [pol0, pub0, pol1, pub1, ...]
                specs = [x for j in range(max(map(len, by.values()), default=0)) for t in SLOT_TYPES for x in by[t][j:j + 1]]
                cur.update(specs=specs, picked=True); write_state(state)
            while True:
                need = [s for s in slot_ids() if s not in kept]
                todo = [x for x in cur["specs"] if slot_of(x) in {slot_type(s) for s in need}
                        and x["case"] not in cur.get("failed_candidates", [])]
                if not need or not todo:
                    break
                log(f"u{k}: rolling {len(todo)} candidate groups for {need}: {[x['case'] for x in todo]}")
                with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                    # spread [polestar0, public0, polestar1, public1] over the servers so each serves both kinds
                    futs = [(x, pool.submit(rollout_and_judge, k, x, CONFIG["servers"][(i + i // 2) % len(CONFIG["servers"])], model,
                                            state["policy"]["version"], udir, cur["data"], cur.get("prerestored")))
                            for i, x in enumerate(todo)]
                    results = []
                    for x, f in futs:   # one failed candidate (restore/rollout/judge) must not sink the others
                        try:
                            results.append((x, f.result()))
                        except Exception as exc:
                            cur.setdefault("failed_candidates", []).append(x["case"])
                            cur.setdefault("funnel", []).append({"case": x["case"], "slot": slot_of(x), "outcome": "failed"})
                            log(f"u{k}: candidate {variant(x)} failed, skipped ({str(exc)[:300]})")
                extras = []
                for x, (g, r, nf) in results:   # candidate order decides which informative group fills a slot
                    entry = {"slot": slot_of(x), "case": x["case"], "group": str(g), "rewards": r, "failures": nf,
                             "behavior": state["policy"]["version"], "step_age": 0,
                             **({"mask": x["mask"]} if x.get("mask") else {}),
                             **({"turn_budget": x["turn_budget"]} if x.get("turn_budget") else {})}
                    if ds and group_is_flat(g):
                        if str(g) not in cur.setdefault("flat_groups", []):
                            cur["flat_groups"].append(str(g))
                        solved = json.loads(Path(g).read_text())["episodes"][0]["joint_success"]
                        se = ds.get("solved_extra") or {}
                        # 2026-10-19 (user): an all-solved group whose rewards still differ (mostly evidence grounding, a little
                        # length) trains as one extra group beyond the slots; it never takes a slot from a mixed group
                        # 2026-10-19 (user): up to max_per_update such groups (the widest spreads); inside an all-solved group
                        # the advantage is only length + evidence (+ causal depth), i.e. efficiency and grounding
                        extras_now = extra_solved_list(cur)
                        if solved and se and max(r) - min(r) >= se["min_spread"] and (
                                len(extras_now) < se.get("max_per_update", 1) or max(r) - min(r) > min(e["spread"] for e in extras_now)):
                            extras_now = sorted(extras_now + [{**entry, "spread": max(r) - min(r)}], key=lambda e: -e["spread"])
                            cur["extra_solved"] = extras_now[:se.get("max_per_update", 1)]
                            log(f"u{k}: {variant(x)} all solved, reward spread {max(r) - min(r):.3f}; kept as an extra solved group")
                        else:
                            log(f"u{k}: {variant(x)} flat (all joint {solved}, reward spread {max(r) - min(r):.3f}); dropped")
                    elif any(e["case"] == x["case"] for e in kept.values()):
                        pass   # already filled a slot (restart replays a finished round)
                    elif free := [s for s in slot_ids() if slot_type(s) == entry["slot"] and s not in kept]:
                        kept[free[0]] = entry
                        log(f"u{k}: slot {free[0]} <- {variant(x)} (reward spread {max(r) - min(r):.3f})")
                    else:
                        extras.append(entry)
                for x, (g, r, nf) in results:
                    outcome = ("kept" if any(e["case"] == x["case"] and e["group"] == str(g) for e in kept.values()) else
                               "extra_solved" if any(e["case"] == x["case"] for e in extra_solved_list(cur)) else
                               "surplus_carried" if any(e["case"] == x["case"] for e in extras) else "flat_dropped")
                    cur.setdefault("funnel", []).append({"case": x["case"], "slot": slot_of(x), "outcome": outcome, "draw": cur.get("draws", 0),
                                                         "spread": round(max(r) - min(r), 3) if r else None,
                                                         "joints": sum(e["joint_success"] for e in json.loads(Path(g).read_text())["episodes"])})
                if extras:   # informative but surplus: train them in the next update instead of discarding the rollout
                    prev = state.get("carry") if (state.get("carry") or {}).get("behavior") == state["policy"]["version"] else None
                    state["carry"] = {"behavior": state["policy"]["version"], "data_version": cur["data"]["version"],
                                      "groups": (prev["groups"] if prev else []) + extras}
                    log(f"u{k}: carrying {[e['case'] for e in extras]} to the next update")
                write_state(state)
                need = [s for s in slot_ids() if s not in kept]
                if not need or not ds or cur.get("draws", 0) >= ds["max_redraws"]:
                    break
                cur["draws"] = cur.get("draws", 0) + 1
                used = {x["case"] for x in cur["specs"]} | {Path(p).parent.name for p in cur.get("flat_groups", [])} \
                    | {e["case"] for e in kept.values()} | set(cur.get("failed_candidates", []))
                cur["specs"] = pick_candidates(state, cur["data"], candidate_plan(need), exclude=used, draw=cur["draws"])
                log(f"u{k}: redraw {cur['draws']} for {need}: {[variant(x) for x in cur['specs']]}")
                write_state(state)
            slots = [s for s in slot_ids() if s in kept]   # legacy numeric keys (pre-oversampling state) are ignored
            if not slots and cur.get("funnel") and all(f["outcome"] == "failed" for f in cur["funnel"]):
                # 2026-10-28: a broken runner (e.g. an unknown CLI flag) fails every candidate in seconds; counting that as a
                # flat update burned 6 updates in 10 minutes. Hold for the operator; the update index moves on, history does not.
                log(f"u{k}: every candidate failed ({len(cur['funnel'])}); pausing (fix the runner, then remove PAUSE)")
                (WORK / "PAUSE").touch(); state["k"] = k + 1; state.pop("current"); write_state(state)
                continue
            if not slots:
                log(f"u{k}: every group flat after {cur.get('draws', 0)} redraws; no update, next update keeps the servers")
                finish(state, {**cur, "result": "no_update", "reason": "flat"}); state["k"] = k + 1
                state["current"] = {"k": k + 1, "phase": "rollout", "model": model}; write_state(state)
                continue
            groups = [kept[s]["group"] for s in slots]; rewards = {kept[s]["case"]: kept[s]["rewards"] for s in slots}
            failures = sum(kept[s]["failures"] for s in slots)
            cur["cases"] = [kept[s]["case"] for s in slots]
            for ex in extra_solved_list(cur):
                if ex["case"] not in rewards:
                    groups.append(ex["group"]); rewards[ex["case"]] = ex["rewards"]; failures += ex["failures"]; cur["cases"].append(ex["case"])
            n_ep = sum(len(r) for r in rewards.values())
            cur["model_failure_rate"] = failures / n_ep if n_ep else None
            if rollback_on_failures(state, cur):
                continue
            mark(cur, "rollout_end")
            cur.update(groups=groups, rewards=rewards, phase="train"); write_state(state)
        reload_if_changed()
        if cur["phase"] == "train":
            # 2026-10-20 (user): an update whose chosen groups hold no joint success trains only on differences between
            # wrong answers (u29-u30: 2/12 successes -> regression); skip it and keep the servers for the next rollout
            min_joint = (CONFIG.get("dynamic_sampling") or {}).get("min_joint_per_update")
            if min_joint:
                joints = sum(e["joint_success"] for g in cur["groups"] for e in json.loads(Path(g).read_text())["episodes"])
                if joints < min_joint:
                    log(f"u{k}: {joints} joint successes in the chosen groups (< {min_joint}); no update, next update keeps the servers")
                    finish(state, {**cur, "result": "no_update", "reason": "no_success"}); state["k"] = k + 1
                    state["current"] = {"k": k + 1, "phase": "rollout", "model": model}; write_state(state)
                    continue
            mark(cur, "train_start")
            stop_servers()
            try:
                prefetch_restores(state, cur)
            except Exception as exc:   # an optimization only; the rollout restores normally without it
                log(f"u{k}: prefetch skipped ({exc})")
            progress, adapter_dir = train(k, state, cur["groups"], udir, cur.get("data"))
            mark(cur, "train_end")
            cur.update(train_status=progress.get("status"), adapter_dir=adapter_dir, release=progress.get("release"),
                       learning_rate=state.get("learning_rate", CONFIG["learning_rate"]))
            if progress.get("status") == "passed":
                cur["phase"] = "export"
            else:
                log(f"u{k}: no update ({progress.get('status')}: {str(progress.get('error') or progress.get('release', {}).get('failed_groups'))[:300]})")
                finish(state, {**cur, "result": "no_update"}); state["k"] = k + 1; state.pop("current"); write_state(state)
                continue
            write_state(state)
        reload_if_changed()
        if cur["phase"] == "export":
            lora_dir = export(k, cur["adapter_dir"])
            mark(cur, "export_end")
            rel = cur["release"]
            state["policy"] = {"version": rel["policy_version"], "adapter": f"{cur['adapter_dir']}/adapter.pt", "lora_dir": lora_dir,
                               "optimizer_steps": rel["optimizer_steps"], "parent": state["policy"]["version"]}
            finish(state, {**cur, "result": "updated", "policy_version": rel["policy_version"]}); state["k"] = k + 1
            state.pop("current"); write_state(state)
            log(f"u{k}: released -> {rel['policy_version']} (steps {rel['optimizer_steps']}, grad norm {rel.get('preclip_gradient_norm')})")
            if updated_count(state) % CONFIG["eval_every"] == 0:
                measure_new_track_references(state)
                model = start_servers(state["policy"])
                state["evals"].append(evaluate(f"eval-u{k:03d}", model, state)); write_state(state)
                if not check_eval_regression(state, state["evals"][-1]):
                    state["current"] = {"k": state["k"], "phase": "rollout", "model": model}; write_state(state)
    log("loop finished")


if __name__ == "__main__":
    main()
