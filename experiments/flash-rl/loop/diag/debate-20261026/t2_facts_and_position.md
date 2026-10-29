# Topic 2: how to improve the training itself — speed AND performance (2026-10-26)

## New facts since our agreement
1. LEARNING-RATE BUG (verified): AdamW.load_state_dict restores saved param_group hyperparameters. The first update saved
   lr 5e-5, so EVERY update u2..u63 ran at 5e-5 (adapter.pt param_groups lr = 5e-5 in u005, u020, u030, u040, u050, u055,
   u062). The 10-18 raise to 1e-4, both rollback halvings and the 10-24 restore never took effect. Fixed in runtimes
   fast18/fast19 (re-apply configured lr after loading; release records effective_learning_rate); from u64 lr 1e-4 really
   applies. CPU test passed. The diag claim "lr was cut to 2.5e-5" was wrong in effect.
2. Training-time GPU profile (10 min, 1 s samples, during a real update): both GPUs <40% SM 43% of the time, one busy /
   other idle 34% (2-GPU layer split: layers 0-23 cuda:0, 24-47 cuda:1, one sequence at a time), both busy 0%, mean SM ~28%
   per GPU. PCIe ~350 MB/s (not transfer-bound); trainer process ~1.5 CPU cores (likely Python/launch-bound). Peak memory per
   device 70-109 GB in the split (single-GPU data parallel does not fit long episodes).
3. Candidate host-overhead fixes (implemented as switchable patches, benchmark scheduled tonight with py-spy + gradient
   comparison vs a repeated baseline): stack (routed-expert LoRA: one stacked leaf per layer/projection instead of
   torch.stack over 512 expert params per call; grads scattered back exactly), gpustore (tree prefix states on each layer's GPU
   instead of host cat + pageable H2D per child), empty (empty_cache only when >20 GiB idle-reserved), chunk (LM-head logprob
   chunks 256 instead of 16).
4. Time per update: 3-group update ~4 h (rollout ~0.7 h, train 2.5-3.5 h); 6-group update estimated ~8 h.
5. Config now: 6 groups/update (2 Polestar + 4 public), one optimizer step per 2 groups (3 steps/update), group size 4,
   lr 1e-4 (effective from u64), kl_beta 0, importance cap 2, drift gate truncated mass <= 1%.
6. W&B was never used by this loop; a sidecar now back-fills/follows state.json (update-level metrics only).
7. The agreed step-1 learnability diagnostic starts tonight: a direction test (one group step, log-prob change vs advantage at
   lr 1e-4 and 1e-3, same full-forward path before/after, repeat-scoring noise floor).

## Claude's independent position (before reading yours)
Order of work: finish step 1 first; the plan below is conditional on step 1 showing the update path works.

Speed (largest expected gains first):
- S1 host-overhead patches (tonight's benchmark decides which survive; target 1.3-1.6x on training).
- S2 cross-episode pipelining over the 2-GPU split (episodes are independent: cuda:0 runs episode B's first half while cuda:1
  runs episode A's second half) for the remaining 34% alternation; pair short episodes so memory fits; up to ~1.5x more.
- S3 less wasted rollout: difficulty-aware case sampling from per-case outcome history (prefer cases with mixed outcomes,
  periodic retry of hard ones), instead of rolling 10 candidates and dropping flat ones.
- S4 offload rollouts to other hardware so rollout and training overlap (the GB10 box or 104 GPUs) — only if serving there
  is fast enough; 1-step-stale groups are already admitted.
Performance:
- P1 the lr fix itself (2x the lr ever used) + 3 steps/update: re-measure movement and held-out before adding more.
- P2 group size 4 -> 8 on the cases we train: at p=0.1, P(all fail) drops from 66% to 43%; better advantage estimates;
  costs 2x rollouts per case but fewer redraws.
- P3 data: curriculum by estimated difficulty + the agreed audit; drop/fix infeasible public cases.
- P4 verified-teacher SFT for sources where adequate sampling shows negligible success (agreed conditional rule).
- P5 monitor policy entropy and KL to base per update (cheap; detect collapse/no-movement early) and log to W&B.
- P6 reward: decide the severe-fabrication trade-off with the user; keep joint and clean-joint separate.
Things I would NOT do now: per-turn judging, KL penalty (kl_beta>0) before we know the policy moves, changing LoRA layout.
