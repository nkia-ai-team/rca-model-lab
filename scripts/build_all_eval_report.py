"""Build a read-only, self-contained evaluation archive from allowlisted metrics."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from functools import cache, lru_cache
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import rca_lab.mcp.rl_reward as reward_module
from rca_lab.mcp.rl_reward import judge_cache_path, judge_cache_valid

reward_module.reference_version = lru_cache(maxsize=None)(reward_module.reference_version)

OUT = ROOT / 'outputs/reports/all-evaluations-20261013'
GEMMA_FINETUNED_RUNS = {
    'gemma4-ft-polestar43-legacy60-opus12-20261013': 'gemma4-kt-out32k-legacy60-opus12-20261013',
    'sealed-gemma4-ft-polestar43-legacy60-20261013': 'sealed-gemma4-kt-out32k-legacy60-20261013',
}

GEMMA_MECHANISM_RUNS = {
    'gemma4-ft-mechanism-v2-legacy60-opus12-20261014': 'gemma4-kt-out32k-legacy60-opus12-20261013',
    'sealed-gemma4-ft-mechanism-v2-legacy60-20261014': 'sealed-gemma4-kt-out32k-legacy60-20261013',
}
GEMMA_MECHANISM_SELECTION = ROOT / 'outputs/gemma-mechanism-v2-20261014/selected-checkpoint.json'

QWEN_FLASH_RUNS = {
    'qwen-flash-next-kt-mtp4-out32k-legacy60-opus12-20261014': 'gemma4-legacy60-opus12-20261013',
    'sealed-qwen-flash-next-kt-mtp4-out32k-legacy60-20261014': 'sealed-gemma4-legacy60-20261013',
}
GEMMA_BUDGET_RUNS = {
    'gemma4-kt-out32k-legacy60-opus12-20261013': 'gemma4-legacy60-opus12-20261013',
    'sealed-gemma4-kt-out32k-legacy60-20261013': 'sealed-gemma4-legacy60-20261013',
}
COMPLETE_COMPARISON_RUNS = {**GEMMA_FINETUNED_RUNS, **GEMMA_MECHANISM_RUNS, **QWEN_FLASH_RUNS, **GEMMA_BUDGET_RUNS}


def accepted_fixed_budget_state_file(path, expected=24):
    """Accept a fully judged fixed-32K Gemma run without hiding cutoffs.

    `budget_limited` is model behavior under the chosen output limit, not an
    infrastructure failure, if all expected attempts were scored and judged.
    Older base-run states do not always write telemetry_complete fields, so
    those are enforced when present instead of required for all historical runs.
    """
    state = read(path) or {}
    reports = state.get('reports') or []
    report = reports[0] if reports and isinstance(reports[0], dict) else {}
    if report.get('total') != expected or report.get('unresolved_hits') != 0 or report.get('pending_hits', 0) != 0:
        return False
    phase = state.get('phase')
    if phase == 'complete':
        return True
    if phase != 'budget_limited':
        return False
    metrics = state.get('metrics') or {}
    if metrics.get('completed') != expected or metrics.get('expected') != expected:
        return False
    if state.get('judge_exit') != 0:
        return False
    audit = state.get('output_budget_audit') or {}
    if audit.get('telemetry_complete') is False or audit.get('unknown_finish_events', 0) != 0:
        return False
    return True

def accepted_mechanism_intervention(run_id):
    """Publish only fully graded test runs of the validation-selected artifact."""
    work = ROOT / 'outputs' / run_id
    if not accepted_fixed_budget_state_file(work / 'state.json', expected=24):
        return False
    state = read(work / 'state.json') or {}
    report = (state.get('reports') or [{}])[0]
    if state.get('judge_exit') != 0 or report.get('invalid_cache_hits', 0) != 0:
        return False
    selection = read(GEMMA_MECHANISM_SELECTION) or {}
    deployment = read(GEMMA_MECHANISM_SELECTION.parent / 'selected-deployment.json') or {}
    artifact = deployment.get('artifact_manifest') or {}
    marker = read(work / 'serving/smoke-success.json') or {}
    identity = marker.get('artifact_identity') or {}
    return (
        type(selection.get('selected_step')) is int
        and bool(selection.get('adapter_sha256'))
        and deployment.get('method') == 'qad_fp8'
        and deployment.get('step') == selection['selected_step']
        and bool(deployment.get('model_dir'))
        and artifact.get('export_verified') is True
        and artifact.get('adapter_model_sha256') == selection['adapter_sha256']
        and artifact.get('model_dir') == deployment['model_dir']
        and marker.get('status') == 'success'
        and identity.get('step') == selection['selected_step']
        and identity.get('adapter_model_sha256') == selection['adapter_sha256']
        and identity.get('model_dir') == deployment['model_dir']
    )


CONFIG_KEYS = ('max_turns', 'max_prompt_tokens', 'max_output_tokens', 'thinking',
               'reasoning_strength', 'thinking_token_budget', 'observation_policy',
               'temperature', 'top_p', 'top_k', 'seed', 'speculative_method',
               'num_speculative_tokens', 'speculative_decoding', 'hinted', 'guided',
               'tool_result_max_chars', 'runs_per_case', 'expected', 'repo_id', 'revision', 'transport', 'concurrency')


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def numeric(x):
    return type(x) in (int, float) and math.isfinite(x)


def jsonl(path):
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    yield item
            except ValueError:
                yield {'_invalid': True}


def verdict(path, score, provider, model):
    if score.get('unscored'):
        return None
    if score.get('root_f1') != 1:
        return 0
    item = read(judge_cache_path(path.parent, provider, model=model))
    if (isinstance(item, dict) and numeric(item.get('score')) and item['score'] in (0, .5, 1)
            and judge_cache_valid(item, score.get('case_id', ''), model=model, rubric='v2', provider=provider)):
        return int(item['score'] == 1)
    return None


def episode(path):
    score = read(path)
    if not isinstance(score, dict):
        return {'path': str(path.relative_to(ROOT)), 'corrupt': True}
    fields = ('case_id', 'run', 'runner', 'model', 'stop_reason', 'wall_seconds', 'turns',
              'tool_calls', 'tool_errors', 'root_f1', 'unscored', 'hinted',
              'observation_policy', 'max_output_tokens', 'output_limit_hits', 'thinking_token_budget')
    ep = {k: score[k] for k in fields if k in score}
    ep['path'] = str(path.relative_to(ROOT))
    ep['strict_codex'] = verdict(path, score, 'codex', 'gpt-6.1-sol')
    ep['strict_claude'] = verdict(path, score, 'claude', 'claude-opus-5-5')
    ep['model_turns'] = score.get('turns') if score.get('runner') == 'student' else None
    ep['turn_source'] = 'student model rounds' if ep['model_turns'] is not None else 'unmeasured'
    responses = list(jsonl(path.parent / 'responses.jsonl'))
    usage = [x.get('usage', {}).get('completion_tokens') for x in responses]
    ep['output_tokens'] = None
    ep['token_source'] = 'unmeasured'
    if responses and len(responses) == ep['model_turns'] and all(numeric(x) and x >= 0 for x in usage):
        ep['output_tokens'] = sum(usage)
        ep['token_source'] = 'responses completion_tokens (all model rounds)'
    if score.get('runner') != 'student':
        results = [x for x in jsonl(path.parent / 'stream.jsonl') if x.get('type') == 'result']
        if len(results) == 1:
            result = results[0]
            ep['model_turns'] = result.get('num_turns')
            ep['turn_source'] = 'CLI provider num_turns'
            tokens = result.get('usage', {}).get('output_tokens')
            if numeric(tokens) and tokens >= 0:
                ep['output_tokens'] = tokens
                ep['token_source'] = 'CLI final result usage.output_tokens'
    wall = ep.get('wall_seconds')
    ep['e2e_tps'] = ep['output_tokens'] / wall if numeric(wall) and wall > 0 and ep['output_tokens'] is not None else None
    turns = ep.get('model_turns')
    ep['sec_per_turn'] = wall / turns if numeric(wall) and numeric(turns) and turns > 0 else None
    return ep


def category(name):
    if any(x in name for x in ('smoke', 'probe', 'verify', 'diagnostic', 'gapcheck', 'leak-scan', 'order-check')):
        return '진단·스모크'
    if name.startswith(('rl-', 'rollouts-', 'golden-', 'aux-')) or ('teacher' in name and not name.startswith('ceiling-')):
        return '교사·학습 생성'
    if name.startswith(('rcae-', 'codex-actions-')):
        return '교사·학습 생성'
    return '평가'


def hardware_groups(name, episodes):
    if name != 'sealed-qwen-corrected-20261012':
        return []
    groups = []
    for hardware, subset in [('L40S', [e for e in episodes if e.get('case_id')=='case-f04-h-v3-af25dea6']), ('H200', [e for e in episodes if e.get('case_id')!='case-f04-h-v3-af25dea6'])]:
        timed = [e for e in subset if numeric(e.get('wall_seconds'))]
        turns = [e for e in timed if numeric(e.get('model_turns')) and e['model_turns'] > 0]
        tokens = [e for e in timed if e.get('output_tokens') is not None]
        groups.append({'hardware':hardware,'n':len(subset),'mean_seconds':mean(e['wall_seconds'] for e in timed) if timed else None, 'sec_per_turn':sum(e['wall_seconds'] for e in turns)/sum(e['model_turns'] for e in turns) if turns else None, 'e2e_tps':sum(e['output_tokens'] for e in tokens)/sum(e['wall_seconds'] for e in tokens) if tokens and len(tokens)==len(subset) else None})
    return groups


@cache
def canonical_cases(name):
    return tuple(sorted({p.parent.parent.name for p in (ROOT/'outputs/mcp-eval'/name).glob('case-*/run*/score.json')}))


def cohort_name(cases):
    ids = tuple(cases)
    if ids == canonical_cases('opus55-structured-v2-opus12-20261008'):
        return '기존 12 시나리오'
    if ids == canonical_cases('sealed-opus55-legacy60-20261012'):
        return '봉인 12 시나리오'
    return '별도 시나리오 집합'


def evaluation_plan_path(run_id):
    """Resolve explicitly grouped workspaces without inferring their experiment settings."""
    grouped = {
        **{name: 'gemma-base-kt-out32k-20261013' for name in GEMMA_BUDGET_RUNS},
        **{name: 'qwen-flash-next-kt-mtp4-out32k-legacy60-20261014' for name in QWEN_FLASH_RUNS},
    }
    if run_id in grouped:
        return ROOT / 'outputs' / grouped[run_id] / ('sealed' if run_id.startswith('sealed-') else 'original') / 'plan.json'
    return ROOT / 'outputs' / run_id / 'plan.json'


def aggregate(root):
    eps = [episode(p) for p in sorted(root.glob('case-*/run*/score.json'))]
    valid = [x for x in eps if not x.get('corrupt')]
    scored = [x for x in valid if not x.get('unscored')]
    cases = sorted({x['case_id'] for x in valid if x.get('case_id')})
    wall = [x for x in scored if numeric(x.get('wall_seconds')) and x['wall_seconds'] >= 0]
    turns = [x for x in wall if numeric(x.get('model_turns')) and x['model_turns'] > 0]
    tps = [x for x in wall if x.get('e2e_tps') is not None]
    config = {}
    p = evaluation_plan_path(root.name)
    plan = read(p)
    if isinstance(plan, dict):
        config = {k: plan[k] for k in CONFIG_KEYS if k in plan}
    for key in CONFIG_KEYS:
        values = sorted({json.dumps(x[key], ensure_ascii=False) for x in valid if key in x})
        if values:
            config['observed_' + key] = [json.loads(x) for x in values]
    def judge(key):
        passed = sum(x.get(key) == 1 for x in scored)
        pending = sum(x.get(key) is None for x in scored)
        return {'passed': passed, 'pending': pending, 'strict': passed if not pending else None}
    counts = Counter(x.get('stop_reason', 'unknown') for x in valid)
    return {'run_id': root.name, 'category': category(root.name), 'models': sorted({x.get('model', 'unknown') for x in valid}),
            'cases': cases, 'cohort': hashlib.sha256('\n'.join(cases).encode()).hexdigest()[:12],
            'cohort_name': cohort_name(cases), 'case_count': len(cases), 'n': len(scored), 'score_files': len(eps), 'unscored': len(valid)-len(scored),
            'corrupt': len(eps)-len(valid), 'root_hit': sum(x.get('root_f1') == 1 for x in scored),
            'codex': judge('strict_codex'), 'claude': judge('strict_claude'), 'stop_reasons': dict(counts),
            'submitted': counts['submitted'], 'mean_seconds': mean([x['wall_seconds'] for x in wall]) if wall else None,
            'wall_coverage': len(wall), 'mean_turns': mean([x['model_turns'] for x in turns]) if turns else None,
            'sec_per_turn': sum(x['wall_seconds'] for x in turns)/sum(x['model_turns'] for x in turns) if turns else None,
            'turn_coverage': len(turns), 'token_coverage': len(tps),
            'e2e_tps': sum(x['output_tokens'] for x in tps)/sum(x['wall_seconds'] for x in tps) if tps and len(tps)==len(scored) else None,
            'hardware_groups': hardware_groups(root.name, scored), 'config': config, 'config_source': str(p.relative_to(ROOT)) if plan else None,
            'episodes': eps}



def model_label(model, name):
    text = (str(model) + ' ' + name).lower()
    if 'sonnet' in text:
        return 'Sonnet 5.5'
    if 'opus' in str(model).lower() or name.startswith('ablation-'):
        return 'Opus 5.5' if '5-5' in str(model) or '55' in text or name.startswith('ablation-') else 'Opus 5'
    if name in GEMMA_MECHANISM_RUNS:
        return '젬마 기전 강화 파인튜닝'
    if name in GEMMA_FINETUNED_RUNS:
        return 'Gemma 파인튜닝'
    if 'gemma' in text:
        return '기본 Gemma 모델'
    if 'qwen' in text and 'flash' in text and 'next' in text:
        return '기본 Qwen Flash Next 모델'
    if 'qwen' in text:
        return '기본 Qwen 모델'
    if any(t in text for t in ('checkpoint', 'ckpt', 'adapter', 'joint-baseline', 'runtime-family', 'rl-', 'sft-', 'sdft-')):
        return '뮤즈 파인튜닝'
    return '기본 뮤즈 모델'


def checkpoint_label(model):
    text = str(model)
    match = re.search(r'(v[0-9]+[a-z0-9]*)[^/]*?(?:checkpoint-|ckpt)([0-9]+)', text)
    if match:
        return f'{match[1]} · checkpoint {match[2]}'
    if 'joint-baseline' in text:
        return 'joint baseline · 최종'
    version = re.search(r'v[0-9]+[a-z0-9]*', text)
    if version:
        return version[0]
    return text.removeprefix('mcp-') if text.startswith(('mcp-', 'rl-')) else ''


def complete_evaluation(r):
    name = r['run_id']
    if name in COMPLETE_COMPARISON_RUNS:
        reference = canonical_cases(COMPLETE_COMPARISON_RUNS[name])
        repeats = Counter(e.get('case_id') for e in r['episodes'] if not e.get('unscored'))
        if (len(reference) != 12 or tuple(sorted(r['cases'])) != reference
                or r['n'] != 24 or r['score_files'] != 24
                or r['codex']['pending'] != 0 or r['codex']['strict'] is None
                or any(repeats.get(case) != 2 for case in reference)):
            return False
    if name == 'gemma4-legacy60-opus12-20261013' and (r['n'] != 24 or r['codex']['pending'] != 0):
        return False
    if r['category'] != '평가' or r['unscored'] or r['corrupt'] or r['submitted'] == 0:
        return False
    if any(t in name for t in ('tainted', 'crash', 'smoke', 'probe', 'diagnostic', 'teacher', 'ladder-', 'submit-recovery', 'f05h-k8sfix')):
        return False
    if name.startswith('muse-ablation-'):
        return False
    expected = r['config'].get('expected')
    if expected is None and name.startswith(('val36', 'sealed-')):
        expected = 36 if name.startswith('val36') or name in ('sealed-base', 'sealed-mcp-v1i-checkpoint-40', 'sealed-mcp-v1j-checkpoint-40') else None
    if numeric(expected):
        return r['n'] >= expected
    repeats = {}
    for e in r['episodes']:
        repeats.setdefault(e.get('case_id'), set()).add(e.get('run'))
    return r['case_count'] >= 4 and r['n'] >= 12 and len({tuple(sorted(v)) for v in repeats.values()}) == 1


def best_finetuned_by_dataset(runs):
    """Select a single measured mechanism winner for each exact case-ID set."""
    groups = {}
    for row in runs:
        if row['display_model'] == '뮤즈 파인튜닝':
            groups.setdefault(row['cohort'], []).append(row)
    winners = set()
    for candidates in groups.values():
        # Compare like judge protocols; prefer the current judge when available.
        for provider in ('codex', 'claude'):
            measured = [r for r in candidates if r['n'] and r[provider]['strict'] is not None]
            if not measured:
                continue
            winner = max(measured, key=lambda r: (
                r[provider]['strict'] / r['n'], r['root_hit'] / r['n'],
                r['n'], r['run_id']))
            winner['selection_note'] = '데이터셋별 원인+기전 최고 · ' + (
                'gpt-6.1-sol / v2' if provider == 'codex' else 'Claude Opus / v2')
            winners.add(winner['run_id'])
            break
    return [r for r in runs if r['display_model'] != '뮤즈 파인튜닝' or r['run_id'] in winners]


def curated_view(source):
    data = copy.deepcopy(source)
    data.pop('empty_directories', None)
    data['runs'] = [r for r in data['runs'] if complete_evaluation(r)]
    for r in data['runs']:
        r['display_model'] = model_label(r['models'][0] if r['models'] else '', r['run_id'])
        r['checkpoint'] = checkpoint_label(r['models'][0]) if r['display_model']=='뮤즈 파인튜닝' else ''
        r['cohort_name'] = cohort_name(r['cases'])
        r['hardware_groups'] = hardware_groups(r['run_id'], r['episodes'])
        config = r['config']
        known = {'ablation-harness':'초기 하네스', 'ablation-harness-k8s':'K8s 변경 조회 추가', 'ablation-harness-final':'최종 legacy · CLI', 'ablation-raw':'하네스 없음 · CLI', 'muse-assistant-harness-20261008':'legacy · assistant 초기 설정'}
        r['condition_label'] = known.get(r['run_id'], ', '.join(config.get('observed_observation_policy') or [config.get('observation_policy', '이전 평가 하네스')]))
        if 'thinking' in config:
            r['condition_label'] += ' · thinking ' + ('ON' if config['thinking'] else 'OFF')
        if 'thinking' not in config and r['run_id'] == 'gemma4-fp8-opus12-20261008':
            r['condition_label'] += ' · thinking OFF'
        elif 'thinking' not in config and 'gemma4-fp8-thinking-' in r['run_id']:
            r['condition_label'] += ' · thinking ON'
        if r['run_id'].startswith('base-val-'):
            r['condition_label'] += ' · ' + r['run_id'].removeprefix('base-val-')
        if 'submission-resumed' in r['run_id']:
            r['condition_label'] += ' · 제출 복구 조건'
    data['runs'] = best_finetuned_by_dataset(data['runs'])
    legacy = []
    for r in data.get('legacy', []):
        name = r['name']
        if not numeric(r.get('expected_runs')) or not r['expected_runs'] or (r.get('root_scored_runs') or 0) < r['expected_runs']:
            continue
        if any(t in name for t in ('gate', 'smoke', 'probe', 'monitor', 'teacher', 'diagnostic')):
            continue
        r['display_model'] = model_label(r.get('model'), name)
        r['checkpoint'] = checkpoint_label(r.get('model'))
        # Old strict means grounding, so these cannot establish a mechanism winner.
        if r['display_model'] != '뮤즈 파인튜닝':
            legacy.append(r)
    data['legacy'] = legacy
    return data


def api_telemetry_metrics(row):
    """Expose request-wall TPS only when telemetry matches every recorded model round."""
    path = ROOT / 'outputs' / row['run_id'] / 'api-telemetry.jsonl'
    records = [r for r in jsonl(path) if r.get('active_case') in row['cases']]
    episodes = [e for e in row['episodes'] if not e.get('unscored')]
    if not records or not episodes:
        return None
    if any(not numeric(e.get('output_tokens')) or not numeric(e.get('model_turns')) for e in episodes):
        return None
    if any(r.get('status') != 200 or not numeric(r.get('api_elapsed_seconds'))
           or r['api_elapsed_seconds'] <= 0
           or not numeric(r.get('usage', {}).get('completion_tokens')) for r in records):
        return None
    for case in row['cases']:
        actual = [r for r in records if r['active_case'] == case]
        expected = [e for e in episodes if e['case_id'] == case]
        if (len(actual) != sum(e['model_turns'] for e in expected)
                or sum(r['usage']['completion_tokens'] for r in actual)
                != sum(e['output_tokens'] for e in expected)):
            return None
    seconds = sum(r['api_elapsed_seconds'] for r in records)
    return {'tps': sum(e['output_tokens'] for e in episodes) / seconds,
            'request_count': len(records), 'seconds': seconds,
            'source': str(path.relative_to(ROOT))}


def final_harness_view(data):
    # Final comparison uses the user's legacy protocol, not structured-v2.
    final_runs = {
        'ablation-harness-final',
        'sonnet55-legacy-opus12-20261012',
        'muse-assistant-legacy-ab-opus12-20261008',
        'qwen38-thinking-repaired-opus12-20261008',
        'gemma4-fp8-thinking-opus12-20261008',
        'kt-v2bs94-opus12-20261008',
        'sealed-opus55-legacy60-20261012',
        'sealed-sonnet55-legacy60-20261012',
        'sealed-qwen-corrected-20261012',
        'kt-v2bs94-sealed-20261008',
        'sealed-base',
        'sealed-gemma4-legacy60-20261013',
        'gemma4-legacy60-opus12-20261013',
    }
    final_runs.update(COMPLETE_COMPARISON_RUNS)
    data['runs'] = [r for r in data['runs'] if r['run_id'] in final_runs]
    # Publish Flash only after the fixed-budget cohort and mechanism judging finish.
    flash_work = ROOT / 'outputs/qwen-flash-next-kt-mtp4-out32k-legacy60-20261014'
    data['runs'] = [r for r in data['runs'] if r['run_id'] not in QWEN_FLASH_RUNS
                    or accepted_fixed_budget_state_file(
                        flash_work / ('sealed' if r['run_id'].startswith('sealed-') else 'original') / 'state.json',
                        expected=24)]
    gemma_work = ROOT / 'outputs/gemma-base-kt-out32k-20261013'
    data['runs'] = [r for r in data['runs'] if r['run_id'] not in GEMMA_BUDGET_RUNS
                    or accepted_fixed_budget_state_file(
                        gemma_work / ('sealed' if r['run_id'].startswith('sealed-') else 'original') / 'state.json',
                        expected=24)]
    data['runs'] = [r for r in data['runs'] if r['run_id'] not in GEMMA_FINETUNED_RUNS
                    or accepted_fixed_budget_state_file(ROOT / 'outputs' / r['run_id'] / 'state.json', expected=24)]
    data['runs'] = [r for r in data['runs'] if r['run_id'] not in GEMMA_MECHANISM_RUNS
                    or accepted_mechanism_intervention(r['run_id'])]
    replacements = {GEMMA_BUDGET_RUNS[r['run_id']] for r in data['runs']
                    if r['run_id'] in GEMMA_BUDGET_RUNS}
    data['runs'] = [r for r in data['runs'] if r['run_id'] not in replacements]
    rerun_complete = any(r['run_id'] in ('gemma4-legacy60-opus12-20261013',
                                        'gemma4-kt-out32k-legacy60-opus12-20261013') for r in data['runs'])
    if rerun_complete:
        data['runs'] = [r for r in data['runs'] if r['run_id'] != 'gemma4-fp8-thinking-opus12-20261008']
    data['gemma_original_rerun_complete'] = rerun_complete
    data['legacy'] = []
    data['historical'] = {}
    hardware = {
        'ablation-harness-final': 'Claude 외부 서비스 · CLI',
        'sonnet55-legacy-opus12-20261012': 'Claude API',
        'sealed-opus55-legacy60-20261012': 'Claude API',
        'sealed-sonnet55-legacy60-20261012': 'Claude API',
        'muse-assistant-legacy-ab-opus12-20261008': '104 · L40S',
        'gemma4-fp8-thinking-opus12-20261008': '104 · L40S',
        'sealed-gemma4-legacy60-20261013': '104 · L40S GPU2 · MTP 4 · 동시 1',
        'gemma4-legacy60-opus12-20261013': '104 · L40S GPU2 · MTP 4 · 동시 1',
        'qwen38-thinking-repaired-opus12-20261008': 'KT · H200 (eager ON, MTP 1, 동시 2)',
        'sealed-qwen-corrected-20261012': 'L40S 2회 + H200 22회 혼합',
        'kt-v2bs94-opus12-20261008': 'KT · H200',
        'kt-v2bs94-sealed-20261008': 'KT · H200',
    }
    hardware.update({name: 'KT · H200 GPU0 · MTP 4 · 동시 1' for name in GEMMA_BUDGET_RUNS})
    recount = read(OUT/'token-recount.json') or {}
    recounts = {r['run_id']:r for r in recount.get('runs', [])}
    for row in data['runs']:
        if row['run_id'] == 'gemma4-fp8-thinking-opus12-20261008':
            row['condition_label'] = 'legacy · thinking ON · 이전 조건 결과 (동일 조건 재평가 결과 대기)'
        elif row['run_id'] in ('sealed-gemma4-legacy60-20261013', 'gemma4-legacy60-opus12-20261013'):
            row['condition_label'] = 'legacy · thinking ON · 60턴 · 프롬프트 예산 제한 없음 · MTP 4'
        if row['run_id'] in GEMMA_FINETUNED_RUNS or row['run_id'] in GEMMA_MECHANISM_RUNS:
            selection_path = (GEMMA_MECHANISM_SELECTION if row['run_id'] in GEMMA_MECHANISM_RUNS
                              else ROOT / 'outputs/gemma-polestar-20261013/selected-checkpoint.json')
            selection = read(selection_path) or {}
            row['checkpoint_selection'] = {key: selection[key] for key in (
                'selected_step', 'selected_path', 'adapter_sha256', 'validation_run',
                'validation_strict_correct', 'validation_root_correct', 'validation_total',
                'candidate_steps') if key in selection}
            row['checkpoint_selection_source'] = str(selection_path.relative_to(ROOT))
            step = selection.get('selected_step')
            row['checkpoint'] = f'checkpoint {step} · 검증셋 선정' if type(step) is int else '검증셋 선정 체크포인트'

        if row['run_id'] in GEMMA_FINETUNED_RUNS or row['run_id'] in GEMMA_MECHANISM_RUNS:
            row['condition_label'] = 'legacy · thinking ON · 병합 후 FP8 · 60턴 · 컨텍스트 131,072 · 출력 한도 32,768 · MTP 4'
        if row['run_id'] in GEMMA_MECHANISM_RUNS:
            # Admission above verified QAD export and selected adapter identity.
            row['condition_label'] = row['condition_label'].replace('병합 후 FP8', '기전 강화 · QAD 후 FP8')
        if row['run_id'] in GEMMA_BUDGET_RUNS:
            row['condition_label'] = 'legacy · thinking ON · 60턴 · 컨텍스트 131,072 · 출력 한도 32,768 · MTP 4'
        if row['run_id'] in QWEN_FLASH_RUNS:
            row['condition_label'] = 'legacy · thinking ON (xhigh) · INT4 W4A16 AutoRound · vLLM · 공식 MTP 4 · PLE CPU offload · 컨텍스트 131,072 · 출력 한도 32,768'
        row['api_telemetry'] = api_telemetry_metrics(row)
        row['api_tps'] = row['api_telemetry']['tps'] if row['api_telemetry'] else None
        row['token_recount'] = recounts.get(row['run_id'])
        measured = [e for e in row['episodes'] if not e.get('unscored') and numeric(e.get('output_tokens'))]
        row['output_token_coverage'] = len(measured)
        row['mean_output_tokens'] = (sum(e['output_tokens'] for e in measured) / row['n']
                                     if row['n'] and len(measured) == row['n'] else None)
        row['hardware_label'] = hardware.get(row['run_id'], '장비 기록 미확인')
        if row['run_id'] in COMPLETE_COMPARISON_RUNS:
            row['hardware_label'] = row['config'].get('transport', {}).get('gpu', '장비 기록 미확인')
    data['final_harness'] = 'legacy'
    data['generated'] = datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds')
    return data


def write_view(source):
    data = final_harness_view(curated_view(source))
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
    (OUT/'data.json').write_text(payload, encoding='utf-8')
    (OUT/'report.html').write_text(HTML.replace('__DATA__', payload.replace('<', '\\u003c')), encoding='utf-8')
    keys = ['display_model','checkpoint','condition_label','run_id','cohort','case_count','n','root_hit','api_tps','e2e_tps','token_coverage','sec_per_turn','turn_coverage','mean_seconds','mean_turns','mean_output_tokens','output_token_coverage','submitted','unscored']
    with (OUT/'summary.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f, fieldnames=keys+['codex_passed','codex_pending','claude_passed','claude_pending']);writer.writeheader()
        for r in data['runs']:
            writer.writerow({**{k:r[k] for k in keys},'codex_passed':r['codex']['passed'],'codex_pending':r['codex']['pending'],'claude_passed':r['claude']['passed'],'claude_pending':r['claude']['pending']})
    print(json.dumps({'conditions':len(data['runs']), 'legacy':len(data['legacy']), 'scores':sum(r['score_files'] for r in data['runs'])}))


HTML = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>RCA 최종 하네스 평가</title><style>
*{box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#f3f6fb;color:#18283b;margin:0;padding:32px;line-height:1.6}main{max-width:1500px;margin:auto}h1{margin-bottom:8px}h2{font-size:21px}.muted,small{color:#607087}small{display:block}.notice{background:#e8f0ff;border-left:4px solid #2a67c4;padding:16px}section{background:white;padding:22px;border:1px solid #dce4ee;border-radius:12px;margin:24px 0}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:13px 11px;border-bottom:1px solid #e3e9f1;text-align:left;vertical-align:top}th{background:#eef3fa;white-space:nowrap}.pending{color:#7a8493}details{margin:14px 0}summary{cursor:pointer;color:#275cb1}pre{white-space:pre-wrap;overflow-wrap:anywhere}th button{font:inherit;color:inherit;background:transparent;border:0;padding:0;text-align:left;cursor:pointer;width:100%}th button:focus-visible{outline:2px solid #225fc2;outline-offset:5px}.sort-indicator{margin-left:5px;color:#275cb1}button{background:#225fc2;color:white;border:0;border-radius:7px;padding:10px 16px;cursor:pointer}@media(max-width:600px){body{padding:12px}section{padding:12px}}
</style></head><body><main><h1>RCA 최종 하네스 평가</h1><p class="muted" id="date"></p><p><a href="training-review.html" style="color:#275cb1">Gemma · Flash 학습 진행 리뷰 →</a></p><p class="muted">표 머리글을 클릭하거나 키보드로 선택하면 오름차순·내림차순 정렬됩니다. 미측정 값은 항상 마지막에 표시합니다.</p><button id="csv">결과 CSV 내려받기</button><div id="tables"></div><section id="gemma-parity"><h2>Gemma 최종 기본 모델 평가</h2><div id="gemma-parity-summary"></div></section>
<section id="generation-speed"><h2>생성 속도 비교 — 같은 L40S, 별도 속도 측정</h2><p><b>Qwen MTP 4는 이 측정에서 뮤즈 DFlash 10과 거의 같은 속도였습니다.</b> 생성 구간 기준 44.46 대 44.49 tok/s로 차이는 약 0.07%입니다. 이는 아래 조건에서의 관측이며 모든 입력·장비에서 같은 속도라는 의미는 아닙니다.</p><div class="scroll"><table><thead><tr><th>모델</th><th>장비</th><th>추측 디코딩</th><th>생성 구간 tok/s<br>(실측 근사)</th><th>평균 첫 토큰 시간</th><th>측정 요청</th></tr></thead><tbody><tr><td>기본 뮤즈 모델</td><td>L40S</td><td>DFlash 10</td><td><b>44.49</b></td><td>0.158초</td><td>6회</td></tr><tr><td>기본 Qwen 모델</td><td>L40S</td><td>MTP 4</td><td><b>44.46</b></td><td>0.207초</td><td>6회</td></tr><tr><td>기본 Gemma 모델</td><td>L40S</td><td>공식 assistant MTP 4</td><td>162.38</td><td>0.313초</td><td>6회</td></tr></tbody></table></div><p class="muted">2026-10-12 측정. 한국어 RCA 입력 3개 × 2회, FP8 기본 모델, thinking ON(뮤즈 high), 동시 요청 1, vLLM 0.28.0, 최대 컨텍스트 16,384, 출력 1,024토큰. 워밍업 제외. 모든 요청은 출력 한도에서 종료됐으며 완성된 시나리오 풀이 속도를 측정한 것이 아닙니다.</p><p>생성 구간 tok/s는 출력 토큰 수 ÷ 첫~마지막 텍스트 델타 시간으로 계산한 SSE 관측 근사치입니다. 위 정답률 표의 <b>풀이 처리속도 (tok/s)</b>에는 도구 조회 시간까지 포함되므로 서로 바꿔 해석하지 않습니다.</p><details><summary>Qwen의 기존 162.03초/턴과 다른 이유</summary><div class="scroll"><table><tr><th>조건</th><th>기존셋 RCA 평가</th><th>별도 생성 속도 측정</th></tr><tr><td>장비</td><td>H200</td><td>L40S</td></tr><tr><td>추측 디코딩</td><td>MTP 1</td><td>MTP 4</td></tr><tr><td>동시 요청</td><td>2</td><td>1</td></tr><tr><td>서빙·출력</td><td>eager ON · 출력 한도 32,768</td><td>속도 측정 전용 · 출력 한도 1,024</td></tr><tr><td>속도 지표</td><td>전체 풀이 17.97 tok/s<br>162.03초/턴</td><td>생성 구간 44.46 tok/s</td></tr></table></div><p>기존 평가에는 MTP 1, eager ON, 동시 요청 2와 긴 추론이 함께 적용됐습니다. 따라서 MTP 1만이 지연 원인이라고 확정하거나, 두 수치의 비율을 MTP 4의 가속 배수라고 계산할 수는 없습니다.</p></details><p class="muted">출처: docs/korean-vanilla-speed-l40s-20261012.md · outputs/korean-speed-l40s-20261012/ · Qwen 기존 평가 serving/launch.json</p></section>
<section><h2>지표 기준</h2><p><b>‘약 · 저장 텍스트 사후 집계’</b>는 남아 있는 추론·답변·도구 함수명·인자 텍스트를 모델 계열 토크나이저로 다시 센 값입니다. 도구 파싱 과정의 JSON 정규화와 누락된 제어 토큰 때문에 당시 API 생성 토큰 수와 같지는 않습니다. 과거 파인튜닝은 보관된 기본 뮤즈 토크나이저 기준이며 당시 토크나이저와의 일치가 별도로 입증된 값은 아닙니다. 실제 API 계측과 구분하며 순수 생성 tok/s 복원에는 사용하지 않습니다. 기본 뮤즈 기존셋은 API 기록이 있는 16/24회만 보면 평균 23,088토큰입니다.</p><p><b>평균 생성 토큰/시나리오</b>는 각 시나리오 1회 실행에서 모든 모델 턴의 출력 토큰을 합한 뒤 평가 횟수로 나눈 값입니다. API가 보고한 추론 토큰도 출력 토큰에 포함되며 중복 합산하지 않습니다. 입력·도구 응답 토큰은 제외합니다. 전체 실행의 토큰 기록이 갖춰진 경우에만 평균을 표시하고, 계측 범위를 함께 표기합니다.</p><p><b>모든 결과가 H200 기준인 것은 아닙니다.</b> 장비·서빙 설정·동시 요청 수가 달라 현재 시간은 각 실험의 관측값이며 동일 장비 속도 비교가 아닙니다. 기존셋 Qwen의 162.03초/턴은 H200에서 eager ON·MTP 1·동시 2 조건의 결과입니다. 평균 출력 2,912토큰/턴 중 추론 토큰이 83.1%였으며 전체 풀이 기준 17.97 tok/s였습니다.</p><p>봉인셋 Qwen은 L40S 2회와 H200 22회가 섞여 있습니다. H200 22회만 집계하면 28.35초/턴, 498.73초/시나리오, 전체 풀이 기준 162.82 tok/s입니다. 기존셋과 데이터·서빙 조건이 달라 이 차이를 특정 설정 하나의 개선 효과로 단정할 수 없습니다.</p><p><b>원인+기전</b>은 원인 대상 적중과 기전 채점 통과를 모두 요구합니다. 채점자는 gpt-6.1-sol, rubric v2를 우선 사용합니다. 초기 Opus CLI의 기전 점수는 당시 Claude Opus 5.5 / v2 채점이며 별도로 표시합니다. 완료 평가의 오답과 미제출도 원래 분모에 포함합니다.</p><p><b>풀이 처리속도 (tok/s)</b> = 출력 토큰 수 ÷ 전체 조사 시간. 도구 조회·네트워크·입력 처리를 포함하며 순수 생성 속도가 아닙니다. 토큰 기록이 없으면 미측정입니다. <b>초/턴</b>은 총 조사 시간 ÷ 총 모델 응답 턴, <b>초/시나리오</b>는 시나리오 1회 실행의 평균입니다.</p><p>봉인셋 기본 뮤즈는 과거 36회 평가입니다. 다른 24회 평가와 반복 수·조사 한도가 다를 수 있으며, 이를 동일 설정으로 간주하지 않습니다. 기본 Gemma는 최종 thinking ON 결과를 표시합니다.</p><p>뮤즈 파인튜닝은 동일 데이터셋·최종 하네스에서 기전 채점이 완료된 최고 모델만 표시하는 기준입니다. 현재 기존셋·봉인셋의 선정 모델은 v2bs checkpoint-94입니다. 기전 미채점 모델은 최고 모델 선정에서 제외합니다. 모델별 생성 한도·장비 등의 세부 조건은 아래 출처에서 확인할 수 있습니다.</p><details><summary>최종 실행 조건·출처</summary><div id="sources"></div></details></section></main>
<script id="data" type="application/json">__DATA__</script><script>
const D=JSON.parse(document.getElementById('data').textContent),models=['Opus 5.5','Sonnet 5.5','기본 뮤즈 모델','기본 Qwen 모델','기본 Qwen Flash Next 모델','기본 Gemma 모델','Gemma 파인튜닝','젬마 기전 강화 파인튜닝','뮤즈 파인튜닝'],sets=['기존 12 시나리오','봉인 12 시나리오'];
const esc=x=>String(x??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),num=x=>x==null?'미측정':Number(x).toFixed(2),rate=(n,d)=>`${n}/${d} · ${(n/d*100).toFixed(1)}%`;
document.getElementById('date').textContent=`갱신 ${D.generated} · 기존셋·봉인셋 각각 12개 시나리오 · 반복 횟수는 행별 분모 확인`;
const gemmaBaseRows=D.runs.filter(r=>r.display_model==='기본 Gemma 모델');
document.getElementById('gemma-parity').hidden=!gemmaBaseRows.length;
document.getElementById('gemma-parity-summary').innerHTML=gemmaBaseRows.map(r=>`<p><b>${esc(r.cohort_name)}</b>: 원인 ${rate(r.root_hit,r.n)} · 원인+기전 ${r.codex.strict!=null?rate(r.codex.strict,r.n):'기전 미채점'}<small>${esc(r.condition_label)} · ${esc(r.hardware_label)}</small></p>`).join('');
let records=[];document.getElementById('tables').innerHTML=sets.map(set=>`<section><h2>${set}</h2><div class="scroll"><table><thead><tr><th>모델</th><th>실행 장비</th><th>원인 정답률</th><th>원인+기전 정답률</th><th>풀이 처리속도 (tok/s)</th><th>초/턴</th><th>초/시나리오</th><th>평균 생성 토큰/시나리오</th><th>평균 턴</th></tr></thead><tbody>${models.filter(model=>!['Gemma 파인튜닝','젬마 기전 강화 파인튜닝','기본 Qwen Flash Next 모델'].includes(model)||D.runs.some(r=>r.cohort_name===set&&r.display_model===model)).map(model=>{let r=D.runs.find(r=>r.cohort_name===set&&r.display_model===model);records.push({set,model,r});return r?`<tr><td><b>${esc(model)}</b><small>${esc(r.condition_label)}</small>${r.checkpoint?`<small>${esc(r.checkpoint)}</small>`:''}</td><td>${esc(r.hardware_label)}</td><td>${rate(r.root_hit,r.n)}</td><td><b>${r.codex.strict!=null?rate(r.codex.strict,r.n):r.claude.strict!=null?rate(r.claude.strict,r.n)+'<small>Claude Opus 5.5 / v2</small>':'기전 미채점'}</b></td><td>${num(r.e2e_tps)}</td><td>${num(r.sec_per_turn)}</td><td>${num(r.mean_seconds)}</td><td>${r.mean_output_tokens==null?(r.token_recount?'약 '+Math.round(r.token_recount.mean_stored_text_tokens).toLocaleString('ko-KR'):'미측정'):Math.round(r.mean_output_tokens).toLocaleString('ko-KR')}</td><td>${num(r.mean_turns)}</td></tr>`:`<tr class="pending"><td>${esc(model)}</td><td colspan="8">평가 없음 — legacy 결과 없음</td></tr>`}).join('')}</tbody></table></div></section>`).join('');
document.getElementById('sources').innerHTML=D.runs.map(r=>`<details><summary>${esc(r.cohort_name)} · ${esc(r.display_model)}</summary><p>제출 ${r.submitted}/${r.n} · 토큰 계측 ${r.token_coverage}/${r.n}</p><pre>${esc(JSON.stringify({실행:r.run_id,설정:r.config,출처:r.config_source,체크포인트선정:r.checkpoint_selection,선정출처:r.checkpoint_selection_source,API계측:r.api_telemetry,종료사유:r.stop_reasons},null,2))}</pre></details>`).join('');
document.getElementById('csv').onclick=()=>{let rows=[['데이터셋','모델','실행 장비','하네스','상태','원인적중','원인기전','평가수','풀이 처리속도 (tok/s)','초/턴','초/시나리오','평균 생성 토큰/시나리오(API)','저장 텍스트 사후 집계 토큰/시나리오','평균턴'],...records.map(({set,model,r})=>[set,model,r?.hardware_label,'legacy',r?'평가 완료':'평가 없음',r?.root_hit,(r?.codex.strict??r?.claude.strict),r?.n,r?.e2e_tps,r?.sec_per_turn,r?.mean_seconds,r?.mean_output_tokens,r?.token_recount?.mean_stored_text_tokens,r?.mean_turns])];let text=rows.map(row=>row.map(v=>'"'+String(v??'').replaceAll('"','""')+'"').join(',')).join('\r\n'),a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+text],{type:'text/csv;charset=utf-8'}));a.download='rca-final-evaluations.csv';a.click();URL.revokeObjectURL(a.href)};
// One sorting contract for result, speed, and configuration tables.
function sortableValue(cell) {
  if (!cell) return null;
  const clone=cell.cloneNode(true); clone.querySelectorAll('small').forEach(x=>x.remove());
  const text=clone.textContent.trim();
  if (!text || /^(미측정|기전 미채점|평가 없음|—)/.test(text)) return null;
  const ratio=text.match(/^(\d+)\/(\d+)/);
  if (ratio) return Number(ratio[2]) ? Number(ratio[1])/Number(ratio[2]) : null;
  const numeric=text.replace(/^약\s*/, '').match(/^(-?[\d,]+(?:\.\d+)?)(?:\s*(?:초|회|토큰|턴|%))?$/);
  return numeric ? Number(numeric[1].replaceAll(',','')) : text;
}
function enableTableSorting(table) {
  let head=table.tHead;
  if (!head) { const first=table.rows[0]; head=table.createTHead(); if(first)head.append(first); }
  // createTHead does not add rows: use the existing header row for tables without a thead.
  const header=head.rows[0];
  if (!header) return;
  [...header.cells].forEach((th,index)=>{
    const label=th.textContent.trim(), button=document.createElement('button');
    button.type='button';button.innerHTML=th.innerHTML+'<span class="sort-indicator" aria-hidden="true">↕</span>';
    button.setAttribute('aria-label',label+' 정렬');th.replaceChildren(button);th.setAttribute('aria-sort','none');
    button.onclick=()=>{
      const ascending=th.getAttribute('aria-sort')!=='ascending';
      [...header.cells].forEach(h=>{h.setAttribute('aria-sort','none');h.querySelector('.sort-indicator').textContent='↕'});
      th.setAttribute('aria-sort',ascending?'ascending':'descending');button.querySelector('.sort-indicator').textContent=ascending?'▲':'▼';
      const body=table.tBodies[0];if(!body)return;
      const rows=[...body.rows];
      rows.sort((a,b)=>{
        const x=sortableValue(a.cells[index]),y=sortableValue(b.cells[index]);
        if(x==null)return y==null?0:1;if(y==null)return -1;
        const order=typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y),'ko',{numeric:true});
        return ascending?order:-order;
      });
      body.append(...rows);
    };
  });
}
document.querySelectorAll('table').forEach(enableTableSorting);
</script></body></html>'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    archive = OUT/'archive-data.json'
    if '--render-only' in sys.argv:
        if not archive.exists():
            archive.write_text((OUT/'data.json').read_text(), encoding='utf-8')
        write_view(read(archive))
        return
    runs = [aggregate(p) for p in sorted((ROOT/'outputs/mcp-eval').iterdir()) if p.is_dir() and any(p.glob('case-*/run*/score.json'))]
    runs.sort(key=lambda r: (not ('20261012' in r['run_id'] or '20261008' in r['run_id']), r['run_id']))
    historical = read(ROOT/'outputs/reports/opus55-harness-20261007/evidence.json') or {}
    legacy = read(OUT/'legacy-nonmcp.json') or {}
    data = {'legacy':legacy.get('experiments', []),'generated':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds'),'runs':runs,'historical':historical.get('metrics', {})}
    archive.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    write_view(data)


if __name__ == '__main__':
    main()
