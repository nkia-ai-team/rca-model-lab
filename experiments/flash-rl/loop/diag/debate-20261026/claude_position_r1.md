# Claude's independent position (written before reading Codex round 1)

## Q1 data
Not OK as an RL signal source, for three separable reasons:
1. Difficulty band. RL with group-relative advantage only learns from cases with 0 < p(joint) < 1 for the current policy.
   RCAEval ob4/tt4: 56–80% of groups all-fail, 0% all-pass; OpenRCA2 ~50% all-fail; Polestar 27–45% all-pass. Most
   rollout compute produces no gradient; dynamic sampling hides this by redrawing, it does not fix it. Two-thirds of the
   slots are public sets that the policy mostly cannot solve.
2. Possibly infeasible / mislabeled public cases. An all-fail case is either beyond the policy or unsolvable from the
   converted tool surface (lost signals in the OpenRCA/RCAEval -> rca-mcp conversion, metric-name mismatch, labels that name
   an injected fault type not visible in our telemetry). Unknown which; must be audited: a strong teacher (Opus 5.5 blind,
   the existing rca-distill pipeline) on a sample of all-fail public train cases. Teacher-unsolvable => drop or fix.
3. Reference/label noise. Scenario metadata descriptions are often inaccurate; the judge reads title+cause, but evidence
   and decisive-evidence terms read descriptions, adding reward noise.
Also: train joint rates by source did not rise across eras (Polestar 0.62/0.66/0.57, OpenRCA2 0.38/0.32/0.27), i.e. the
loop is not even visibly fitting its training distribution — confounded by case selection, needs a fixed-set re-roll.

## Q2 per-turn judging
Not required, and LLM-judging every turn would cost ~1000 judge calls per 6-group update (too slow, and noisier than the
outcome judge). Better, in order:
- First confirm credit assignment is the bottleneck: evidence so far says the policy barely moves at all (0.01 nat/token),
  which is a signal-volume/consistency problem, not a credit-assignment one.
- Cheap deterministic process signal from tool traces: we already have per-episode "decisive evidence reached/cited"; the
  turn at which the trajectory first observes a decisive item (observed_refs ∩ golden decisive refs) is computable without
  an LLM. Use it for turn-level shaping (credit to turns up to first decisive observation; efficiency term = turns after it).
- If needed later: value estimation by branching K continuations from intermediate turns (compute, no judge) or a learned
  PRM trained on judged trajectories. Both expensive; not first.

## Q3 is the task "general reasoning"?
No. The base model's general reasoning is adequate (start policy 19/20 on Polestar validation). What is missing is
(a) domain knowledge of failure mechanisms (public mechanism errors: wrong fault class), (b) investigation strategy and
stopping, (c) honest, grounded reporting. RL with outcome reward sharpens behaviors the policy already sometimes produces
(pass@k -> pass@1); it does not add missing knowledge on all-fail cases. Capability expansion needs SFT/distillation from
correct teacher trajectories (rewritten compactly, as in TeleLogs), then RL for selection, honesty and efficiency.

## Proposed changes (ranked)
1. Measurement before more training: fixed-set re-roll of ~20 train cases (start vs current policy, 4 samples) to see if
   training reward moves at all; pass@4 of the start policy on held-out public to bound what RL can reach.
2. Curriculum by difficulty band: train only on cases with 0.1 < p < 0.9 under the current policy (estimated from past
   groups), drop or deprioritize teacher-unsolvable public cases after the audit.
3. SFT warm start from blind-teacher trajectories (Polestar + solvable public), with template/termination checks
   (Gemma SFT collapse lesson), then resume RL.
4. Deterministic decisive-evidence turn shaping (only after 1–3 if credit assignment is shown to matter).
