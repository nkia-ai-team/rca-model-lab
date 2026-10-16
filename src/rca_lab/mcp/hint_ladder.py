"""Real-time hint ladder for student rollouts (orchestrator side).

A train capture whose blind runs all miss the golden roots is retried inside the same restore
window with ladder guidance in the student's *live* system prompt only (StudentConfig.hint):

  rung 0  blind
  rung 1  the student's own wrong picks are named as rejected (a verdict, never the answer);
          when some picks were right but roots are missing, the conclusion is called incomplete
  rung 2  rung 1 + the capture's written ladder hint (configs/teacher/hints-*.yaml)

The ladder stops at the first run that names every root (root_f1 = 1). Stored trajectories stay
clean; score.json carries hinted / hint_sha256 / hint_rung, and the hint texts go to the
case-level ladder.json (orchestrator record, never read by the exporters).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from rca_lab.mcp.golden import ExpectedCase, RootCandidate, candidates_from_result, root_matches

NO_PICK_TEXT = "이전 조사는 근본 원인을 맞히지 못했다."
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def pick_verdicts(expected: ExpectedCase, result: dict[str, Any] | None) -> tuple[list[RootCandidate], int, int]:
    """(wrong picks, matched roots, missed roots) of one answer against the golden roots."""
    actual = candidates_from_result(result or {})
    wrong = [candidate for candidate in actual if not any(root_matches(root, candidate) for root in expected.roots)]
    matched = sum(any(root_matches(root, candidate) for candidate in actual) for root in expected.roots)
    return wrong, matched, len(expected.roots) - matched


def candidate_key(candidate: RootCandidate) -> str:
    return candidate.target_id if candidate.variant == "target" else (candidate.pseudo_name or candidate.pseudo_id)


def target_names(project: str, ids: list[str]) -> dict[str, str]:
    """UUID -> target name from the restored capture's PostgreSQL (the student sees names, not raw UUIDs)."""
    wanted = sorted({value for value in ids if UUID_RE.match(value)})
    if not wanted:
        return {}
    quoted = ",".join(f"'{value}'" for value in wanted)
    proc = subprocess.run(
        ["docker", "exec", f"{project}-postgres-1", "psql", "-U", "lucida", "-d", "lucida", "-At", "-F", "\t",
         "-c", f"SELECT id::text, name FROM targets WHERE id::text IN ({quoted})"],
        capture_output=True, text=True, check=False,
    )
    return dict(line.split("\t", 1) for line in proc.stdout.splitlines() if "\t" in line)


def exclusion_hint(results: list[dict[str, Any] | None], expected: ExpectedCase, names: dict[str, str]) -> str:
    """Rung 1 text from every failed answer so far: wrong picks (most frequent first) + incompleteness."""
    wrong_counts: Counter[str] = Counter()
    partial = full_target = False
    for result in results:
        wrong, matched, missed = pick_verdicts(expected, result)
        # Only internal targets are named as rejected. A wrong *external* pick is free text that can be
        # the scenario's trigger or background (e.g. an ingress surge in front of a capacity bottleneck):
        # calling it "wrong" would poison the next attempt. Claude-written rungs handle those.
        wrong_counts.update(dict.fromkeys(candidate.target_id for candidate in wrong if candidate.variant == "target" and candidate.target_id).keys())
        partial = partial or (matched > 0 and missed > 0)
        # every root named and nothing wrong, yet the ladder goes on: under the mechanism stop that
        # means the explanation of the failure failed the judge
        full_target = full_target or (matched == len(expected.roots) and not wrong)
    lines = []
    if wrong_counts:
        labels = [names.get(key, key) for key, _ in wrong_counts.most_common()]
        lines.append(f"이전 조사 판정: {', '.join(labels)} 을(를) 근본 원인으로 지목한 결론은 오답 판정.")
    if partial:
        lines.append("지목 중 일부는 맞지만 결론이 불완전하다 — 놓친 근본 원인이 더 있다.")
    if full_target:
        lines.append("대상 지목이 맞은 결론도 있었지만, 그 대상에서 무엇이 어떻게 고장 났는지에 대한 설명은 오답 판정.")
    if not lines:
        lines.append(NO_PICK_TEXT)
    return "\n".join(lines)


def load_ladder_hints(paths: list[str] | None) -> dict[str, str]:
    """Written ladder hints by capture id; earlier files win."""
    merged: dict[str, str] = {}
    for path in paths or []:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for case_id, text in raw.items():
            if str(text).strip():
                merged.setdefault(str(case_id), str(text).strip())
    return merged


