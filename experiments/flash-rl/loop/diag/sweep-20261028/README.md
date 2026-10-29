# lr/optimizer sweep + resume protocol (2026-10-28)

User: "목표설정해놓고 최적 및 최고 학습 상태 찾고 그상태로 빨리 학습해서 rca 성능올려야지" + "하면서 계속 codex astra랑도 토론을해".
Protocol agreed with Codex (gpt-6-astra) in 3 rounds, OPEN: none (debate-20261028/t3_*).

## Target
public_full >= 44/113 (start 32/113), non-negative paired sealed change, guards (validation10 19/20, new13 22/26) preserved.
Matched u062 milestone references first; freeze checkpoint before sealed; never train on public/sealed cases.

## Sweep (running on KT: /home/work/models/sweep-20261028)
- start u062 (runtime requires group behavior = start or parent), fast20 + stack, per-group steps 1 group/step -> 4 steps
- train groups: ob4-1ac8 [T,F,F,F], tt4-72cf [F,T,T,T], f23-r [F,F,T,T], f01-p [T,T,T,T]
- arms: AdamW 1e-3, AdamW 3e-4, Muon 3e-4 (exploratory; continuation-choice comparison, fresh momentum), AdamW 1e-4
- screen (heldout_score.py, run_heldout.sh): 6 held-out mixed groups (u057-u061), identical picks incl. final turn + 3 random;
  PRIMARY = within-group joint-success minus failure dlogp; reward transfer secondary; per-group, leave-one-group-out,
  trajectory drift on 2 all-success probes, scoring noise (start scored twice). Also check effective lr / update norms / accepted tokens.

## Selection
- shortlist <= 2; unresolved -> lower-lr AdamW; transfer alone never establishes improvement.
- fresh confirmation: 2 finalists + u062 on the 6 held-out cases (train split = development panel), 4 episodes each (18 groups),
  matched seeds/decoding, frozen blinded judge.
- provisional pass: >= +3/24 joint vs u062 AND positive net gain on >= 2/6 cases; report per-case diffs and finalist-vs-finalist.
- confirmed joint/guard regression vetoes an arm. Fallback lower-lr AdamW only as budgeted exploration (measurable movement, no regression).

## Resume
stack, 2 groups/step (first 3 updates test the production regime).
- +3 updates: continue if unresolved and optimization healthy; repeat disputed public12/guard cases before declaring deterioration;
  intervene early on numerical instability.
- +6 updates: need positive paired joint evidence at both checkpoints (public12 + development panel), else stop unchanged continuation.
- next change by diagnosis: unstable updates -> 4 groups/step; uninformative groups -> training-only difficulty sampling;
  surrogate-only gains -> reward/credit audit; outcome sampling inadequate -> group size 8 at equal budget; negligible movement -> lr/acceptance audit.
