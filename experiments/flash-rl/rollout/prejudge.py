"""Judge one finished episode while the rest of its group is still rolling out (2026-10-15 speedup).

Calls exactly the judge functions finalize.py uses, with the same arguments, so the provider/model-specific judge caches
it writes are the ones finalize later finds ("cached"). It never writes rewards or group files; finalize.py remains the
only place that validates caches, computes rewards and exports a group. Model-failure episodes are skipped (finalize
does not judge them either). An episode that would make finalize withhold the group is left for finalize to report.
"""
import argparse
import json
from pathlib import Path

from finalize import classify, judge_causal_depth, judge_cache_path, judge_run, judge_trajectory_evidence, load_jsonl


def main():
    p = argparse.ArgumentParser()
    p.add_argument('run', type=Path, help='episode run directory containing score.json')
    p.add_argument('--judge-provider', choices=('codex', 'claude'), default='codex')
    p.add_argument('--judge-model', default='gpt-6.1-sol')
    p.add_argument('--depth', action='store_true', help='also judge causal depth on joint successes (rca-evidence-v5)')
    p.add_argument('--judge-rubric', default='v2')
    p.add_argument('--depth-rubric', default='rca-causal-depth-v1')
    args = p.parse_args()
    score = json.loads((args.run / 'score.json').read_text())
    try:
        if classify(score):
            print(f'{args.run}: model failure, not judged')
            return
    except ValueError as exc:
        print(f'{args.run}: left for finalize ({exc})')
        return
    outcome = judge_run(args.run, args.judge_model, args.judge_rubric, True, args.judge_provider)
    result = json.loads((args.run / 'result.json').read_text())
    _, evidence = judge_trajectory_evidence(args.run, messages=load_jsonl(args.run / 'trajectory.jsonl'), result=result, score=score,
                                            model=args.judge_model, provider=args.judge_provider)
    depth = 'skipped'
    if args.depth and score.get('root_f1') == 1 and outcome in ('judged', 'cached'):
        cache = json.loads(judge_cache_path(args.run, args.judge_provider, model=args.judge_model, rubric=args.judge_rubric).read_text())
        if cache.get('score') == 1:
            _, depth = judge_causal_depth(args.run, messages=load_jsonl(args.run / 'trajectory.jsonl'), result=result, score=score,
                                          model=args.judge_model, provider=args.judge_provider, rubric=args.depth_rubric)
    print(f'{args.run}: mechanism {outcome}, evidence {evidence}, depth {depth}')


if __name__ == '__main__':
    main()
