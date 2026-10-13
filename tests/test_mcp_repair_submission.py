import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_repair_preserves_source_and_loads_reference_only_after_inference(tmp_path, monkeypatch):
    from rca_lab.mcp import golden, score

    script = Path(__file__).resolve().parents[1] / "scripts/mcp_repair_submission.py"
    spec = importlib.util.spec_from_file_location("repair_submission", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "original"
    source.mkdir()
    original = {"case_id": "case-test", "run": 1, "stop_reason": "forced_answer_rejected",
                "turns": 9, "tool_calls": 31, "tool_errors": 2, "wall_seconds": 1800,
                "output_limit_hits": 0}
    for name, value in {"score.json": original, "alias.json": {}, "observed_refs.json": ["ref"]}.items():
        (source / name).write_text(json.dumps(value))
    (source / "trajectory.jsonl").write_text(json.dumps({"role": "tool", "content": "validation error"}) + "\n")
    (source / "responses.jsonl").write_text('{}\n')
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"endpoint": "http://unused", "model": "test", "guided": False,
                              "max_output_tokens": 32768, "temperature": 1, "top_p": .95,
                              "top_k": 64, "seed": 0, "request_timeout": 3600,
                              "chat_template_kwargs": {"enable_thinking": True}, "contract": "unused"}))
    inference_done = False

    def infer(config, messages, aliases, **kwargs):
        nonlocal inference_done
        assert kwargs == {"retry_budget": 1, "observed_refs": {"ref"}}
        assert config.seed == 1000
        inference_done = True
        return SimpleNamespace(messages=messages, result={"status": "insufficient", "causes": []},
                               turns=1, tool_calls=0, tool_errors=0, stop_reason="submitted",
                               wall_seconds=10, forced_submit_validation_retries=1,
                               output_limit_hits=0, prompt_tokens_last=42000)

    def reference(path):
        assert inference_done
        return {"case-test": object()}

    monkeypatch.setattr(module, "run_forced_submit_repair", infer)
    monkeypatch.setattr(golden, "load_contract", reference)
    monkeypatch.setattr(score, "score_result", lambda *args, **kw: {"turns": kw["turns"], "root_f1": 0})
    destination = tmp_path / "recovery"
    monkeypatch.setattr(sys, "argv", [str(script), str(source), str(destination), "--plan", str(plan)])
    module.main()
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}
    result = json.loads((destination / "score.json").read_text())
    assert result["submission_recovery"] is True
    assert result["turns"] == 10 and result["wall_seconds"] == 1810
    assert json.loads((destination / "recovery-provenance.json").read_text())["comparable_to_unmodified_original"] is False
    with pytest.raises(SystemExit):
        module.main()  # Never overwrite a recovery or original directory.
