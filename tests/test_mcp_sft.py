"""Hermetic tests for the rca-mcp SFT data path (fake tokenizer, no GPU)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from rca_lab.train.mcp_sft import (
    IGNORE_INDEX,
    _strip_prior_reasoning,
    _template_messages,
    case_weights,
    tokenize_episode,
    tokenize_turns,
)


class FakeTokenizer:
    """Renders each message as one token per role plus one token per character of a digest.

    The end token of an assistant message depends on the next message (like the Muse template):
    `E` when followed by another message, `T` when last.
    """

    def apply_chat_template(self, messages: list[dict[str, Any]], *, tokenize: bool, tools: Any = None, add_generation_prompt: bool = False, **_: Any) -> list[int]:
        assert tokenize
        ids: list[int] = [1]  # system header stands in for the rendered tools
        for index, message in enumerate(messages):
            role = {"system": 10, "user": 11, "assistant": 12, "tool": 13}[message["role"]]
            body = json.dumps({key: value for key, value in message.items() if key != "role"}, sort_keys=True)
            ids.append(role)
            ids.extend(20 + (ord(char) % 50) for char in body[:12])
            if message["role"] == "assistant":
                ids.append(2 if index + 1 < len(messages) else 3)
        if add_generation_prompt:
            ids.append(12)
        return ids


def test_tokenize_episode_masks_only_assistant_and_drops_end_token() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "r1", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "describe_target", "arguments": "{\"target\": \"T1\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "{}"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "submit_rca", "arguments": "{\"status\": \"insufficient\"}"}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "submit_rca", "content": "accepted"},
    ]
    encoded = tokenize_episode(FakeTokenizer(), messages, tools=[])
    ids, labels = encoded["input_ids"], encoded["labels"]
    assert len(ids) == len(labels)
    supervised = [index for index, label in enumerate(labels) if label != IGNORE_INDEX]
    assert supervised, "assistant tokens must be supervised"
    # every supervised position is inside an assistant span and never the span's end token
    assert all(ids[index] not in (1, 10, 11, 13, 2, 3) for index in supervised)
    assert 12 in {ids[index] for index in supervised}
    with_end = tokenize_episode(FakeTokenizer(), messages, tools=[], supervise_end_token=True)
    assert sum(label != IGNORE_INDEX for label in with_end["labels"]) == len(supervised) + 2


def test_tokenize_episode_requires_assistant_turn() -> None:
    with pytest.raises(ValueError, match="no supervised assistant tokens"):
        tokenize_episode(FakeTokenizer(), [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], tools=[])


def test_template_messages_parses_argument_strings() -> None:
    prepared = _template_messages([{"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "x", "arguments": "{\"a\": 1}"}}]}])
    assert prepared[0]["tool_calls"][0]["function"]["arguments"] == {"a": 1}


def test_case_weights_balance_cases() -> None:
    rows = [{"case_id": "a"}, {"case_id": "a"}, {"case_id": "a"}, {"case_id": "b"}]
    weights = case_weights(rows, balanced=True)
    assert weights == pytest.approx([2 / 3, 2 / 3, 2 / 3, 2.0])
    assert sum(weights) == pytest.approx(len(rows))
    assert case_weights(rows, balanced=False) == [1.0] * 4


def test_tokenize_turns_uses_rollout_context_without_prior_reasoning() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "r1", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "describe_target", "arguments": "{\"target\": \"T1\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "{}"},
        {"role": "assistant", "content": "", "reasoning_content": "r2", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "submit_rca", "arguments": "{\"status\": \"insufficient\"}"}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "submit_rca", "content": "accepted"},
    ]
    examples = tokenize_turns(FakeTokenizer(), messages, tools=[])
    assert len(examples) == 2
    for example in examples:
        supervised = [index for index, label in enumerate(example["labels"]) if label != IGNORE_INDEX]
        assert supervised and supervised[0] > 0
        assert supervised[-1] == len(example["input_ids"]) - 2  # end token excluded
        assert all(example["labels"][index] == example["input_ids"][index] for index in supervised)
    stripped = _strip_prior_reasoning(messages[:4])
    assert "reasoning_content" not in stripped[2] and stripped[3] == messages[3]


def test_rolling_view_truncates_old_observations_only() -> None:
    from rca_lab.mcp.context import rolling_view

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for index in range(12):
        messages.append({"role": "assistant", "content": "", "reasoning_content": "r", "tool_calls": [{"id": f"c{index}", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "name": "x", "content": "o" * 3000})
    view = rolling_view(messages, recent_full=8, old_chars=100)
    tools = [message for message in view if message["role"] == "tool"]
    assert all(len(message["content"]) >= 3000 for message in tools[-8:])
    assert all(len(message["content"]) < 200 for message in tools[:-8])
    assert all("reasoning_content" not in message for message in view)
    assert len(messages[3]["content"]) == 3000  # input untouched


def test_export_clean_messages_drops_off_catalog_turns_and_rewrites_budget():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("mcp_export_sft", Path(__file__).resolve().parents[1] / "scripts" / "mcp_export_sft.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "seed … 도구 호출은 최대 30회. 끝"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "Bash", "content": "<tool_use_error>…</tool_use_error>"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "describe_target", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "describe_target", "content": "ok"},
    ]
    cleaned, dropped = module.clean_messages(messages, {"describe_target", "submit_rca"}, 40)
    assert dropped == 1
    assert [m["role"] for m in cleaned] == ["system", "user", "assistant", "tool"]
    assert "최대 40회" in cleaned[1]["content"] and "30회" not in cleaned[1]["content"]
    assert cleaned[3]["tool_call_id"] == "c2"


def test_tokenize_turns_skips_unsupervised_turn_but_keeps_it_in_context():
    tok = FakeTokenizer()
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    call = lambda i: {"id": f"c{i}", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "r1", "tool_calls": [call(1)], "supervise": False},
        {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "o1"},
        {"role": "assistant", "content": "", "reasoning_content": "r2", "tool_calls": [call(2)]},
    ]
    examples = tokenize_turns(tok, messages, tools)
    assert len(examples) == 1
    prompt_len = sum(1 for x in examples[0]["labels"] if x == IGNORE_INDEX)
    assert prompt_len > 0 and len(examples[0]["input_ids"]) > prompt_len  # context includes the skipped turn


def test_judge_pass_resamples_flagged_turns_and_reports_stubborn_ones(monkeypatch):
    from rca_lab.mcp import rationalize as rz

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "bad", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "describe_target", "arguments": "{\"target\": \"T1\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "o1"},
        {"role": "assistant", "content": "", "reasoning_content": "good", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "sample_logs", "arguments": "{\"target\": \"T1\"}"}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "sample_logs", "content": "o2"},
        {"role": "assistant", "content": "", "reasoning_content": "hopeless", "tool_calls": [{"id": "c3", "type": "function", "function": {"name": "submit_rca", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c3", "name": "submit_rca", "content": "accepted"},
    ]
    samples = {2: iter(["still bad", "fixed"]), 6: iter(["nope", "nope again"])}

    def fake_regenerate(config, out, index, tools, stats, *, seed_offset=0):
        out[index]["reasoning_content"] = next(samples[index])
        return True

    def fake_judge(items):
        return [{"id": it["id"], "consistent": it["reasoning"] in ("good", "fixed"), "grounded": True, "issue": ""} for it in items]

    monkeypatch.setattr(rz, "regenerate_turn", fake_regenerate)
    stats = {"turns": 3}
    rz.judge_pass(rz.RationalizeConfig(endpoint="x", model="m"), messages, [], fake_judge, stats, rounds=2)
    assert stats["judge_rounds"] == [2, 2, 1]  # flagged: {2,6} → {2,6} → {6}
    assert stats["inconsistent"] == [6]
    assert messages[2]["reasoning_content"] == "fixed"
    assert stats["judge_resampled"] == 4


def test_judge_flagged_treats_errors_and_ungrounded_as_flagged():
    from rca_lab.mcp.judge import _extract_json_array, flagged

    verdicts = [{"id": 1, "consistent": True, "grounded": True}, {"id": 2, "consistent": True, "grounded": False}, {"id": 3, "error": "x"}, {"id": 4, "consistent": False, "grounded": True}]
    assert flagged(verdicts) == [2, 3, 4]
    assert _extract_json_array('Here you go:\n```json\n[{"id": 1, "consistent": true}]\n```')[0]["id"] == 1


def test_tokenize_turns_reasoning_weight_applies_before_call_header():
    class HeaderTokenizer(FakeTokenizer):
        """Assistant turns render as: 12 (header) [reasoning digest…] 99 (call header) [call digest…] end."""

        def apply_chat_template(self, messages, *, tokenize, tools=None, add_generation_prompt=False, **_):
            ids = [1]
            for index, message in enumerate(messages):
                role = {"system": 10, "user": 11, "assistant": 12, "tool": 13}[message["role"]]
                ids.append(role)
                if message["role"] == "assistant":
                    ids.extend([31, 32, 33])  # reasoning span
                    ids.append(99)  # <|start|> of the call header
                    ids.extend([41, 42])  # call span
                    ids.append(2 if index + 1 < len(messages) else 3)
                else:
                    ids.extend(20 + (ord(c) % 50) for c in json.dumps(message)[:6])
            if add_generation_prompt:
                ids.append(12)
            return ids

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "", "reasoning_content": "r", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
    ]
    [example] = tokenize_turns(HeaderTokenizer(), messages, [], reasoning_weight=0.3, call_header_id=99)
    supervised = [(tok, w) for tok, lab, w in zip(example["input_ids"], example["labels"], example["token_weight"]) if lab != IGNORE_INDEX]
    assert [w for tok, w in supervised if tok in (31, 32, 33)] == [0.3, 0.3, 0.3]
    assert [w for tok, w in supervised if tok in (99, 41, 42)] == [1.0, 1.0, 1.0]
    [plain] = tokenize_turns(HeaderTokenizer(), messages, [])
    assert set(plain["token_weight"]) == {1.0}


def test_export_unsupervise_wrong_status_marks_only_final_submit_turn():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("mcp_export_sft", Path(__file__).resolve().parents[1] / "scripts" / "mcp_export_sft.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    call = lambda i, name: {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [call(1, "describe_target")]},
        {"role": "tool", "tool_call_id": "c1", "name": "describe_target", "content": "ok"},
        {"role": "assistant", "content": "", "tool_calls": [call(2, "submit_rca")]},
        {"role": "tool", "tool_call_id": "c2", "name": "submit_rca", "content": "accepted"},
    ]
    assert module.unsupervise_wrong_status(messages, {"status_correct": True}) == 0
    assert all("supervise" not in m for m in messages)
    assert module.unsupervise_wrong_status(messages, {"status_correct": False}) == 1
    assert messages[2]["supervise"] is False and "supervise" not in messages[0]


def test_extract_reasoning_accepts_json_control_chars_and_bare_text() -> None:
    from rca_lab.mcp.rationale_writer import _extract_reasoning

    assert _extract_reasoning('{"reasoning": "line one\nline two with T1"}') == "line one\nline two with T1"
    bare = "We have T1 blocking sixteen sessions since 08:47, so the next step is to read the pool gauge."
    assert _extract_reasoning(bare) == bare
    assert _extract_reasoning('{"reasoning": ""}') is None
    assert _extract_reasoning("ok") is None
    prose = "We pinned the onset to T11 at 23:00:01 (log {reconnecting}) so the next call reads the pool gauge for T3."
    assert _extract_reasoning(prose) == prose
    assert _extract_reasoning('{"reasoning": "x", broken') is None


def test_rl_group_advantages_and_example_weight() -> None:
    import importlib.util
    from pathlib import Path

    from rca_lab.train.mcp_sft import rl_example_weight

    spec = importlib.util.spec_from_file_location("mcp_rl_build", Path(__file__).resolve().parents[1] / "scripts" / "mcp_rl_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adv = module.group_advantages([1.0, 0.0, 0.0, 1.0])
    assert adv is not None and abs(sum(adv)) < 1e-9 and adv[0] > 0 > adv[1]
    assert module.group_advantages([0.5, 0.5, 0.5]) is None  # flat group: no signal
    assert module.group_advantages([1.0]) is None
    clipped = module.group_advantages([1.0] + [0.0] * 50, clip=3.0)
    assert clipped is not None and max(clipped) == 3.0
    assert rl_example_weight(2.0, 0.5) == 2.0 and rl_example_weight(-2.0, 0.5) == -1.0


def test_rl_config_requires_advantage_and_no_case_balancing(tmp_path) -> None:
    import json

    import pytest

    from rca_lab.train.mcp_sft import MCPSFTConfig, load_rows

    row = {"case_id": "case-a", "run": "r1", "messages": [{"role": "user", "content": "u"}], "tools": [{"type": "function"}]}
    data = tmp_path / "rl.jsonl"
    data.write_text(json.dumps(row) + "\n", encoding="utf-8")
    base = {"name": "t", "model_name": "m", "tokenizer_name": "t", "dataset": str(data), "expected_examples": 1, "expected_cases": 1,
            "output_dir": str(tmp_path), "wandb": {"entity": "e", "project": "p", "version": "v"}, "method": "rl", "case_balanced_loss": False}
    try:
        config = MCPSFTConfig.model_validate(base)
    except ValueError:  # checkpoint-contract fields are environment specific; the row checks are what matter here
        pytest.skip("training checkpoint contract requires GPU-host fields")
    with pytest.raises(ValueError, match="advantage"):
        load_rows(config)
    data.write_text(json.dumps({**row, "advantage": 0.7}) + "\n", encoding="utf-8")
    assert load_rows(config)[0]["advantage"] == 0.7
    with pytest.raises(ValueError, match="case_balanced_loss"):
        load_rows(MCPSFTConfig.model_validate({**base, "case_balanced_loss": True}))


def test_rl_sample_turns_keeps_final_turn_and_order() -> None:
    from rca_lab.train.mcp_sft import sample_turns

    turns = list(range(35))
    picked = sample_turns(turns, 8, seed="case-a/run1")
    assert len(picked) == 8 and picked[-1] == 34 and picked == sorted(picked)
    assert picked == sample_turns(turns, 8, seed="case-a/run1")  # deterministic per episode
    assert sample_turns(turns[:5], 8, seed="x") == turns[:5] and sample_turns(turns, 0, seed="x") == turns


def test_rl_reward_closes_eval_reward_gaps() -> None:
    from rca_lab.mcp.rl_reward import forced_answer, rl_reward

    ok = {"stop_reason": "submitted", "status": "provisional", "status_correct": True, "root_f1": 1.0, "root_precision": 1.0, "root_recall": 1.0, "ref_grounding": 1.0, "tool_calls": 20}
    assert rl_reward(ok, mechanism=1.0, forced=False) == 1.0
    assert rl_reward(ok, mechanism=0.0, forced=False) == 0.7  # right target, wrong mechanism no longer full marks
    assert rl_reward({**ok, "status": "confirmed", "status_correct": False}, mechanism=1.0, forced=False) == 1.0  # confidence is not graded
    wrong_prov = {**ok, "root_f1": 0.0, "root_precision": 0.0, "root_recall": 0.0, "status_correct": False}
    wrong_conf = {**wrong_prov, "status": "confirmed"}
    assert rl_reward(wrong_conf, mechanism=None, forced=False) == rl_reward(wrong_prov, mechanism=None, forced=False) == 0.0
    # legacy formula (datasets built before 2026-10-01)
    assert rl_reward(ok, mechanism=0.0, forced=False, status_terms=True) == 0.8
    assert rl_reward(wrong_conf, mechanism=None, forced=False, status_terms=True) < rl_reward(wrong_prov, mechanism=None, forced=False, status_terms=True) == 0.0
    assert rl_reward({**ok, "tool_calls": 60}, mechanism=1.0, forced=True) == round(1.0 - 0.10 - 0.05, 6)
    assert rl_reward({**ok, "stop_reason": "max_turns"}, mechanism=1.0, forced=False) == -0.30
    assert forced_answer([{"role": "user", "content": "조사 예산이 끝났다. 지금까지의 관측만으로 submit_rca"}])
    assert not forced_answer([{"role": "user", "content": "seed"}])


def test_rl_build_judge_cache_requires_same_model_and_rubric(tmp_path):
    import argparse
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location("mcp_rl_build", "scripts/mcp_rl_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    args = argparse.Namespace(judge_model="claude-opus-5-5", judge_rubric="v2")
    assert mod._cached_judgement(tmp_path, args) is None
    (tmp_path / "mechanism_judge.json").write_text(_json.dumps({"score": 0.5, "model": "claude-opus-5-5"}))
    assert mod._cached_judgement(tmp_path, args) is None  # pre-rubric entries are v1
    (tmp_path / "mechanism_judge.json").write_text(_json.dumps({"score": 1.0, "model": "claude-opus-5-5", "rubric": "v2"}))
    assert mod._cached_judgement(tmp_path, args)["score"] == 1.0
    assert mod._cached_judgement(tmp_path, argparse.Namespace(judge_model="claude-sonnet-5", judge_rubric="v2")) is None


def test_judge_prompt_versions():
    from rca_lab.mcp.rl_reward import JUDGE_PROMPTS

    assert set(JUDGE_PROMPTS) == {"v1", "v2", "v3"}
    assert "NOT visible" in JUDGE_PROMPTS["v2"]
    # v3 = v2 plus the trigger definition (load is a condition; an unobserved initiator is not required)
    assert JUDGE_PROMPTS["v3"].startswith(JUDGE_PROMPTS["v2"].split("Answer with JSON only:")[0])
    assert "not the trigger" in JUDGE_PROMPTS["v3"] and "initiator" in JUDGE_PROMPTS["v3"]


def test_rl_build_skips_hinted_runs(tmp_path):
    import importlib.util
    import json as _json

    spec = importlib.util.spec_from_file_location("mcp_rl_build_hinted", "scripts/mcp_rl_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for run, hinted in (("run1", False), ("run2", True)):
        d = tmp_path / "case-x-v3-1" / run
        d.mkdir(parents=True)
        (d / "score.json").write_text(_json.dumps({"case_id": "case-x-v3-1", "hinted": hinted, "root_f1": 1.0}))
    groups = mod.load_groups(tmp_path)
    assert [path.name for path, _ in groups["case-x-v3-1"]] == ["run1"]


def test_rl_advantage_normalization_modes() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("mcp_rl_build_norm", "scripts/mcp_rl_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real = [1.0, 0.0, 1.0, 0.0]  # hit/miss split
    noise = [0.30, 0.20, 0.25, 0.20]  # spread 0.10 (judge / call-count noise)
    # original GRPO: the noise group gets advantages as large as the real split
    g_real = module.group_advantages(real, norm="group")
    g_noise = module.group_advantages(noise, norm="group")
    assert max(g_noise) > 0.7 * max(g_real)
    # min-range drops the noise group; batch scaling keeps the real split near unit scale
    assert module.group_advantages(noise, norm="batch", min_range=0.15) is None
    noisy_kept = module.group_advantages([0.40, 0.20, 0.30, 0.20], norm="batch", min_range=0.15)
    scaled_real, scaled_noise = module.scale_advantages([module.group_advantages(real, norm="batch"), noisy_kept], norm="batch")
    assert max(scaled_noise) < 0.3 * max(scaled_real) and 0.8 < max(scaled_real) < 1.3
    # Dr. GRPO keeps reward units
    assert module.scale_advantages([module.group_advantages(real, norm="none")], norm="none") == [[0.5, -0.5, 0.5, -0.5]]


def test_rl_reward_precision_squared_makes_hedging_lose() -> None:
    from rca_lab.mcp.rl_reward import rl_reward

    base = {"stop_reason": "submitted", "status": "provisional", "status_correct": False, "ref_grounding": 1.0, "tool_calls": 20}
    sure = {**base, "root_f1": 1.0, "root_precision": 1.0, "root_recall": 1.0}
    hedge2 = {**base, "root_f1": 2 / 3, "root_precision": 0.5, "root_recall": 1.0}  # one root, right + wrong named
    hedge3 = {**base, "root_f1": 0.5, "root_precision": 1 / 3, "root_recall": 1.0}
    half = {**base, "root_f1": 2 / 3, "root_precision": 1.0, "root_recall": 0.5}  # two roots, one named
    r = lambda s: rl_reward(s, mechanism=1.0, forced=False)
    assert r(sure) == 1.0 and r(half) == 0.425 and r(hedge2) == 0.2125 and round(r(hedge3), 4) == 0.0944
    # two candidates that cover the root 60% of the time beat a 30%-sure single guess under F1, not any more
    f1 = lambda s: rl_reward(s, mechanism=1.0, forced=False, root_term="f1")
    assert 0.6 * f1(hedge2) > 0.3 * f1(sure)
    assert 0.6 * r(hedge2) < 0.3 * r(sure) and 0.9 * r(hedge3) < 0.3 * r(sure)
    import pytest
    with pytest.raises(ValueError):
        rl_reward({**base, "root_f1": 1.0}, mechanism=1.0, forced=False)  # precision/recall required unless legacy f1


def test_reference_override_replaces_meta_and_invalidates_old_verdicts() -> None:
    from pathlib import Path

    from rca_lab.mcp.rl_reward import judge_cache_valid, reference_cause, reference_version

    case = "case-f20-r-v3-caf04820"
    text = reference_cause(Path("/nonexistent") / case)  # the override is used without reading meta.json
    assert "dailyStats" in text and "42P18" in text and "OOM" in text and "must be named" in text
    assert reference_version(case) == "override-2026-10-01b" and reference_version("case-none") == "meta"
    old = {"score": 0.5, "model": "claude-opus-5-5", "rubric": "v2"}  # verdict made against the meta text
    assert not judge_cache_valid(old, case, model="claude-opus-5-5", rubric="v2")
    assert judge_cache_valid({**old, "reference": "override-2026-10-01b"}, case, model="claude-opus-5-5", rubric="v2")
    assert judge_cache_valid(old, "case-other", model="claude-opus-5-5", rubric="v2")


def test_sft_turns_per_episode_keeps_final_turn_and_is_seeded():
    from rca_lab.train.mcp_sft import sample_turns

    turns = list(range(20))
    a = sample_turns(turns, 6, seed="case/run1")
    b = sample_turns(turns, 6, seed="case/run1")
    assert a == b and len(a) == 6 and a[-1] == 19 and a == sorted(a)
    assert sample_turns(turns, 0, seed="x") == turns


def test_length_filter_and_sampling_counts_are_distinct(monkeypatch, capsys):
    from types import SimpleNamespace

    from rca_lab.train import mcp_sft

    turns = [{"input_ids": [1] * length} for length in (1, 2, 3, 30)]
    monkeypatch.setattr(mcp_sft, "tokenize_turns", lambda *args, **kwargs: turns)
    config = SimpleNamespace(
        sequence_mode="turns", method="sft", max_length=10,
        reasoning_strength=None, supervise_end_token=False, reasoning_loss_weight=1.0,
        turns_per_episode=2, case_balanced_loss=False,
    )
    result = mcp_sft.build_examples(config, None, [{"case_id": "case", "run": "run", "messages": [], "tools": []}])
    assert len(result) == 2
    output = capsys.readouterr().out
    assert "dropped 1 turn(s) over max_length 10" in output
    assert "sampled 2 of 3 eligible turns" in output
