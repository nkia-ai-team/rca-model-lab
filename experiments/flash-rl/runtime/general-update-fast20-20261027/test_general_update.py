"""CPU contracts for general_update (multi-group, start adapter, AdamW continuation, flat skip, drift gate, resume)."""
import copy, json, sys
from pathlib import Path
import pytest, torch
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from general_update import GeneralUpdate, DEFAULTS
from runtime_identity import SCHEMA, bind_profile, digest
from rca_lab.train.grpo_correction import corrected_grpo_loss
from rca_lab.train import grpo


class Toy(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.lora_A = torch.nn.Parameter(torch.tensor([[.2, -.1], [.3, .4]]))
        self.lora_B = torch.nn.Parameter(torch.zeros(2, 2))
        self.register_buffer("base", torch.tensor([[.1, -.3], [.2, .4]]))
    def logps(self, turn):
        tokens = torch.tensor(turn["completion_token_ids"])
        x = torch.stack((tokens.float() + 1, tokens.float() * .1 + .5), dim=-1)
        logits = x @ (self.base + self.lora_A @ self.lora_B)
        return logits.log_softmax(-1).gather(-1, (tokens % 2)[:, None]).T

VERSION = "adapter-test0001"

def group(policy, gid, rewards, lengths_list, version=VERSION):
    backend = {"name": "cpu-test"}
    g = dict(policy_version=version, group_id=gid, case_id="case-" + gid, serving_backend=backend,
             serving_backend_sha256=digest(backend), split_sha256="a" * 64,
             sampling=dict(temperature=1., top_p=1., top_k=-1, min_p=0.), episodes=[])
    for i, lengths in enumerate(lengths_list):
        turns = []
        for length in lengths:
            t = dict(prompt_token_ids=[1, 2], completion_token_ids=list(range(length)))
            t["old_logprobs"] = policy.logps(t).detach().reshape(-1).tolist()
            turns.append(t)
        r = rewards[i]
        g["episodes"].append(dict(episode_id=f"{gid}-{i}", judge_status="completed", reward=r, joint_success=r >= .8,
                                  turns=turns, generated_tokens=sum(lengths)))
    return json.dumps(g).encode()

def setup(start_b=None):
    policy = Toy()
    if start_b is not None:
        with torch.no_grad(): policy.lora_B.copy_(start_b)
    g1 = group(policy, "g1", (.8, .1, .9, .2), ((1, 3), (2,), (3, 2, 1), (4, 2)))
    g2 = group(policy, "g2", (.1, .1, .1, .1), ((2,), (1,), (3,), (2,)))          # flat: skipped
    g3 = group(policy, "g3", (.9, .0, .3, .8), ((2, 2), (1,), (2, 3), (3,)))
    profile = bind_profile(dict(probability_backend="toy"), {"schema": SCHEMA, "test": "cpu"})
    bindings = dict(checkpoint_identity_sha256="b" * 64, loader_sha256="c" * 64, split_sha256="a" * 64)
    return policy, [g1, g2, g3], profile, bindings

def run_all(run):
    for g, i, j in run.order:
        run.accumulate(g, i, j, run.policy.logps(run.episode(g, i)["turns"][j]))

def expected_grads(policy, groups_bytes):
    groups = [json.loads(b) for b in groups_bytes]
    rewards = torch.tensor([ep["reward"] for g in groups for ep in g["episodes"]])
    gid = torch.tensor([k for k, g in enumerate(groups) for _ in g["episodes"]])
    adv = grpo.group_advantages(rewards, gid, std_floor=1.)
    active = [(k, i) for k, g in enumerate(groups) for i in range(len(g["episodes"]))
              if adv[gid == k].ne(0).any()]
    loss = 0.
    idx = {(k, i): n for n, (k, i) in enumerate((k, i) for k, g in enumerate(groups) for i in range(len(g["episodes"])))}
    for k, i in active:
        ep = groups[k]["episodes"][i]
        for t in ep["turns"]:
            cur = policy.logps(t); old = torch.tensor(t["old_logprobs"])[None]
            r = corrected_grpo_loss(cur, cur.detach(), old, cur.detach(), torch.ones_like(cur, dtype=torch.bool),
                                    adv[idx[(k, i)]:idx[(k, i)] + 1], importance_cap=2., clip_epsilon=.2, kl_beta=0.)
            loss = loss + r.loss * cur.numel() / ep["generated_tokens"] / len(active)
    loss.backward()
    return {n: p.grad.clone() for n, p in policy.named_parameters()}

def test_multigroup_matches_two_pass_and_skips_flat(tmp_path):
    policy, groups, profile, bindings = setup()
    ref = copy.deepcopy(policy)
    run = GeneralUpdate(policy, groups, profile, bindings, policy_version=VERSION)
    assert run.identity["active_groups"] == [0, 2] and all(g != 1 for g, _, _ in run.order)
    run_all(run)
    want = expected_grads(ref, groups)
    for n, p in policy.named_parameters():
        assert torch.allclose(p.grad, want[n], atol=1e-7), n
    res = run.release(tmp_path / "out", profile)
    assert res["status"] == "passed" and res["optimizer_steps"] == 1
    saved = torch.load(tmp_path / "out" / "adapter.pt", weights_only=False)
    assert saved["optimizer_steps"] == 1 and saved["parent_policy_version"] == VERSION

def test_adamw_continues_from_saved_state(tmp_path):
    policy, groups, profile, bindings = setup()
    run = GeneralUpdate(policy, groups, profile, bindings, policy_version=VERSION)
    run_all(run); run.release(tmp_path / "s1", profile)
    s1 = torch.load(tmp_path / "s1" / "adapter.pt", weights_only=False)
    # second update from the released adapter; groups re-recorded under the new version
    p2 = Toy(); p2.load_state_dict({**p2.state_dict(), **{k.split(".")[-1]: v for k, v in s1["state_dict"].items()}})
    v2 = "adapter-test0002"
    g = [group(p2, "h1", (.9, .1, .3, .2), ((2, 1), (3,), (1, 2), (2,)), version=v2)]
    run2 = GeneralUpdate(p2, g, profile, bindings, policy_version=v2, start_state=s1)
    for gg, i, j in run2.order: run2.accumulate(gg, i, j, p2.logps(run2.episode(gg, i)["turns"][j]))
    grads = {n: q.grad.clone() for n, q in p2.named_parameters()}
    res = run2.release(tmp_path / "s2", profile)
    # manual: AdamW with the loaded state, one more step
    m = Toy(); m.load_state_dict({**m.state_dict(), **{k.split(".")[-1]: v for k, v in s1["state_dict"].items()}})
    names = sorted(n for n, q in m.named_parameters())
    opt = torch.optim.AdamW([dict(m.named_parameters())[n] for n in names], lr=DEFAULTS["learning_rate"], weight_decay=0.)
    opt.load_state_dict(s1["optimizer"]["state"])
    for n, q in m.named_parameters(): q.grad = grads[n]
    torch.nn.utils.clip_grad_norm_([dict(m.named_parameters())[n] for n in names], 1.)
    opt.step()
    s2 = torch.load(tmp_path / "s2" / "adapter.pt", weights_only=False)
    for n, q in m.named_parameters():
        assert torch.allclose(s2["state_dict"][n], q.detach(), atol=1e-8), n
    assert s2["optimizer_steps"] == 2 and res["status"] == "passed"

def test_drift_budget_blocks_release(tmp_path):
    policy, groups, profile, bindings = setup()
    run = GeneralUpdate(policy, groups, profile, bindings, policy_version=VERSION)
    for g, i, j in run.order:
        cur = run.policy.logps(run.episode(g, i)["turns"][j])
        run.accumulate(g, i, j, cur + torch.where(torch.arange(cur.shape[1]) % 2 == 0, -3., 0.)[None])
    res = run.release(tmp_path / "out", profile)
    assert res["status"] == "blocked_drift_budget" and not (tmp_path / "out" / "adapter.pt").exists()

def test_resume_and_version_guard(tmp_path):
    policy, groups, profile, bindings = setup()
    run = GeneralUpdate(policy, groups, profile, bindings, policy_version=VERSION)
    for g, i, j in run.order[:5]: run.accumulate(g, i, j, run.policy.logps(run.episode(g, i)["turns"][j]))
    sha = run.save_prefix(tmp_path / "p.pt")
    p2, groups2, _, _ = setup()
    run2 = GeneralUpdate(p2, groups, profile, bindings, policy_version=VERSION); run2.resume(tmp_path / "p.pt", sha)
    for g, i, j in run2.order[5:]: run2.accumulate(g, i, j, p2.logps(run2.episode(g, i)["turns"][j]))
    full_p, _, _, _ = setup(); full = GeneralUpdate(full_p, groups, profile, bindings, policy_version=VERSION); run_all(full)
    for (n, a), (_, b) in zip(sorted(p2.named_parameters()), sorted(full_p.named_parameters())):
        assert torch.allclose(a.grad, b.grad, atol=1e-7), n
    with pytest.raises(ValueError):
        GeneralUpdate(Toy(), groups, profile, bindings, policy_version="adapter-other")
