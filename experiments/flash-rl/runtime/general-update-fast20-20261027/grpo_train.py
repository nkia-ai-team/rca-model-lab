"""One genuine complete-group GRPO optimizer step from the frozen base policy.

No generation is performed here. Subsequent updates require fresh rollouts from
exported current-policy weights, which this initial base-only admission deliberately
refuses to impersonate. Full prompts and every completion turn are retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import torch

BASE_REVISION = 'base-4c67bf686b7f7fd386bae6b07ab59e8ff1d5b897'


def validate_group(group, expected_version):
    if group.get('policy_version') != expected_version:
        raise ValueError('Only the explicitly matching initial base-policy group is admitted')
    if not group.get('group_id') or not group.get('case_id'):
        raise ValueError('Missing group/case identity')
    backend = group.get('serving_backend')
    if backend is not None:
        encoded = json.dumps(backend, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        if not isinstance(backend, dict) or hashlib.sha256(encoded).hexdigest() != group.get('serving_backend_sha256'):
            raise ValueError('Serving backend identity mismatch')
    sampling = group.get('sampling', {})
    expected_sampling = {'temperature': 1.0, 'top_p': 1.0, 'top_k': -1, 'min_p': 0.0}
    if any(sampling.get(key) != value for key, value in expected_sampling.items()):
        raise ValueError('Base pilot requires explicit temperature1/unfiltered sampling')
    episodes = group.get('episodes', [])
    if len(episodes) < 2:
        raise ValueError('At least two complete same-prompt episodes are required')
    reward_version = group.get('reward_version', 'rca-v1')
    if reward_version not in ('rca-v1', 'rca-evidence-v2', 'rca-evidence-v3', 'rca-evidence-v4', 'rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7'):
        raise ValueError('Unknown reward contract version')
    evidence_enabled = reward_version in ('rca-evidence-v2', 'rca-evidence-v3', 'rca-evidence-v4', 'rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7')
    if evidence_enabled:
        if group.get('path_weight') != .05 or group.get('hallucination_weight') != .1:
            raise ValueError('Evidence pilot requires explicit versioned reward weights')
        # 2026-10-19 (fast11): v3 = fabrication penalty on joint successes raised to .3 (partials keep .1)
        if reward_version in ('rca-evidence-v3', 'rca-evidence-v4', 'rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') and group.get('success_hallucination_weight') != .3:
            raise ValueError('Evidence v3/v4 requires success_hallucination_weight .3')
        # 2026-10-19 (fast12): v4 = v3 with mechanism-led partial credit quality*(.1 + .2*mechanism)
        if reward_version in ('rca-evidence-v4', 'rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') and (group.get('partial_root_weight'), group.get('partial_mechanism_weight')) != (.1, .2):
            raise ValueError('Evidence v4/v5 requires partial_root_weight .1 and partial_mechanism_weight .2')
        # 2026-10-19 (fast13): v5 = v4 + .1 * causal depth (rca_lab.mcp.causal_depth) on joint successes
        if reward_version in ('rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') and group.get('depth_weight') != .1:
            raise ValueError('Evidence v5/v6 requires depth_weight .1')
        # 2026-10-20 (fast14): v6 = v5 + joint successes with a major fabricated claim capped at .35 before evidence terms
        if reward_version == 'rca-evidence-v6' and group.get('fabricated_success_cap') != .35:
            raise ValueError('Evidence v6 requires fabricated_success_cap .35')
        # 2026-10-20 (fast15): v7 = v5 + joint successes lose .1 per major fabricated claim (at most .3)
        if reward_version == 'rca-evidence-v7' and group.get('major_claim_penalty') != .1:
            raise ValueError('Evidence v7 requires major_claim_penalty .1')
    success_floor = .5 if reward_version in ('rca-evidence-v3', 'rca-evidence-v4', 'rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') else .7
    reward_ceiling = 1.15 if reward_version in ('rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') else 1.05
    episode_ids = set()
    for episode in episodes:
        identity = episode.get('episode_id')
        if not identity or identity in episode_ids:
            raise ValueError('Missing/duplicate episode identity')
        episode_ids.add(identity)
        if episode.get('judge_status') != 'completed' or episode.get('infrastructure_error', False):
            raise ValueError('Incomplete judgment/infrastructure failure blocks the entire group')
        if episode.get('case_id', group['case_id']) != group['case_id']:
            raise ValueError('Group mixes prompts/cases')
        reward = episode.get('reward')
        if isinstance(reward, bool) or not isinstance(reward, (float, int)) or not math.isfinite(reward):
            raise ValueError('Missing or nonfinite episode reward')
        if not -.2 <= reward <= (reward_ceiling if evidence_enabled else 1) or type(episode.get('joint_success')) is not bool:
            raise ValueError('Invalid reward or missing explicit joint judgment')
        capped = reward_version == 'rca-evidence-v6' and episode['joint_success'] and (episode.get('major_claims') or 0) > 0
        if capped and reward > .4 + 1e-6:
            raise ValueError('Fabricated success above the v6 cap')
        floor = success_floor - (min(.3, .1 * (episode.get('major_claims') or 0)) if reward_version == 'rca-evidence-v7' else 0)
        if episode['joint_success'] and not capped and reward < (floor - 1e-7 if evidence_enabled else .8):
            raise ValueError('Joint success reward violates success-first contract')
        if not episode['joint_success'] and reward > (.350001 if evidence_enabled else .300001):
            raise ValueError('Non-success reward violates success-first contract')
        if evidence_enabled:
            validate_evidence_reward(group, episode)
        turns = episode.get('turns', [])
        if not turns:
            raise ValueError('No assistant turns')
        completion_tokens = 0
        for turn in turns:
            prompt = turn.get('prompt_token_ids')
            completion = turn.get('completion_token_ids')
            old = turn.get('old_logprobs')
            if not isinstance(prompt, list) or not prompt or not isinstance(completion, list) or not completion:
                raise ValueError('Missing full prompt/completion token IDs')
            if any(type(token) is not int or token < 0 for token in prompt + completion):
                raise ValueError('Invalid token IDs')
            if not isinstance(old, list) or len(old) != len(completion):
                raise ValueError('Every sampled completion token requires its old log probability')
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   or v > 1e-5 for v in old):
                raise ValueError('Invalid old sampled-token log probabilities')
            completion_tokens += len(completion)
        if type(episode.get('generated_tokens')) is not int or episode['generated_tokens'] != completion_tokens:
            raise ValueError('Full episode usage differs from retained completion tokens; do not truncate')
    return episodes


def validate_evidence_reward(group, episode):
    from rca_lab.train.grpo import EpisodeOutcome, trajectory_rewards
    failure = episode.get('model_failure')
    if type(failure) is not bool:
        raise ValueError('Evidence reward requires explicit model_failure')
    if not failure:
        if episode.get('evidence_judge_status') != 'completed':
            raise ValueError('Unresolved evidence judge blocks entire group')
        digest = episode.get('evidence_artifact_sha256', '')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Missing evidence artifact identity')
        artifact = episode.get('evidence_artifact')
        if not isinstance(artifact, dict):
            raise ValueError('Missing embedded evidence artifact')
        encoded = json.dumps(artifact, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError('Evidence artifact digest mismatch')
        from rca_lab.mcp.rl_reward import evidence_cache_valid
        # 2026-10-15: judge recorded per group (Claude Opus 5.5 while Codex is quota-limited); default = original Codex judge
        if not evidence_cache_valid(artifact, artifact.get('item', {}), model=group.get('judge_model', 'gpt-6.1-sol'), provider=group.get('judge_provider', 'codex')):
            raise ValueError('Invalid evidence judge provenance or references')
        verdict = artifact['verdict']
        if any(episode.get(key) != verdict.get(key) for key in ('path_score', 'hallucination_score')):
            raise ValueError('Evidence scores disagree with embedded judge artifact')
    if group.get('reward_version') in ('rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7'):
        validate_causal_depth(group, episode)
    if group.get('reward_version') in ('rca-evidence-v6', 'rca-evidence-v7') and not failure:
        majors = sum(1 for c in episode['evidence_artifact']['verdict'].get('unsupported_or_fabricated_claims') or [] if c.get('severity') == 'major')
        if episode.get('major_claims') != majors:
            raise ValueError('major_claims disagrees with embedded evidence judge artifact')
    outcome = EpisodeOutcome(
        judge_status=episode['judge_status'],
        root_correct=episode.get('root_precision') == 1 and episode.get('root_recall') == 1,
        mechanism_correct=episode.get('mechanism_score') == 1,
        generated_tokens=episode['generated_tokens'],
        investigation_turns=len(episode.get('turns', [])),
        root_precision=episode['root_precision'], root_recall=episode['root_recall'],
        model_failure=failure, mechanism_score=episode['mechanism_score'],
        evidence_judge_status=episode.get('evidence_judge_status'),
        path_score=episode.get('path_score'), hallucination_score=episode.get('hallucination_score'),
        causal_depth=episode.get('causal_depth'), major_claims=episode.get('major_claims'),
    )
    if episode['joint_success'] != (outcome.root_correct and outcome.mechanism_correct and not failure):
        raise ValueError('Joint judgment disagrees with correctness fields')
    expected = trajectory_rewards(
        [outcome], token_budget=group['reference_tokens'], turn_budget=group['reference_turns'],
        efficiency_weight=group['efficiency_coefficient'], path_weight=group['path_weight'],
        hallucination_weight=group['hallucination_weight'],
        success_hallucination_weight=group.get('success_hallucination_weight'),
        **({'partial_root_weight': group['partial_root_weight'], 'partial_mechanism_weight': group['partial_mechanism_weight']}
           if group.get('reward_version') in ('rca-evidence-v4', 'rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') else {}),
        **({'depth_weight': group['depth_weight']} if group.get('reward_version') in ('rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') else {}),
        **({'fabricated_success_cap': group['fabricated_success_cap']} if group.get('reward_version') == 'rca-evidence-v6' else {}),
        **({'major_claim_penalty': group['major_claim_penalty']} if group.get('reward_version') == 'rca-evidence-v7' else {}),
    ).item()
    if not math.isclose(episode['reward'], expected, abs_tol=1e-6, rel_tol=0):
        raise ValueError('Stored reward disagrees with evidence reward contract')


def validate_causal_depth(group, episode):
    """v5: a joint success carries the causal-depth judge artifact it was scored with; nothing else carries a depth."""
    if not episode['joint_success']:
        if episode.get('causal_depth') is not None:
            raise ValueError('Causal depth on a non-success episode')
        return
    digest = episode.get('causal_depth_artifact_sha256', '')
    artifact = episode.get('causal_depth_artifact')
    if not isinstance(artifact, dict) or not isinstance(digest, str) or len(digest) != 64:
        raise ValueError('Missing causal depth artifact')
    encoded = json.dumps(artifact, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    if hashlib.sha256(encoded).hexdigest() != digest:
        raise ValueError('Causal depth artifact digest mismatch')
    from rca_lab.mcp.causal_depth import causal_depth_cache_valid
    if not causal_depth_cache_valid(artifact, artifact.get('item', {}), model=group.get('judge_model'), provider=group.get('judge_provider')):
        raise ValueError('Invalid causal depth judge provenance or references')
    # the depth judge must have read the same trajectory the evidence judge read
    same = lambda item: {k: v for k, v in item.items() if k != 'rubric_version'}
    if same(artifact['item']) != same(episode['evidence_artifact']['item']):
        raise ValueError('Causal depth judged a different trajectory')
    if episode.get('causal_depth') != artifact['verdict']['depth']:
        raise ValueError('Causal depth disagrees with embedded judge artifact')


def validate_training_split(group, preparation_path, split_override=None):
    import yaml
    preparation = json.loads(Path(preparation_path).read_text())
    source = Path(split_override or preparation['split_source'])
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != preparation['split_sha256'] or group.get('split_sha256') != digest:
        raise ValueError('Rollout/preparation/source split SHA mismatch')
    split = yaml.safe_load(raw)
    sets = {}
    # 2026-10-15: split v3 (train53/validation10/sealed12); counts come from the preparation record, v2 default 43/6/12
    counts = preparation.get('expected_counts', {'train_cases': 43, 'validation_cases': 6, 'sealed_cases': 12})
    if counts['sealed_cases'] != 12:
        raise ValueError('Sealed set must stay the original 12 cases')
    for key, source_key, count in [('train_cases', 'train', counts['train_cases']),
                                    ('validation_cases', 'validation', counts['validation_cases']),
                                    ('sealed_cases', 'sealed_eval', 12)]:
        items = preparation[key]
        sets[key] = set(items)
        if len(items) != count or len(sets[key]) != count or sets[key] != set(split[source_key]):
            raise ValueError(f'{key} cardinality/identity mismatch')
    train, validation, sealed = (sets[k] for k in ('train_cases', 'validation_cases', 'sealed_cases'))
    if train & validation or train & sealed or validation & sealed:
        raise ValueError('Train/validation/sealed overlap')
    if group.get('case_id') not in train or group.get('case_id') in preparation['excluded_cases']:
        raise ValueError('Only allowlisted train cases can contribute optimizer updates')
    return digest


def validate_checkpoint(checkpoint, expected_version):
    metadata = json.loads((Path(checkpoint) / 'download-complete.json').read_text())
    if metadata.get('repo') != 'Intel/Qwen3.8-Flash-Next-W4A16-AutoRound':
        raise ValueError('Wrong checkpoint repository')
    if 'base-' + metadata.get('revision', '') != expected_version:
        raise ValueError('Checkpoint metadata does not match rollout base revision')
    if not isinstance(metadata.get('bytes'), int) or metadata['bytes'] <= 0:
        raise ValueError('Checkpoint download completion metadata missing byte count')
    return metadata


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False))
    temporary.replace(path)


def completion_logps(policy, turn, device, chunk_size):
    prompt, completion = turn['prompt_token_ids'], turn['completion_token_ids']
    ids = torch.tensor([prompt + completion], dtype=torch.long, device=device)
    result = policy.log_probs(ids, chunk_size=chunk_size, start_position=len(prompt) - 1)
    if result.shape != (1, len(completion)):
        raise ValueError('Policy omitted completion tokens')
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def probability_profile(args):
    return {'probability_backend': args.probability_backend,
            'serving_hyperconnections': args.serving_hyperconnections,
            'base_policy_version': args.expected_policy_version}


def profile_digest(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_reviewed_admission(args, group, episodes, group_sha256):
    """CPU-only verification; must finish before loading policy or optimizer.

    This runner is initial-base only, so proximal and fixed reference numerically
    coincide. Their independent snapshots and roles remain explicit.
    """
    from rca_lab.train.grpo_drift import build_drift_report
    if not all((args.reviewed_admission, args.proximal_cache, args.drift_report)):
        raise ValueError('Reviewed correction requires admission, proximal cache and drift report')
    if len(episodes) != 4 or not group.get('serving_backend_sha256'):
        raise ValueError('Reviewed pilot requires four complete fingerprinted rollouts')
    admission = json.loads(Path(args.reviewed_admission).read_text())
    if admission.get('schema_version') != 1 or admission.get('decision') != 'approved_one_update':
        raise ValueError('No explicit one-update reviewed approval')
    for field in ('reviewer', 'rationale'):
        if not isinstance(admission.get(field), str) or not admission[field].strip():
            raise ValueError(f'Missing admission {field}')
    for field in ('systematic_drift', 'context_growth', 'serving_controls'):
        value = admission.get('reviews', {}).get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'Missing explicit {field} review')
    identities = {'group_sha256': group_sha256,
                  'serving_backend_sha256': group['serving_backend_sha256'],
                  'loader_sha256': sha256_file(Path(__file__).with_name('flash_train.py')),
                  'drift_report_sha256': sha256_file(args.drift_report),
                  'proximal_cache_sha256': sha256_file(args.proximal_cache),
                  'probability_profile_sha256': profile_digest(probability_profile(args))}
    for name, value in identities.items():
        if admission.get(name) != value:
            raise ValueError(f'Stale/mismatched admission {name}')
    if admission.get('importance_cap') != 2.0:
        raise ValueError('Reviewed pilot requires explicit importance cap 2')
    cache = torch.load(args.proximal_cache, map_location='cpu', weights_only=True)
    for key in ('group_sha256', 'serving_backend_sha256', 'loader_sha256'):
        if cache.get(key) != identities[key]:
            raise ValueError(f'Stale proximal cache {key}')
    for key, value in probability_profile(args).items():
        if cache.get(key) != value:
            raise ValueError(f'Proximal cache probability profile mismatch: {key}')
    if ('probability_profile_sha256' in cache and
            cache['probability_profile_sha256'] != identities['probability_profile_sha256']):
        raise ValueError('Proximal cache profile hash mismatch')
    snapshots = {}
    for key in ('proximal_logps', 'reference_logps'):
        rows = cache.get(key)
        if not isinstance(rows, list) or len(rows) != len(episodes):
            raise ValueError(f'Incomplete {key} episode coverage')
        snapshots[key] = []
        for episode, turns in zip(episodes, rows):
            if not isinstance(turns, list) or len(turns) != len(episode['turns']):
                raise ValueError(f'Incomplete {key} turn coverage')
            copied = []
            for turn, values in zip(episode['turns'], turns):
                if (not isinstance(values, torch.Tensor) or not values.is_floating_point()
                        or values.shape != (1, len(turn['completion_token_ids']))
                        or not torch.isfinite(values).all() or (values > 1e-5).any()):
                    raise ValueError(f'Invalid {key} token coverage/values')
                copied.append(values.detach().float().clone())
            snapshots[key].append(copied)
    for prox, refs in zip(snapshots['proximal_logps'], snapshots['reference_logps']):
        if any(not torch.equal(p, r) for p, r in zip(prox, refs)):
            raise ValueError('Initial-base fixed reference differs from initial proximal snapshot')
    samples = [{'episode_id': ep['episode_id'], 'turns': [
        {'prompt_tokens': len(turn['prompt_token_ids']),
         'completion_token_ids': turn['completion_token_ids'],
         'old_logprobs': turn['old_logprobs'],
         'proximal_logprobs': snapshots['proximal_logps'][i][j].reshape(-1).tolist()}
        for j, turn in enumerate(ep['turns'])]} for i, ep in enumerate(episodes)]
    recomputed = build_drift_report(samples, importance_cap=2.)
    drift = json.loads(Path(args.drift_report).read_text())
    for key in ('experimental_budgets', 'all_experimental_budgets_pass', 'context_bins'):
        if drift.get(key) != recomputed[key]:
            raise ValueError(f'Drift report inconsistent with cached group: {key}')
    if not recomputed['all_experimental_budgets_pass']:
        raise ValueError('Residual drift exceeds experimental budgets; approval cannot override')
    saved_episodes = drift.get('episodes', [])
    if len(saved_episodes) != len(episodes):
        raise ValueError('Drift report episode coverage incomplete')
    controls_verified = 0
    for saved, actual in zip(saved_episodes, recomputed['episodes']):
        for key in ('episode_id', 'summary', 'experimental_budgets_pass'):
            if saved.get(key) != actual[key]:
                raise ValueError(f'Drift episode mismatch: {key}')
        if len(saved.get('turns', [])) != len(actual['turns']):
            raise ValueError('Drift report turn coverage incomplete')
        required_control_indices = {0, len(actual['turns']) // 2, len(actual['turns']) - 1}
        verified_control_indices = set()
        for recorded, calculated in zip(saved['turns'], actual['turns']):
            if any(recorded.get(k) != v for k, v in calculated.items()):
                raise ValueError('Drift report turn statistics mismatch')
            control = recorded.get('serving_control')
            if control:
                if control.get('token_identity_verified') is not True:
                    raise ValueError('Serving control sampled token identity unverified')
                controls_verified += 1
                verified_control_indices.add(recorded['turn_index'])
        if not required_control_indices.issubset(verified_control_indices):
            raise ValueError('Missing early/mid/last verified controls for an episode')
    if not controls_verified or drift.get('controls_supplied') != controls_verified:
        raise ValueError('Missing verified serving controls for residual-drift review')
    return snapshots, {'identities': identities, 'reviewer': admission['reviewer'],
                       'rationale': admission['rationale'], 'reviews': admission['reviews'],
                       'controls_verified': controls_verified, 'importance_cap': 2.0}


def gradient_identity(args, group_sha256):
    return {
        'group_sha256': group_sha256,
        'admission_sha256': sha256_file(args.reviewed_admission),
        'proximal_cache_sha256': sha256_file(args.proximal_cache),
        'drift_report_sha256': sha256_file(args.drift_report),
        'loader_sha256': sha256_file(Path(__file__).with_name('flash_train.py')),
        'runner_sha256': sha256_file(__file__),
        'profile': probability_profile(args),
        'training': {k: getattr(args, k) for k in (
            'learning_rate', 'clip_epsilon', 'kl_beta', 'std_floor',
            'max_grad_norm', 'expert_rank', 'dense_rank', 'logit_chunk_size')},
    }


def save_gradient_prefix(path, policy, identity, completed, totals, parity):
    """Only pre-optimizer, post-backward complete turns may be checkpointed."""
    state = {'schema_version': 1, 'optimizer_steps': 0, 'identity': identity,
             'completed_prefix': list(completed), 'totals': list(totals),
             'parity': dict(parity), 'gradients': {
                 name: None if p.grad is None else p.grad.detach().cpu().clone()
                 for name, p in policy.named_parameters() if p.requires_grad}}
    if not all(g is None or torch.isfinite(g).all() for g in state['gradients'].values()):
        raise ValueError('Nonfinite accumulated gradients; preserve previous checkpoint')
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    torch.save(state, temporary)
    temporary.replace(path)
    return sha256_file(path)


def restore_gradient_prefix(path, expected_sha256, policy, identity, episodes):
    if not expected_sha256 or sha256_file(path) != expected_sha256:
        raise ValueError('Gradient checkpoint SHA mismatch')
    state = torch.load(path, map_location='cpu', weights_only=True)
    if state.get('schema_version') != 1 or state.get('optimizer_steps') != 0 or state.get('identity') != identity:
        raise ValueError('Gradient checkpoint identity mismatch')
    expected = [(i, j) for i, ep in enumerate(episodes) for j in range(len(ep['turns']))]
    completed = state.get('completed_prefix')
    if not isinstance(completed, list) or not completed or len(completed) > len(expected) or completed != expected[:len(completed)]:
        raise ValueError('Gradient checkpoint must contain an exact complete-turn prefix')
    tokens = sum(len(episodes[i]['turns'][j]['completion_token_ids']) for i, j in completed)
    parity = state.get('parity', {})
    error = parity.get('max_absolute_logprob_error')
    if (parity.get('turns_checked') != len(completed) or parity.get('sampled_tokens_checked') != tokens
            or not isinstance(error, (float, int)) or not math.isfinite(error) or not 0 <= error <= 1e-4):
        raise ValueError('Gradient checkpoint parity coverage mismatch')
    totals = state.get('totals')
    if not isinstance(totals, list) or len(totals) != 3 or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in totals):
        raise ValueError('Invalid gradient checkpoint totals')
    parameters = {name: p for name, p in policy.named_parameters() if p.requires_grad}
    gradients = state.get('gradients', {})
    if gradients.keys() != parameters.keys():
        raise ValueError('Gradient checkpoint parameter coverage mismatch')
    # Validate the whole artifact before mutating any GPU gradient.
    for name, gradient in gradients.items():
        p = parameters[name]
        if gradient is not None and (not isinstance(gradient, torch.Tensor)
                or gradient.shape != p.shape or gradient.dtype != p.dtype
                or not torch.isfinite(gradient).all()):
            raise ValueError('Invalid accumulated gradient: ' + name)
    for name, gradient in gradients.items():
        parameters[name].grad = None if gradient is None else gradient.to(parameters[name].device).clone()
    return completed, totals, parity


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--preparation', default=str(Path(__file__).resolve().parent.parent / 'preparation.json'))
    ap.add_argument('--split-source', help='Relocated copy of the exact SHA-verified split YAML')
    ap.add_argument('--repo-src', required=True, help='Path containing freshly deployed rca_lab package')
    ap.add_argument('--checkpoint', required=True)
    ap.add_argument('--group', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--expected-policy-version', default=BASE_REVISION)
    ap.add_argument('--device', default='cuda:0')
    ap.add_argument('--gradient-checkpoint-every', type=int, default=0, help='Opt-in atomic accumulation save cadence; 0 disables')
    ap.add_argument('--resume-gradients', help='Pre-optimizer complete-turn accumulation checkpoint')
    ap.add_argument('--resume-gradients-sha256', help='SHA from the previous progress report')
    ap.add_argument('--learning-rate', type=float, default=1e-6)
    ap.add_argument('--clip-epsilon', type=float, default=.2)
    ap.add_argument('--kl-beta', type=float, default=.02)
    ap.add_argument('--std-floor', type=float, default=1.)
    ap.add_argument('--max-grad-norm', type=float, default=1.)
    ap.add_argument('--parity-max-error', type=float, default=.1)
    ap.add_argument('--admission-mode', choices=('strict-parity', 'residual-drift-audit', 'reviewed-correction'), default='strict-parity')
    ap.add_argument('--reviewed-admission', help='Explicit review bound to exact cache/report/source hashes')
    ap.add_argument('--proximal-cache', help='Frozen CPU snapshots from residual-drift-audit')
    ap.add_argument('--drift-report', help='Exact residual-drift report reviewed for one update')
    ap.add_argument('--serving-controls', help='Complete early/mid/late serving self-control JSON')
    ap.add_argument('--logit-chunk-size', type=int, default=16)
    ap.add_argument('--probability-backend', choices=('native', 'fla-matched'), default='native')
    ap.add_argument('--serving-hyperconnections', action='store_true')
    ap.add_argument('--expert-rank', type=int, default=2)
    ap.add_argument('--dense-rank', type=int, default=16)
    args = ap.parse_args(argv)
    sys.path.insert(0, str(Path(args.repo_src).resolve()))
    from rca_lab.train.grpo import group_advantages, grpo_loss
    from flash_train import load_policy
    for name in ('learning_rate', 'max_grad_norm', 'parity_max_error'):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise ValueError(f'{name} must be finite positive')
    if args.gradient_checkpoint_every < 0 or ((args.resume_gradients is None) != (args.resume_gradients_sha256 is None)):
        raise ValueError('Invalid gradient checkpoint options')
    if (args.gradient_checkpoint_every or args.resume_gradients) and args.admission_mode != 'reviewed-correction':
        raise ValueError('Gradient resume requires reviewed-correction mode')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'adapter.pt').exists() or (output / 'result.json').exists():
        raise ValueError('Refusing to overwrite an optimizer admission result; use a new output directory')
    started = time.time()
    report = {'status': 'starting', 'optimizer_steps': 0, 'gemma_touched': False,
              'arguments': vars(args), 'limitations': [
                  'One complete-group base-policy update only; multi-update on-policy rollout not implemented',
                  'No teacher or reference answers are fed into the actor',
              ]}
    try:
        raw = Path(args.group).read_bytes()
        group = json.loads(raw)
        episodes = validate_group(group, args.expected_policy_version)
        report['split_sha256'] = validate_training_split(group, args.preparation, args.split_source)
        report['checkpoint_metadata'] = validate_checkpoint(args.checkpoint, args.expected_policy_version)
        report.update(group_id=group['group_id'], case_id=group['case_id'],
                      group_sha256=hashlib.sha256(raw).hexdigest(),
                      reward_version=group.get('reward_version', 'rca-v1'),
                      serving_backend=group.get('serving_backend'),
                      serving_backend_sha256=group.get('serving_backend_sha256'),
                      evidence_judge_model='gpt-6.1-sol' if group.get('reward_version') in ('rca-evidence-v2', 'rca-evidence-v3', 'rca-evidence-v4', 'rca-evidence-v5', 'rca-evidence-v6', 'rca-evidence-v7') else None)
        rewards = torch.tensor([e['reward'] for e in episodes], dtype=torch.float32)
        advantages = group_advantages(rewards, torch.zeros(len(episodes), dtype=torch.long),
                                      std_floor=args.std_floor)
        report.update(rewards=rewards.tolist(), advantages=advantages.tolist())
        if not advantages.ne(0).any():
            raise ValueError('Flat-reward group has no policy learning signal; collect another complete group')
        corrected = args.admission_mode == 'reviewed-correction'
        reviewed_snapshots = None
        if corrected:
            reviewed_snapshots, report['reviewed_admission'] = validate_reviewed_admission(
                args, group, episodes, report['group_sha256'])
        elif any((args.reviewed_admission, args.proximal_cache, args.drift_report)):
            raise ValueError('Review artifacts require explicit reviewed-correction mode')
        torch.manual_seed(20261014)
        torch.set_num_threads(8)
        if args.probability_backend == 'fla-matched':
            backend = group.get('serving_backend') or {}
            if backend.get('gdn_prefill_backend') != 'triton':
                raise ValueError('FLA-matched admission requires fresh fingerprinted Triton rollouts')
            from flash_train import enable_serving_convolution
            enable_serving_convolution()
        report['status'] = 'loading_model'
        write_json(output / 'progress.json', report)
        print(json.dumps({'event': 'loading_model', 'checkpoint': args.checkpoint}), flush=True)
        policy = load_policy(args.checkpoint, device=args.device,
                             expert_rank=args.expert_rank, dense_rank=args.dense_rank)
        if args.probability_backend == 'fla-matched':
            from flash_train import enable_serving_delta_rule
            enable_serving_delta_rule(policy, backend='fla')
        if args.serving_hyperconnections:
            from flash_train import enable_serving_hyperconnections
            enable_serving_hyperconnections(policy)
        # Keep checkpointing active without introducing train/eval sampling differences.
        policy.train()
        if getattr(policy.backbone.config, 'attention_dropout', None) != 0:
            raise ValueError('Native attention_dropout must explicitly be zero for parity')
        for module in policy.modules():
            if isinstance(module, torch.nn.Dropout) and module.p != 0:
                raise ValueError('Nonzero dropout invalidates deterministic rollout parity')
        for name, parameter in policy.named_parameters():
            if name.endswith('lora_B') and parameter.detach().ne(0).any():
                raise ValueError('Initial-base runner cannot accept an already updated adapter')
        adapter_params = policy.adapter_parameters()
        if not adapter_params:
            raise ValueError('No trainable adapter parameters')
        parity_max, parity_sum, parity_tokens = 0., 0., 0
        references = reviewed_snapshots['reference_logps'] if corrected else []
        proximals = reviewed_snapshots['proximal_logps'] if corrected else []
        # All turns must pass BEFORE any backwards or optimizer step.
        report['status'] = 'checking_behavior_policy_parity'
        for episode_index, episode in enumerate([] if corrected else episodes):
            episode_refs = []
            for turn_index, turn in enumerate(episode['turns']):
                report['current_episode'] = episode_index
                report['current_turn'] = turn_index
                write_json(output / 'progress.json', report)
                with torch.no_grad(), policy.disable_adapter():
                    reference = completion_logps(policy, turn, args.device, args.logit_chunk_size)
                old = torch.tensor(turn['old_logprobs'], dtype=torch.float32,
                                   device=args.device)[None, :]
                error = (reference.float() - old).abs()
                if not torch.isfinite(error).all():
                    raise ValueError('Nonfinite base/rollout parity')
                parity_max = max(parity_max, error.max().item())
                parity_sum += error.sum().item()
                parity_tokens += error.numel()
                episode_refs.append(reference.detach().cpu())
                report['parity'] = {'max_absolute_logprob_error': parity_max,
                                    'mean_absolute_logprob_error': parity_sum / parity_tokens,
                                    'sampled_tokens_checked': parity_tokens,
                                    'threshold': args.parity_max_error}
                write_json(output / 'progress.json', report)
                if args.admission_mode == 'strict-parity' and parity_max > args.parity_max_error:
                    raise ValueError(
                        f'Training/rollout probability parity exceeds threshold at '
                        f'episode {episode_index}, turn {turn_index}; no update allowed'
                    )
            references.append(episode_refs)
        if args.admission_mode == 'residual-drift-audit':
            from rca_lab.train.grpo_drift import build_drift_report
            control_rows = None
            if args.serving_controls:
                controls = json.loads(Path(args.serving_controls).read_text())
                if controls.get('group_sha256') != report['group_sha256'] or controls.get('status') != 'complete':
                    raise ValueError('Serving self-control must cover the exact completed group')
                if controls.get('serving_backend_sha256') != group.get('serving_backend_sha256'):
                    raise ValueError('Serving self-control backend identity mismatch')
                control_rows = controls['controls']
            samples = [
                {'episode_id': episode['episode_id'], 'turns': [
                    {'prompt_tokens': len(turn['prompt_token_ids']),
                     'completion_token_ids': turn['completion_token_ids'],
                     'old_logprobs': turn['old_logprobs'],
                     'proximal_logprobs': references[i][j].reshape(-1).tolist()}
                    for j, turn in enumerate(episode['turns'])]}
                for i, episode in enumerate(episodes)]
            drift = build_drift_report(samples, controls=control_rows, importance_cap=2.)
            write_json(output / 'drift-report.json', drift)
            loader = Path(__file__).with_name('flash_train.py')
            cache = {'group_sha256': report['group_sha256'],
                     'serving_backend_sha256': group.get('serving_backend_sha256'),
                     'loader_sha256': hashlib.sha256(loader.read_bytes()).hexdigest(),
                     'probability_backend': args.probability_backend,
                     'serving_hyperconnections': args.serving_hyperconnections,
                     'base_policy_version': args.expected_policy_version,
                     'probability_profile_sha256': profile_digest(probability_profile(args)),
                     'proximal_logps': references,
                     'reference_logps': references,
                     'note': 'Initial zero-adapter base only: fixed reference and proximal coincide'}
            torch.save(cache, output / 'proximal-cache.pt')
            report.update(status='drift_audit_complete_requires_review', optimizer_steps=0,
                          drift_report=str(output / 'drift-report.json'),
                          proximal_cache=str(output / 'proximal-cache.pt'))
            return
        if not corrected and parity_max > args.parity_max_error:
            raise ValueError('Training/rollout probability parity exceeds threshold; no update allowed')
        # Verify the exact differentiable forward used for each loss. No optimizer
        # exists until every turn passes; gradients cannot mutate policy weights.
        report['status'] = 'accumulating_complete_group_gradients'
        report['initial_proximal_parity'] = {'max_absolute_logprob_error': 0.,
                                            'sampled_tokens_checked': 0,
                                            'turns_checked': 0}
        write_json(output / 'progress.json', report)
        for parameter in adapter_params:
            parameter.grad = None
        loss_sum, kl_sum, clips_sum = 0., 0., 0.
        completed_prefix = []
        accumulation_identity = gradient_identity(args, report['group_sha256']) if corrected else None
        if args.resume_gradients:
            completed_prefix, totals, parity = restore_gradient_prefix(
                args.resume_gradients, args.resume_gradients_sha256, policy, accumulation_identity, episodes)
            loss_sum, kl_sum, clips_sum = totals
            report['initial_proximal_parity'] = parity
            report['initial_proximal_parity_max_error'] = parity['max_absolute_logprob_error']
            report['resumed_turns'] = len(completed_prefix)
        completed_set = set(completed_prefix)
        for episode_index, episode in enumerate(episodes):
            total_tokens = episode['generated_tokens']
            for turn_index, turn in enumerate(episode['turns']):
                if (episode_index, turn_index) in completed_set:
                    continue
                report.update(current_episode=episode_index, current_turn=turn_index)
                current = completion_logps(policy, turn, args.device, args.logit_chunk_size)
                if corrected:
                    proximal = proximals[episode_index][turn_index].to(args.device)
                    with torch.no_grad():
                        error = (current.detach().float() - proximal).abs()
                        if not torch.isfinite(error).all():
                            raise ValueError('Nonfinite current/proximal parity')
                        stats = report['initial_proximal_parity']
                        stats['max_absolute_logprob_error'] = max(
                            stats['max_absolute_logprob_error'], error.max().item())
                        stats['sampled_tokens_checked'] += error.numel()
                        stats['turns_checked'] += 1
                        report['initial_proximal_parity_max_error'] = stats['max_absolute_logprob_error']
                        write_json(output / 'progress.json', report)
                        if stats['max_absolute_logprob_error'] > 1e-4:
                            raise ValueError('Current/proximal snapshot mismatch; no optimizer allowed')
                old = torch.tensor(turn['old_logprobs'], dtype=torch.float32,
                                   device=args.device)[None, :]
                reference = references[episode_index][turn_index].to(args.device)
                mask = torch.ones_like(current, dtype=torch.bool)
                advantage = advantages[episode_index:episode_index + 1].to(args.device)
                if corrected:
                    from rca_lab.train.grpo_correction import corrected_grpo_loss
                    proximal = proximals[episode_index][turn_index].to(args.device)
                    result = corrected_grpo_loss(current, proximal, old, reference, mask, advantage,
                                                 importance_cap=2., clip_epsilon=args.clip_epsilon,
                                                 kl_beta=args.kl_beta)
                    sampled_regularizer = result.sampled_reference_regularizer
                    clip_fraction = result.diagnostics['ppo_clip_fraction']
                else:
                    result = grpo_loss(current, old, reference, mask, advantage,
                                       clip_epsilon=args.clip_epsilon, kl_beta=args.kl_beta)
                    sampled_regularizer = result.reference_kl
                    clip_fraction = result.clip_fraction
                # Exact complete-episode average, followed by equal episode average.
                weight = current.numel() / total_tokens / len(episodes)
                (result.loss * weight).backward()
                loss_sum += result.loss.detach().item() * weight
                kl_sum += sampled_regularizer.item() * weight
                clips_sum += clip_fraction.item() * weight
                del current, result
                completed_prefix.append((episode_index, turn_index))
                if args.gradient_checkpoint_every and len(completed_prefix) % args.gradient_checkpoint_every == 0:
                    checkpoint_path = output / 'gradient-prefix.pt'
                    digest = save_gradient_prefix(
                        checkpoint_path, policy, accumulation_identity, completed_prefix,
                        (loss_sum, kl_sum, clips_sum), report['initial_proximal_parity'])
                    report['gradient_checkpoint'] = {'path': str(checkpoint_path),
                        'sha256': digest, 'completed_turns': len(completed_prefix)}
                report['completed_backward_turns'] = len(completed_prefix)
                write_json(output / 'progress.json', report)
                print(json.dumps({'event': 'turn_backward', 'episode': episode_index,
                                  'turn': turn_index, 'weight': weight}), flush=True)
        scopes = {}
        for name, parameter in policy.named_parameters():
            if not parameter.requires_grad:
                continue
            scope = ('experts' if '.mlp.experts.' in name else
                     'shared_mlp' if '.shared_expert.' in name else
                     'linear_attention' if '.linear_attn.' in name else
                     'full_attention' if '.self_attn.' in name else 'unexpected')
            stats = scopes.setdefault(scope, {'parameters': 0, 'nonzero_gradient_tensors': 0,
                                               'finite': True})
            stats['parameters'] += parameter.numel()
            if parameter.grad is not None:
                stats['finite'] &= bool(torch.isfinite(parameter.grad).all())
                stats['nonzero_gradient_tensors'] += int(parameter.grad.ne(0).any())
        report['gradient_scopes'] = scopes
        if set(scopes) != {'experts', 'shared_mlp', 'linear_attention', 'full_attention'}:
            raise ValueError('Unexpected or missing trainable scope')
        if not all(s['finite'] and s['nonzero_gradient_tensors'] > 0 for s in scopes.values()):
            raise ValueError('Missing/nonfinite gradient in a required training scope')
        gradients = [p.grad for p in adapter_params if p.grad is not None]
        if not gradients or not all(torch.isfinite(g).all() for g in gradients):
            raise ValueError('Missing or nonfinite adapter gradients')
        nonzero = sum(int(g.ne(0).any()) for g in gradients)
        if not nonzero:
            raise ValueError('No nonzero adapter gradients')
        norm = torch.nn.utils.clip_grad_norm_(adapter_params, args.max_grad_norm,
                                              error_if_nonfinite=True)
        # Copy only a bounded set of nonzero-gradient elements to verify a real update.
        checks = []
        for parameter in adapter_params:
            if parameter.grad is not None:
                indices = parameter.grad.reshape(-1).ne(0).nonzero().flatten()[:16]
                if indices.numel():
                    checks.append((parameter, indices, parameter.detach().reshape(-1)[indices].clone()))
                    if len(checks) >= 64:
                        break
        optimizer = torch.optim.AdamW(adapter_params, lr=args.learning_rate, weight_decay=0.)
        optimizer.step()
        report['optimizer_steps'] = 1
        if not all(torch.isfinite(p).all() for p in adapter_params):
            raise ValueError('Nonfinite adapter parameters after update; do not publish checkpoint')
        changed = sum(int((p.detach().reshape(-1)[indices] != before).any())
                      for p, indices, before in checks)
        if not changed:
            raise ValueError('Optimizer produced no verified adapter change')
        weights = {name: p.detach().cpu() for name, p in policy.named_parameters() if p.requires_grad}
        temp = output / 'adapter.pt.tmp'
        torch.save({'state_dict': weights, 'base_policy_version': args.expected_policy_version,
                    'policy_version': f"{group['group_id']}-step1", 'expert_rank': args.expert_rank,
                    'dense_rank': args.dense_rank, 'optimizer_steps': 1,
                    'group_sha256': report['group_sha256'],
                    'reward_version': report['reward_version']}, temp)
        temp.replace(output / 'adapter.pt')
        report.update(status='passed', loss=loss_sum, sampled_reference_regularizer=kl_sum, clip_fraction=clips_sum,
                      preclip_gradient_norm=float(norm), nonzero_gradient_tensors=nonzero,
                      changed_sampled_tensors=changed,
                      adapter_path=str(output / 'adapter.pt'))
    except Exception as exc:
        # A late-turn mismatch must not leave a reusable partial gradient group.
        if 'adapter_params' in locals():
            for parameter in adapter_params:
                parameter.grad = None
        report.update(status='blocked', error_type=type(exc).__name__, error=str(exc),
                      full_turns_retained=True, truncated=False)
        raise
    finally:
        report['elapsed_seconds'] = time.time() - started
        if torch.cuda.is_available():
            report['peak_gpu_gib'] = torch.cuda.max_memory_allocated() / 2**30
        write_json(output / 'result.json', report)
        print(json.dumps(report, allow_nan=False), flush=True)


if __name__ == '__main__':
    main()
