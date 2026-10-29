"""Judge every valid submission and export complete, exact-token GRPO groups.

The pure scalar formula is cross-tested against authoritative train.grpo rewards.
No infrastructure/judge errors become zero, partial credit, or model failure.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from run import ROOT, WORK, validate_response
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'scripts'))
from rca_lab.mcp.rl_reward import (
    evidence_cache_valid,
    judge_cache_path,
    judge_cache_valid,
    judge_trajectory_evidence,
    sha256_json,
)
from rca_lab.train.grpo import EpisodeOutcome, trajectory_rewards
from rca_lab.mcp.causal_depth import causal_depth_cache_valid, judge_causal_depth
from mcp_judge_runs import judge_run

MODEL_FAILURES={'no_tool_call','max_turns','output_budget_exhausted','forced_answer_rejected'}

def scalar_reward(*, root_precision, root_recall, mechanism_score, success,
                  model_failure, generated_tokens, investigation_turns,
                  token_reference, turn_reference, efficiency_weight,
                  path_score=None, hallucination_score=None,
                  evidence_judge_status=None, path_weight=0.0,
                  hallucination_weight=0.0, success_hallucination_weight=None, partial_weights=None,
                  depth_weight=0.0, causal_depth=None, fabricated_success_cap=None, major_claims=None, major_claim_penalty=0.0):
    if not 0<=efficiency_weight<=.2 or not all(math.isfinite(x) for x in [root_precision,root_recall,efficiency_weight]):
        raise ValueError('Invalid reward inputs')
    if not 0<=root_precision<=1 or not 0<=root_recall<=1 or mechanism_score not in (0,.5,1):
        raise ValueError('Invalid correctness inputs')
    if token_reference<=0 or turn_reference<=0 or generated_tokens<=0 or investigation_turns<0:
        raise ValueError('Invalid full-trajectory cost inputs')
    if model_failure:
        if success:raise ValueError('Failure cannot be success')
        return -.2
    outcome=EpisodeOutcome('completed',root_precision==root_recall==1,mechanism_score==1,
        generated_tokens,investigation_turns,root_precision=root_precision,root_recall=root_recall,
        model_failure=model_failure,mechanism_score=mechanism_score,
        evidence_judge_status=evidence_judge_status,path_score=path_score,
        hallucination_score=hallucination_score,causal_depth=causal_depth,major_claims=major_claims)
    expected_success=outcome.root_correct and outcome.mechanism_correct and not model_failure
    if success!=expected_success:raise ValueError('Success flag disagrees with scalar reward inputs')
    return trajectory_rewards([outcome],token_budget=token_reference,turn_budget=turn_reference,
        efficiency_weight=efficiency_weight,path_weight=path_weight,
        hallucination_weight=hallucination_weight,success_hallucination_weight=success_hallucination_weight,
        **({'partial_root_weight':partial_weights[0],'partial_mechanism_weight':partial_weights[1]} if partial_weights else {}),
        **({'depth_weight':depth_weight} if depth_weight else {}),
        **({'fabricated_success_cap':fabricated_success_cap} if fabricated_success_cap is not None else {}),
        **({'major_claim_penalty':major_claim_penalty} if major_claim_penalty else {})).item()

def classify(score):
    if score.get('unscored') or score.get('infrastructure_error') or score.get('error'):
        raise ValueError('Infrastructure/unscored episode; group withheld')
    reason=score.get('stop_reason')
    if reason=='submitted':return False
    if reason in MODEL_FAILURES:return True
    raise ValueError(f'Unknown failure classification {reason!r}; group withheld')

def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def main():
    p=argparse.ArgumentParser()
    p.add_argument('directory',type=Path)
    p.add_argument('--efficiency-coefficient',type=float,default=.2)
    p.add_argument('--reference-costs',type=Path,default=WORK.parent/'reference-costs.json')
    # 2026-10-15: Codex is quota-limited until 2026-10-21; the user chose Claude Opus 5.5 (rubric v2) for loop rewards.
    p.add_argument('--judge-provider',choices=('codex','claude'),default='codex')
    p.add_argument('--judge-model',default='gpt-6.1-sol')
    # 2026-10-19 (user): rca-evidence-v3 = fabrication penalty on joint successes raised to this weight (partials keep .1)
    p.add_argument('--success-hallucination-weight',type=float,default=None)
    # 2026-10-19 (user: "기전 비율을 좀더 높여"): rca-evidence-v4 = v3 + partial credit quality*(root + mechanism*score) with these weights
    p.add_argument('--partial-root-weight',type=float,default=None)
    p.add_argument('--partial-mechanism-weight',type=float,default=None)
    # 2026-10-19 (user: explain the target and its cause precisely): rca-evidence-v5 = v4 + depth_weight * causal depth
    # (rca_lab.mcp.causal_depth, grounded root-chain links) on joint successes
    p.add_argument('--depth-weight',type=float,default=None)
    # 2026-10-20 (user): rca-evidence-v6 = v5 + joint successes with a major fabricated claim capped (before evidence terms)
    p.add_argument('--fabricated-success-cap',type=float,default=None)
    # 2026-10-20 (user): rca-evidence-v7 = v5 + joint successes lose this much per major fabricated claim (at most .3)
    p.add_argument('--major-claim-penalty',type=float,default=None)
    # 2026-10-21: mechanism judge rubric v3 (what counts as the trigger) and causal-depth rubric v2 (load is not a trigger)
    p.add_argument('--judge-rubric',default='v2')
    p.add_argument('--depth-rubric',default='rca-causal-depth-v1')
    args=p.parse_args()
    if (args.partial_root_weight is None) != (args.partial_mechanism_weight is None):
        raise ValueError('--partial-root-weight and --partial-mechanism-weight go together')
    partial_weights=(args.partial_root_weight,args.partial_mechanism_weight) if args.partial_root_weight is not None else None
    if partial_weights and args.success_hallucination_weight is None:
        raise ValueError('rca-evidence-v4 builds on v3: --success-hallucination-weight is required')
    if args.major_claim_penalty is not None and (args.depth_weight is None or args.fabricated_success_cap is not None):
        raise ValueError('rca-evidence-v7 builds on v5 (needs --depth-weight) and replaces the v6 cap')
    if args.fabricated_success_cap is not None and args.depth_weight is None:
        raise ValueError('rca-evidence-v6 builds on v5: --depth-weight is required')
    if args.depth_weight is not None and not partial_weights:
        raise ValueError('rca-evidence-v5 builds on v4: partial weights are required')
    if not 0<=args.efficiency_coefficient<=.2:p.error('coefficient must be in [0,.2]')
    refs=json.loads(args.reference_costs.read_text())
    # Root creates this immutable, train-only base calibration with source hashes.
    nref=refs.get('reference_tokens',refs.get('token_reference'))
    tref=refs.get('reference_turns',refs.get('turn_reference'))
    if not isinstance(nref,(int,float)) or not isinstance(tref,(int,float)) or nref<=0 or tref<=0:
        raise ValueError('Missing positive frozen reference_tokens/reference_turns')
    m=json.loads((args.directory/'manifest.json').read_text())
    prep=json.loads(Path(m.get('preparation') or WORK.parent/'preparation.json').read_text())   # pinned per group (2026-10-15)
    if m.get('public'):
        import yaml
        sp=Path(m['split_source']);split=yaml.safe_load(sp.read_text())
        if hashlib.sha256(sp.read_bytes()).hexdigest()!=m['split_sha256']:raise ValueError('Public split changed since collection')
        held={c for k,v in split.items() if k!='train' and isinstance(v,list) for c in v}
        if m['case'] not in split.get('train',[]) or m['case'] in held:raise ValueError('Public case not in train partition')
    elif m['case'] not in prep['train_cases']:raise ValueError('Not a train case')
    if m['status']!='captured_requires_complete_group_judging_and_policy_identity_verification':raise ValueError('Collection incomplete')
    cohort=args.directory/'episodes'/m['group_id']
    runs=sorted(cohort.glob('case-*/run*/score.json'))
    if len(runs)!=m['group_size']:raise ValueError('Incomplete group: never optimize a subset')
    raw=[json.loads(p.read_text()) for p in sorted(args.directory.glob('turn-*.json'))]
    by_run={}
    serving_backend_sha256=m.get('serving_backend_sha256')
    for row in raw:
        if row['case_id']!=m['case'] or row['policy_version']!=m['policy_version'] or row['group_id']!=m['group_id']:raise ValueError('Mixed rollout identity')
        if serving_backend_sha256 is not None and row.get('serving_backend_sha256')!=serving_backend_sha256:
            raise ValueError('Mixed serving backend identity')
        if row['request'].get('tool_choice') not in ('auto',None):raise ValueError('Constrained tool-choice probabilities unsupported; entire group withheld')
        t=validate_response(row['response'])
        by_run.setdefault(str(Path(row['episode_response_log']).parent.resolve()),[]).append(t)
    episodes=[]
    for score_path in runs:
        run=score_path.parent;score=json.loads(score_path.read_text())
        if score['case_id']!=m['case']:raise ValueError('Case mismatch')
        model_failure=classify(score)
        turns=by_run.get(str(run.resolve()))
        if not turns:raise ValueError('Missing raw token samples')
        # Non-submission has a deterministic -.2, no candidate mechanism exists.
        mechanism=0.0
        mechanism_judge_status='not_applicable_model_failure'
        evidence_judge_status='not_applicable_model_failure'
        path_score=None
        hallucination_score=None
        evidence_artifact=None
        evidence_artifact_sha256=None
        major_claims=None
        if not model_failure:
            outcome=judge_run(run,args.judge_model,args.judge_rubric,True,args.judge_provider)
            if outcome not in ('judged','cached'):raise ValueError(f'Mechanism judge unresolved: {outcome}')
            cache=json.loads(judge_cache_path(run,args.judge_provider,model=args.judge_model,rubric=args.judge_rubric).read_text())
            if not judge_cache_valid(cache,m['case'],provider=args.judge_provider,model=args.judge_model,rubric=args.judge_rubric):raise ValueError('Invalid judge cache')
            mechanism=cache['score'];mechanism_judge_status='completed'
            result=json.loads((run/'result.json').read_text())
            evidence_artifact,evidence_outcome=judge_trajectory_evidence(
                run,messages=load_jsonl(run/'trajectory.jsonl'),result=result,score=score,
                model=args.judge_model,provider=args.judge_provider)
            if evidence_outcome not in ('judged','cached') or evidence_artifact is None:
                raise ValueError(f'Evidence judge unresolved: {evidence_outcome}')
            if not evidence_cache_valid(evidence_artifact,
                    evidence_artifact['item'],model=args.judge_model,provider=args.judge_provider):
                raise ValueError('Invalid evidence judge cache')
            verdict=evidence_artifact['verdict']
            path_score=verdict['path_score'];hallucination_score=verdict['hallucination_score']
            evidence_judge_status='completed'
            evidence_artifact_sha256=sha256_json(evidence_artifact)
            major_claims=sum(1 for c in verdict.get('unsupported_or_fabricated_claims') or [] if c.get('severity')=='major')
        root_ok=score.get('root_f1')==1
        success=root_ok and mechanism==1 and not model_failure
        causal_depth=None;depth_artifact=None;depth_artifact_sha256=None
        if args.depth_weight is not None and success:
            depth_artifact,depth_outcome=judge_causal_depth(run,messages=load_jsonl(run/'trajectory.jsonl'),result=result,score=score,
                model=args.judge_model,provider=args.judge_provider,rubric=args.depth_rubric)
            if depth_outcome not in ('judged','cached') or depth_artifact is None:
                raise ValueError(f'Causal depth judge unresolved: {depth_outcome}')
            if not causal_depth_cache_valid(depth_artifact,depth_artifact['item'],model=args.judge_model,provider=args.judge_provider):
                raise ValueError('Invalid causal depth cache')
            causal_depth=depth_artifact['verdict']['depth'];depth_artifact_sha256=sha256_json(depth_artifact)
        tokens=sum(len(t['completion_token_ids']) for t in turns)
        precision=float(score.get('root_precision',0));recall=float(score.get('root_recall',0))
        reward=scalar_reward(root_precision=precision,root_recall=recall,mechanism_score=mechanism,
            success=success,model_failure=model_failure,generated_tokens=tokens,investigation_turns=len(turns),
            token_reference=nref,turn_reference=tref,efficiency_weight=args.efficiency_coefficient,
            path_score=path_score,hallucination_score=hallucination_score,
            evidence_judge_status=evidence_judge_status,path_weight=.05,hallucination_weight=.1,
            success_hallucination_weight=args.success_hallucination_weight,partial_weights=partial_weights,
            depth_weight=args.depth_weight or 0.0,causal_depth=causal_depth,
            fabricated_success_cap=args.fabricated_success_cap,major_claims=major_claims,
            major_claim_penalty=args.major_claim_penalty or 0.0)
        episodes.append({'episode_id':run.name,'reward':reward,'joint_success':success,
            'root_f1':score.get('root_f1'),'root_precision':precision,'root_recall':recall,
            'mechanism_score':mechanism,'mechanism_judge_status':mechanism_judge_status,
            'evidence_judge_status':evidence_judge_status,'path_score':path_score,
            'hallucination_score':hallucination_score,'evidence_artifact_sha256':evidence_artifact_sha256,
            'evidence_artifact':evidence_artifact,
            **({'causal_depth':causal_depth,'causal_depth_artifact':depth_artifact,'causal_depth_artifact_sha256':depth_artifact_sha256}
               if args.depth_weight is not None else {}),
            **({'major_claims':major_claims} if args.fabricated_success_cap is not None or args.major_claim_penalty is not None else {}),
            'status':score.get('stop_reason'),'model_failure':model_failure,
            'infrastructure_error':False,'judge_status':'completed',
            'generated_tokens':tokens,'turns':turns})
    out={'group_id':m['group_id'],'policy_version':m['policy_version'],'case_id':m['case'],
         'split_sha256':m.get('split_sha256',prep['split_sha256']),'split_source':m.get('split_source',prep['split_source']),'public':bool(m.get('public')),'sampling':{'temperature':1.0,'top_p':1.0,'top_k':-1,'min_p':0.0},'episodes':episodes,
         'efficiency_coefficient':args.efficiency_coefficient,'reference_tokens':nref,'reference_turns':tref,
         'path_weight':.05,'hallucination_weight':.1,
         **({'reward_version':'rca-evidence-v7','success_hallucination_weight':args.success_hallucination_weight,
             'partial_root_weight':partial_weights[0],'partial_mechanism_weight':partial_weights[1],'depth_weight':args.depth_weight,
             'major_claim_penalty':args.major_claim_penalty}
            if args.major_claim_penalty is not None else
            {'reward_version':'rca-evidence-v6','success_hallucination_weight':args.success_hallucination_weight,
             'partial_root_weight':partial_weights[0],'partial_mechanism_weight':partial_weights[1],'depth_weight':args.depth_weight,
             'fabricated_success_cap':args.fabricated_success_cap}
            if args.fabricated_success_cap is not None else
            {'reward_version':'rca-evidence-v5','success_hallucination_weight':args.success_hallucination_weight,
             'partial_root_weight':partial_weights[0],'partial_mechanism_weight':partial_weights[1],'depth_weight':args.depth_weight}
            if args.depth_weight is not None else
            {'reward_version':'rca-evidence-v4','success_hallucination_weight':args.success_hallucination_weight,
             'partial_root_weight':partial_weights[0],'partial_mechanism_weight':partial_weights[1]} if partial_weights else
            {'reward_version':'rca-evidence-v3','success_hallucination_weight':args.success_hallucination_weight}
            if args.success_hallucination_weight is not None else {'reward_version':'rca-evidence-v2'}),
         'judge_provider':args.judge_provider,'judge_model':args.judge_model,'judge_rubric':args.judge_rubric,
         **({'depth_rubric':args.depth_rubric} if args.depth_weight is not None else {}),
         'serving_backend':m.get('serving_backend'),'serving_backend_sha256':serving_backend_sha256,
         'reference_costs_path':str(args.reference_costs),'reference_costs_sha256':hashlib.sha256(args.reference_costs.read_bytes()).hexdigest(),
         'trainer_gates':['exact policy identity/logprob parity','frozen reference logprobs','complete-group normalized advantages']}
    output_path=args.directory/'group.evidence-v2.json'
    output_path.write_text(json.dumps(out,ensure_ascii=False))
    print(json.dumps({'group':str(output_path),'rewards':[e['reward'] for e in episodes]}))
if __name__=='__main__':main()
