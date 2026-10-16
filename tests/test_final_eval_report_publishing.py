import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('final_eval_report', 'scripts/build_all_eval_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_grouped_plan_resolves_authoritative_base_conditions():
    for run_id in report.GEMMA_BUDGET_RUNS:
        path = report.evaluation_plan_path(run_id)
        plan = json.loads(path.read_text())
        assert plan['aggregate_root'] == f'outputs/mcp-eval/{run_id}'
        assert plan['max_output_tokens'] == 32768
        assert plan['transport']['max_model_len'] == 131072
        assert plan['transport']['num_speculative_tokens'] == 4


def test_summary_uses_final_rows_not_historical_scores():
    assert 'gemmaBaseRows=D.runs.filter' in report.HTML
    assert '${rate(r.root_hit,r.n)}' in report.HTML
    assert '기존셋 원인 9/24' not in report.HTML
    assert '<td>65,536토큰</td>' not in report.HTML
    assert 'function enableTableSorting(table)' in report.HTML
    for column in ['풀이 처리속도 (tok/s)', '초/턴', '초/시나리오', '평균 생성 토큰/시나리오']:
        assert column in report.HTML


def test_qad_row_requires_selected_deployment_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(report, 'ROOT', tmp_path)
    selection = tmp_path / 'outputs/training/selected-checkpoint.json'
    selection.parent.mkdir(parents=True)
    monkeypatch.setattr(report, 'GEMMA_MECHANISM_SELECTION', selection)
    selection.write_text(json.dumps({'selected_step': 150, 'adapter_sha256': 'selected'}))
    run = next(iter(report.GEMMA_MECHANISM_RUNS))
    work = tmp_path / 'outputs' / run
    (work / 'serving').mkdir(parents=True)
    (work / 'state.json').write_text(json.dumps({'phase': 'complete', 'judge_exit': 0, 'reports': [{'total':24,'unresolved_hits':0}]}))
    artifact = {'export_verified':True, 'adapter_model_sha256':'selected','model_dir':'/models/qad'}
    deployment = {'method':'qad_fp8','step':150,'model_dir':'/models/qad','artifact_manifest':artifact}
    (selection.parent / 'selected-deployment.json').write_text(json.dumps(deployment))
    marker = {'status':'success','artifact_identity':{'step':150,'adapter_model_sha256':'selected','model_dir':'/models/qad'}}
    (work / 'serving/smoke-success.json').write_text(json.dumps(marker))
    assert report.accepted_mechanism_intervention(run)
    marker['artifact_identity']['model_dir'] = '/models/old-ptq'
    (work / 'serving/smoke-success.json').write_text(json.dumps(marker))
    assert not report.accepted_mechanism_intervention(run)
