import importlib.util
import json
import sys
from pathlib import Path


def load_module():
    script = Path(__file__).resolve().parents[1] / "scripts/benchmark_korean_speed.py"
    spec = importlib.util.spec_from_file_location("benchmark_korean_speed", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sse_parse_collects_reasoning_content_usage_and_finish_reason():
    module = load_module()
    lines = [
        b'data: {"choices":[{"delta":{"reasoning_content":"think "}}]}\n',
        b'data: {"choices":[{"delta":{"content":"answer"}}]}\n',
        b'data: {"choices":[{"finish_reason":"length","delta":{}}]}\n',
        (
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":11,'
            b'"total_tokens":18,"completion_tokens_details":{"reasoning_tokens":5}}}\n'
        ),
        b"data: [DONE]\n",
    ]
    ticks = iter([10.1, 10.4, 10.5, 10.6, 10.7])
    observation = module.parse_sse_lines(lines, start_at=10.0, clock=lambda: next(ticks))

    assert observation.reasoning == "think "
    assert observation.content == "answer"
    assert observation.first_delta_at == 10.1
    assert observation.last_delta_at == 10.4
    assert observation.end_at == 10.7
    assert observation.finish_reason == "length"
    assert observation.usage["completion_tokens"] == 11


def test_summary_reports_api_visible_streaming_rate_and_hashes():
    module = load_module()
    lines = [
        'data: {"choices":[{"delta":{"content":"a"}}]}',
        'data: {"choices":[{"delta":{"content":"b"}}]}',
        'data: {"choices":[{"finish_reason":"stop","delta":{}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":8,"total_tokens":11}}',
    ]
    ticks = iter([1.2, 1.6, 1.7, 1.8])
    observation = module.parse_sse_lines(lines, start_at=1.0, clock=lambda: next(ticks))
    summary = module.summarize_observation(
        observation,
        total_seconds=0.9,
        start_at=1.0,
        request_body={"model": "m", "messages": [{"role": "user", "content": "p"}]},
        before_metrics=(
            "vllm:spec_decode_num_draft_tokens_total{rank=\"0\"} 1e2\n"
            "vllm:spec_decode_num_drafts_total{rank=\"0\"} 10\n"
            "vllm:spec_decode_num_accepted_tokens_total{rank=\"0\"} 40\n"
            "vllm:unrelated_draft_looking_counter 999\n"
        ),
        after_metrics=(
            "vllm:spec_decode_num_draft_tokens_total{rank=\"0\"} 120\n"
            "vllm:spec_decode_num_drafts_total{rank=\"0\"} 14\n"
            "vllm:spec_decode_num_accepted_tokens_total{rank=\"0\"} 50\n"
            "vllm:unrelated_draft_looking_counter 1000\n"
        ),
    )

    assert summary["timing"]["ttft_seconds"] == 0.19999999999999996
    assert summary["timing"]["generation_window_seconds"] == 0.40000000000000013
    assert summary["timing"]["completion_tokens_per_generation_window_second"] == 19.999999999999993
    assert "Approximate API-visible" in summary["timing"]["rate_note"]
    assert summary["usage"]["completion_tokens"] == 8
    assert summary["finish_reason"] == "stop"
    assert summary["hashes"]["content_sha256"] == module.sha256_text("ab")
    assert summary["speculative_metrics"]["available"] is True
    assert summary["speculative_metrics"]["accepted_counter_delta_sum"] == 10
    assert summary["speculative_metrics"]["draft_or_spec_counter_delta_sum"] == 20
    assert summary["speculative_metrics"]["acceptance_ratio_from_named_counters"] == 0.5
    assert summary["speculative_metrics"]["draft_token_counter_delta_sum"] == 20
    assert summary["speculative_metrics"]["draft_step_counter_delta_sum"] == 4
    assert summary["speculative_metrics"]["acceptance_ratio_from_token_counters"] == 0.5
    assert summary["speculative_metrics"]["mean_accepted_tokens_per_draft_step"] == 2.5
    draft_key = 'vllm:spec_decode_num_draft_tokens_total{rank="0"}'
    assert summary["speculative_metrics"]["counter_deltas"][draft_key]["delta"] == 20


def test_manifest_defaults_expect_six_measured_requests_and_per_prompt_warmup(
    tmp_path, monkeypatch
):
    module = load_module()
    script = Path(module.__file__)

    def fake_run_case(args, prompt_case, repeat_index, *, warmup, extra_body):
        return {
            "case_id": prompt_case["id"],
            "warmup": warmup,
            "repeat_index": repeat_index,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 5},
            "timing": {"generation_window_seconds": 2.0, "total_api_seconds": 3.0},
        }

    monkeypatch.setattr(module, "run_case", fake_run_case)
    monkeypatch.setattr(
        "sys.argv",
        [
            str(script),
            "--endpoint",
            "http://127.0.0.1:8000",
            "--model",
            "test-model",
            "--output",
            str(tmp_path),
            "--extra-body",
            '{"chat_template_kwargs":{"enable_thinking":true}}',
        ],
    )

    module.main()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["expected_measured_requests"] == 6
    assert manifest["actual_measured_requests"] == 6
    assert manifest["warmup_requests_excluded"] == 3
    assert manifest["weighted_completion_tokens_per_generation_window_second"] == 10.0


def test_reasoning_total_is_null_when_any_measured_request_lacks_count():
    module = load_module()
    rows = [
        {"usage": {"reasoning_tokens": 7}},
        {"usage": {"reasoning_tokens": None}},
    ]

    assert module.total_reasoning_tokens(rows) is None
    assert module.reported_reasoning_token_count(rows) == 1


def test_zero_reasoning_token_count_with_reasoning_text_is_treated_as_unreported():
    module = load_module()
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"생각"}}]}',
        'data: {"choices":[{"delta":{"content":"답"}}]}',
        (
            'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":8,'
            '"total_tokens":11,"completion_tokens_details":{"reasoning_tokens":0}}}'
        ),
    ]
    ticks = iter([1.2, 1.6, 1.8])
    observation = module.parse_sse_lines(lines, start_at=1.0, clock=lambda: next(ticks))

    summary = module.summarize_observation(
        observation,
        total_seconds=0.9,
        start_at=1.0,
        request_body={"model": "m", "messages": [{"role": "user", "content": "p"}]},
        before_metrics="",
        after_metrics="",
    )

    assert summary["usage"]["reasoning_tokens"] is None
