"""Train-only on-policy capture through the unchanged legacy RCA harness.

Each response stores server-exact prompt/output IDs, sampled-token log probabilities,
and request/response. No re-tokenization of parsed reasoning/tool messages is allowed.
The caller must serve the exact frozen policy_version for an entire GRPO group.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[3]
WORK = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))

def validate_response(reply):
    choices = reply.get('choices', [])
    if len(choices) != 1:
        raise ValueError('Exactly one choice required')
    choice = choices[0]
    prompt_ids = reply.get('prompt_token_ids')
    ids = choice.get('token_ids')
    rows = (choice.get('logprobs') or {}).get('content')
    usage = reply.get('usage') or {}
    if not isinstance(prompt_ids, list) or not prompt_ids or not all(type(i) is int for i in prompt_ids):
        raise ValueError('Missing exact server prompt_token_ids')
    if not isinstance(ids, list) or not ids or not all(type(i) is int for i in ids):
        raise ValueError('Missing exact server output token_ids')
    if not isinstance(rows, list) or len(rows) != len(ids) or len(ids) != usage.get('completion_tokens'):
        raise ValueError('Incomplete sampled-token coverage (reasoning/tools/stops must all be covered)')
    if len(prompt_ids) != usage.get('prompt_tokens'):
        raise ValueError('Prompt token count mismatch')
    for token, row in zip(ids, rows):
        if row.get('token') != f'token_id:{token}':
            raise ValueError('Token/logprob identity mismatch')
        value = row.get('logprob')
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value > 1e-5:
            raise ValueError('Invalid sampled-token logprob')
    return {'prompt_token_ids': prompt_ids, 'completion_token_ids': ids,
            'old_logprobs': [r['logprob'] for r in rows],
            'finish_reason': choice.get('finish_reason')}

def unfiltered_request(body):
    """Keep harness instructions/validation, remove only constrained decoding.

    Forced final submission remains an instruction and an execution rule. A
    non-submit action is rejected by the existing force_answer validation and
    yields a model-attributable failure, not an omitted training trajectory.
    """
    allowed={'type':'function','function':{'name':'submit_rca'}}
    choice=body.get('tool_choice')
    if choice not in ('auto',None) and choice!=allowed:
        raise ValueError('Unexpected tool-choice constraint')
    if any(k in body for k in ('structured_outputs','guided_json','guided_regex','guided_grammar','response_format')):
        raise ValueError('Unexpected constrained decoding in unfiltered training')
    wire=dict(body)
    wire.update(tool_choice='auto',temperature=1.0,top_p=1.0,top_k=-1,min_p=0.0,
        repetition_penalty=1.0,presence_penalty=0.0,frequency_penalty=0.0,
        logprobs=True,top_logprobs=0,return_token_ids=True,return_tokens_as_token_ids=True,
        chat_template_kwargs={'enable_thinking':True})
    return wire

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--case', required=True)
    p.add_argument('--policy-version', required=True, help='Immutable base/adapter content identity, never latest')
    p.add_argument('--endpoint', default='http://127.0.0.1:18230/v1')
    p.add_argument('--model', default='qwen-flash-next-int4-mtp4')
    p.add_argument('--group-id', required=True)
    p.add_argument('--group-size', type=int, default=4)
    p.add_argument('--seed', type=int, default=42000)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--serving-manifest', type=Path, help='Immutable serving backend/config identity for this group')
    p.add_argument('--parallel-runs', type=int, default=1, help='episodes of the group run concurrently (2026-10-15)')
    # 2026-10-15 (user): public-data training groups (OpenRCA 2.0 / RCAEval) in our capture format; train partition only.
    p.add_argument('--public-split', type=Path, help='public split YAML (train partition); enables a public-data group')
    p.add_argument('--public-contract', help='golden contract YAML for the public case')
    p.add_argument('--public-cases-root', help='capture root for the public case')
    # 2026-10-15: Polestar data version is pinned per update (split v3 adds new captures); default keeps the original record.
    p.add_argument('--preparation', type=Path, default=WORK.parent/'preparation.json',
                   help='preparation record (train/validation/sealed allowlists, split source + sha, partition contract)')
    # 2026-10-15 speedup: the loop restores the next update's capture while the previous update trains (`--runner prepare`)
    p.add_argument('--prerestored', type=Path, help='prepared case dir (connections.json + prefetch.json) for this group; reused instead of restoring')
    # 2026-10-19 (user): evidence-masking augmentation — the same case with some evidence sources hidden (capture untouched)
    p.add_argument('--max-turns', type=int, default=60, help='investigation turn cap (2026-10-21: turn-budget variants)')
    p.add_argument('--mcp-binary', type=Path, default=ROOT/'outputs/sealed-sonnet55-20261012/runtime/rca-mcp',
                   help='rca-mcp binary (2026-10-20: loop passes its configured runtime)')
    p.add_argument('--prompt-revision', type=int, default=1, help='student prompt revision passed to mcp_case_run (2026-10-19)')
    p.add_argument('--evidence-mask', default='', help='comma list passed to mcp_case_run --evidence-mask (rca_lab.mcp.masking.MASKABLE)')
    p.add_argument('--context-policy', default='rolling8', help='history view passed to mcp_case_run --context-policy (2026-10-28: budget)')
    args = p.parse_args()
    args.out = args.out.resolve()
    serving_backend = None
    serving_backend_sha256 = None
    if args.serving_manifest:
        serving_backend = json.loads(args.serving_manifest.read_text())
        if not isinstance(serving_backend, dict) or not serving_backend:
            raise ValueError('Serving manifest must be a nonempty object')
        serving_backend_sha256 = hashlib.sha256(json.dumps(serving_backend, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    prep = json.loads(args.preparation.read_text())
    if args.public_split:
        import yaml
        split = yaml.safe_load(args.public_split.read_text())
        held = {c for k, v in split.items() if k != 'train' and isinstance(v, list) for c in v}
        if args.case not in split.get('train', []) or args.case in held:
            p.error('Public case must be in the public split train partition and in no held-out list')
        if args.case in prep['train_cases'] + prep['validation_cases'] + prep['sealed_cases']:
            p.error('Public option given for a Polestar case')
        if not (args.public_contract and args.public_cases_root):
            p.error('--public-contract and --public-cases-root are required with --public-split')
        split_source, split_sha256, partition_contract, cases_root = (str(args.public_split.resolve()), hashlib.sha256(args.public_split.read_bytes()).hexdigest(),
                                                                     args.public_contract, args.public_cases_root)
    else:
        from preflight import audit
        audit(args.preparation)
        if args.case not in prep['train_cases']:
            p.error('Only explicitly allowlisted train cases can generate optimizer samples')
        split_source, split_sha256, partition_contract, cases_root = (prep['split_source'], prep['split_sha256'],
                                                                     prep.get('partition_contract', 'configs/eval/train-family-v2-expanded-20261012.yaml'), '/data/eval-cases')
    if args.group_size < 2:
        p.error('GRPO requires group size >=2')
    if args.out.exists():
        p.error('Use a fresh output path; no cached/off-policy rollout reuse')
    args.out.mkdir(parents=True)
    prefix = 'frl'+hashlib.sha256(args.group_id.encode()).hexdigest()[:12]
    reuse = []
    if args.prerestored:
        marker = args.prerestored/'prefetch.json'
        info = json.loads(marker.read_text()) if marker.exists() else {}
        if (args.prerestored/'connections.json').exists() and info.get('project_prefix') == prefix and info.get('case') == args.case:
            target = args.out/'episodes'/args.group_id/args.case   # case runner layout: <out-root>/<run-name>/<case>
            target.mkdir(parents=True)
            (target/'connections.json').write_text((args.prerestored/'connections.json').read_text())
            # consumed once: the case runner tears this DB set down after the episodes, so a retry must restore afresh
            (args.prerestored/'connections.json').rename(args.prerestored/'connections.consumed.json')
            reuse = ['--reuse-db']   # mcp_case_run skips the restore and still tears the DB set down after the episodes
        else:
            print(f'prerestored {args.prerestored} not usable (prefix/case mismatch or missing connections); restoring normally', flush=True)
            args.prerestored = None
    import rca_lab.mcp.student as student
    original = student._post_chat
    index = 0
    import threading
    index_lock = threading.Lock()   # episodes may run concurrently (--parallel-runs); file indices must stay unique
    def recorded(config, body):
        nonlocal index
        # Unwarped categorical sampling makes the returned raw model logprobs
        # correspond to the behavior policy used by the clipped objective.
        wire = unfiltered_request(body)
        reply = original(config, wire)
        with index_lock:
            my_index = index
            index += 1
        # Persist raw evidence even when the response fails validation.
        entry = {'policy_version': args.policy_version, 'group_id': args.group_id,
                 'case_id': args.case, 'response_index': my_index,
                 'serving_backend': serving_backend,
                 'serving_backend_sha256': serving_backend_sha256,
                 'episode_response_log': str(config.response_log),
                 'request': wire, 'response': reply,
                 'original_tool_choice': body.get('tool_choice'),
                 'training_tool_choice_override': body.get('tool_choice') not in ('auto', None),
                 'sampling_constraint': wire.get('tool_choice') not in ('auto', None)}
        path = args.out / f'turn-{my_index:05d}.json'
        path.write_text(json.dumps(entry, ensure_ascii=False))
        checked = validate_response(reply)
        entry['training_tokens'] = checked
        entry['token_digest'] = hashlib.sha256(json.dumps([checked['prompt_token_ids'], checked['completion_token_ids']]).encode()).hexdigest()
        path.write_text(json.dumps(entry, ensure_ascii=False))
        return reply
    student._post_chat = recorded
    manifest = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}   # Path args (public split, preparation) must serialize
    manifest['serving_manifest'] = str(args.serving_manifest) if args.serving_manifest else None
    manifest['serving_backend'] = serving_backend
    manifest['serving_backend_sha256'] = serving_backend_sha256
    manifest['harness'] = 'legacy'; manifest['forced_submit_decoding'] = 'auto sampling; original final-submit instruction and validator retained'; manifest['sampling'] = 'unwarped categorical temperature1'
    manifest['split_sha256'] = split_sha256
    manifest['split_source'] = split_source
    manifest['public'] = bool(args.public_split)
    manifest['status'] = 'collecting_not_optimizer_ready'
    (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2))
    sys.argv = ['scripts/mcp_case_run.py', '--runner', 'student', '--student-tools', 'harness',
                '--run-name', args.group_id, '--case', args.case, '--partition', 'train',
                '--split', split_source, '--cases-root', cases_root, '--contract', partition_contract,
                '--out-root', str(args.out/'episodes'), '--project-prefix', prefix, *reuse,
                '--runs', str(args.group_size), '--parallel-runs', str(args.parallel_runs), '--max-turns', str(args.max_turns), '--max-prompt-tokens', '0',
                '--max-output-tokens', '32768', '--tool-result-max-chars', '6000',
                '--observation-policy', 'legacy', '--no-guided', '--endpoint', args.endpoint,
                '--model', args.model, '--seed', str(args.seed), '--temperature', '1', '--top-p', '1', '--top-k', '-1',
                '--mcp-binary', str(args.mcp_binary),
                *(['--evidence-mask', args.evidence_mask] if args.evidence_mask else []),
                *(['--prompt-revision', str(args.prompt_revision)] if args.prompt_revision != 1 else []),
                *(['--context-policy', args.context_policy] if args.context_policy != 'rolling8' else [])]
    try:
        runpy.run_path(str(ROOT/'scripts/mcp_case_run.py'), run_name='__main__')
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise
    manifest['status'] = 'captured_requires_complete_group_judging_and_policy_identity_verification'
    manifest['captured_responses'] = index
    (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    main()
