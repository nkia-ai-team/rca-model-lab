import importlib.util
import json
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('recover_context',ROOT/'outputs/gemma-mechanism-v2-20261014/recover_context_failure.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def fixture(tmp_path):
    case='case-test';work=tmp_path
    (work/'plan.json').write_text(json.dumps({'observation_policy':'legacy','cases':[case],'runs_per_case':2,'max_output_tokens':32768,'model':'test'}))
    directory=work/'shards/opus12-00'/case/'run2';directory.mkdir(parents=True)
    (directory/'responses.jsonl').write_text(json.dumps({'turn':1,'finish_reason':'length','usage':{'prompt_tokens':100,'completion_tokens':32768}})+'\n')
    error={'type':'BadRequestError','param':'input_tokens','code':400,'message':'maximum context length reached'}
    (work/'opus12-00.log').write_text('[2026-10-14T16:00:00Z] case-test run2: student start\n[2026-10-14T16:00:10Z] case-test: FAILED chat completion HTTP 400: '+json.dumps({'error':error})+'\n')
    (work/'opus12-00.command.json').write_text(json.dumps(['python','case_runner']))
    events=[{'active_case':case,'utc':f'2026-10-14T16:00:0{i}+00:00','request_seed':2000+i,'status':400,'requested_max_tokens':32768,'error':error} for i in [2,3]]
    (work/'api-telemetry.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    return work,case,directory,events


def test_recovery_keeps_denominator_without_fabricating_trajectory(tmp_path):
    work,case,directory,events=fixture(tmp_path)
    _,score,evidence=module.recover(work,case,2)
    assert score['root_f1']==0 and score['strict_correct'] is False
    assert score['turns']==3 and score['tool_calls'] is None
    assert 'unscored' not in score
    assert not (directory/'trajectory.jsonl').exists()
    assert evidence['terminal_events']==events
    assert all(x['byte_length']>0 for x in evidence['source_snapshots'].values())
    assert not (directory/'score.json').exists()  # pure validation, no mutation


@pytest.mark.parametrize('change',['status','budget','missing_event','already_scored'])
def test_recovery_refuses_unproven_or_previously_scored_failure(tmp_path,change):
    work,case,directory,events=fixture(tmp_path)
    if change=='status':events[-1]['status']=500
    elif change=='budget':events[-1]['requested_max_tokens']=4096
    elif change=='missing_event':events.pop()
    else:(directory/'score.json').write_text('{}')
    (work/'api-telemetry.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
    with pytest.raises(ValueError):module.recover(work,case,2)


def test_summary_retains_failure_without_inventing_missing_tool_counts(tmp_path):
    import ast
    source=(ROOT/'scripts/mcp_case_run.py').read_text()
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='write_summary')
    scope={'Path':Path,'Any':object,'json':json}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'summary','exec'),scope)
    base={'case_id':'case-test','run':1,'stop_reason':'submitted','root_f1':1,'strict_correct':True,'tool_calls':4}
    failure={**base,'run':2,'stop_reason':'context_budget_exhausted','root_f1':0,'strict_correct':False,'tool_calls':None}
    scope['write_summary'](tmp_path,[base,failure])
    result=json.loads((tmp_path/'summary.json').read_text())
    assert result['scored_runs']==2 and result['strict_correct_runs']==1
    assert result['mean_tool_calls']==4 and result['tool_calls_coverage']==1
    scope['write_summary'](tmp_path,[failure])
    assert json.loads((tmp_path/'summary.json').read_text())['mean_tool_calls'] is None
    assert 'n/a' in (tmp_path/'summary.md').read_text()