def rung_hint(rung: int, *, results: list[dict[str, Any] | None], expected: ExpectedCase, names: dict[str, str], written: str | None) -> str | None:
    """Live-prompt hint for a rung; None when the rung has nothing to add (ladder ends)."""
    if rung == 0:
        return None
    exclusion = exclusion_hint(results, expected, names)
    if rung == 1:
        return exclusion
    if rung == 2 and written:
        return exclusion + "\n" + written
    return None


# ---------------------------------------------------------------------------------------------
# Claude-written rungs (2 = direction, 3 = navigation). The orchestrator may read the grader-only
# scenario metadata to write a hint; the student never sees it. Each draft is checked twice before
# use: a deterministic root-name check (rung 2) and a separate Claude leak audit; a rejected draft
# is rewritten with the reason, up to three drafts.

WRITER_SYSTEM = """You coach an incident root-cause investigator (an LLM agent) that failed a practice capture. You write one short ladder hint that is added to its next attempt.
You receive: the scenario author's reference (title, description, cause, injection, user impact, distinguishing evidence) - this is the answer and must stay hidden; the investigator's failed attempts (final answers, and the tool-call sequence of the latest ones); the hints it already got; its tool names.
Find the specific wrong turn in its attempts: what it anchored on, which path or evidence it never examined, why its picks are background or downstream effects. Correct that turn.
Rules:
- Never reveal the answer. Do not state the root failure mechanism or the trigger (what changed or broke), and do not paraphrase it so closely that it is given away. Unless rung is 3, do not name any component listed in forbidden_root_names.
- rung 2 (direction): say why its conclusions miss, and point to the kind of path, layer, component class or time region it has not examined.
- rung 3 (navigation): also give concrete steps with its tools - which tools to call, which class of metric, log, trace, Kubernetes or database evidence to pull, which time regions to compare. You may name a component to inspect as a step, but never say that it is the cause or what went wrong in it.
- The investigator only sees telemetry through its tools (metrics, logs, traces, Kubernetes state and events, database sessions and queries). Never refer to source code, class or method names, annotations or configuration file contents.
- Refer to components by the names that appear in its attempts, never by UUID or T-handle.
- Do not repeat a hint already given; build on it. If rejected_draft is present, fix the stated problem.
- Korean, at most 5 sentences, no preamble, never mention that you know the answer.
Answer with JSON only: {"hint": "<text>"}"""

LEAK_SYSTEM = """You audit a hint written for an incident investigator practicing on a capture. Given the reference root cause and the hint, decide whether the hint gives away the answer.
A leak is: stating or unmistakably implying the root failure mechanism or trigger, or declaring which component is the root cause. Saying which conclusions were wrong, pointing to where to look, or naming which evidence to pull is not a leak; naming a component to inspect is allowed only when rung is 3.
Answer with JSON only: {"leak": true | false, "reason": "<one sentence>"}"""

HANDLE_RE = re.compile(r"\bT\d+\b")


def scenario_reference(case_dir: Path) -> dict[str, str]:
    meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    scenario = dict(meta.get("scenario_metadata") or {})
    from rca_lab.mcp.rl_reward import reference_override

    override = reference_override(case_dir.name)
    if override:  # corrected reference wins; drop meta fields that describe the superseded mechanism
        scenario.update({key: override[key] for key in ("title", "cause") if override.get(key)})
        for key in ("description", "user_impact", "distinguishing_evidence"):
            scenario.pop(key, None)
        scenario["observed_evidence"] = str(override.get("evidence", ""))
    keys = ("title", "description", "cause", "injection_summary", "user_impact", "distinguishing_evidence", "observed_evidence")
    return {key: str(scenario.get(key, "")) for key in keys if scenario.get(key)}


K8S_KEY_RE = re.compile(r"^[0-9a-f-]{36}:(?P<kind>[a-z]+):[^/]+/(?P<rest>.+)$")
WORKLOAD_KINDS = frozenset({"deployment", "statefulset", "daemonset", "service"})


def _short_name(name: str) -> str | None:
    """k8s resource keys (<cluster>:<kind>:<ns>/<name>[/<container>]) -> the name a hint would use;
    pod / replicaset keys carry hashes and are covered by their workload name."""
    match = K8S_KEY_RE.match(name)
    if not match:
        return name
    if match["kind"] == "container":
        return match["rest"].rsplit("/", 1)[-1]
    return match["rest"] if match["kind"] in WORKLOAD_KINDS else None


def forbidden_root_names(expected: ExpectedCase, names: dict[str, str]) -> list[str]:
    found: set[str] = set()
    for root in expected.roots:
        found.update(names[target] for target in root.target_ids if target in names)
        found.update(root.target_aliases)
        found.update(value.removeprefix("external:") for value in root.pseudo_ids)
    short = {_short_name(value) for value in found}
    return sorted(value for value in short if value and len(value) >= 3)


def names_leaked(hint: str, forbidden: list[str]) -> list[str]:
    lowered = hint.lower()
    return [name for name in forbidden if name.lower() in lowered]


def attempt_digest(run_dir: Path, result: dict[str, Any] | None, names: dict[str, str], *, with_calls: bool, max_calls: int = 45) -> dict[str, Any]:
    """What the investigator concluded (and, for the latest runs, which calls it made) in readable names."""
    alias_path = run_dir / "alias.json"
    handles = {handle: uuid for uuid, handle in json.loads(alias_path.read_text(encoding="utf-8")).items()} if alias_path.exists() else {}

    def readable(text: str) -> str:
        text = HANDLE_RE.sub(lambda m: names.get(handles.get(m.group(0), ""), m.group(0)), text)
        return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lambda m: names.get(m.group(0), m.group(0)), text)

    result = result or {}
    digest: dict[str, Any] = {
        "status": result.get("status"),
        "summary": readable(str(result.get("summary", "")))[:900],
        "causes": [{"target": names.get(str(c.get("target", "")), str(c.get("target", ""))), "mechanism": readable(str(c.get("mechanism", "")))[:400]} for c in result.get("causes", [])],
        "external_causes": [{"name": str(c.get("name") or c.get("id", "")), "mechanism": readable(str(c.get("mechanism", "")))[:300]} for c in result.get("external_causes", [])],
    }
    if with_calls:
        calls = _student_calls(run_dir) if (run_dir / "trajectory.jsonl").exists() else _teacher_calls(run_dir)
        if calls is not None:
            digest["tool_calls"] = [f"{name}({readable(arguments)[:160]})" for name, arguments in calls][-max_calls:]
    return digest


def _student_calls(run_dir: Path) -> list[tuple[str, str]]:
    calls = []
    for line in (run_dir / "trajectory.jsonl").read_text(encoding="utf-8").splitlines():
        message = json.loads(line) if line.strip() else {}
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            calls.append((str(function.get("name")), str(function.get("arguments", ""))))
    return calls


def _teacher_calls(run_dir: Path) -> list[tuple[str, str]] | None:
    """Tool calls of a Claude-teacher run from its CLI stream (`claude -p --output-format stream-json`)."""
    stream = run_dir / "stream.jsonl"
    if not stream.exists():
        return None
    calls = []
    for line in stream.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "assistant":
            continue
        for item in (event.get("message") or {}).get("content") or []:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                calls.append((str(item.get("name", "")).split("__")[-1], json.dumps(item.get("input") or {}, ensure_ascii=False)))
    return calls


def _json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def claude_hint(*, rung: int, reference: dict[str, str], forbidden: list[str], attempts: list[dict[str, Any]], given: list[str], tools: list[str],
                model: str, run_claude: Any = None, drafts: int = 3) -> tuple[str | None, list[dict[str, Any]]]:
    """(hint | None, audit log). None when no draft passed both checks."""
    if run_claude is None:
        from rca_lab.mcp.claude_cli import run_claude
    log: list[dict[str, Any]] = []
    rejected: dict[str, str] | None = None
    for _ in range(drafts):
        payload: dict[str, Any] = {"rung": rung, "reference": reference, "forbidden_root_names": forbidden, "failed_attempts": attempts, "hints_already_given": given, "investigator_tools": tools}
        if rejected:
            payload["rejected_draft"] = rejected
        text, diag = run_claude(system_prompt=WRITER_SYSTEM, prompt=json.dumps(payload, ensure_ascii=False), model=model, timeout_seconds=300)
        hint = str((_json_object(text) or {}).get("hint", "")).strip()
        if not hint:
            log.append({"stage": "write", "error": diag or (text or "")[:200]})
            continue
        leaked = names_leaked(hint, forbidden) if rung < 3 else []
        if leaked:
            rejected = {"draft": hint, "problem": f"names a root-cause component: {', '.join(leaked)}"}
            log.append({"stage": "name_check", "draft": hint, "leaked": leaked})
            continue
        text, diag = run_claude(system_prompt=LEAK_SYSTEM, prompt=json.dumps({"rung": rung, "reference": reference, "hint": hint}, ensure_ascii=False), model=model, timeout_seconds=180)
        verdict = _json_object(text)
        if verdict is None or not isinstance(verdict.get("leak"), bool):
            log.append({"stage": "leak_audit", "draft": hint, "error": diag or (text or "")[:200]})
            continue
        log.append({"stage": "leak_audit", "draft": hint, "leak": verdict["leak"], "reason": verdict.get("reason")})
        if not verdict["leak"]:
            return hint, log
        rejected = {"draft": hint, "problem": str(verdict.get("reason", "gives away the answer"))}
    return None, log
