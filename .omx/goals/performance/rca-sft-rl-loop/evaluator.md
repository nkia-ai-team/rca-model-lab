# Performance Evaluator: rca-sft-rl-loop

## Objective
RCA student 데이터·학습 계약을 연구 근거에 맞게 수정하고, SFT와 RL을 순차 학습해 봉인 평가에서 비퇴행 성능을 달성한다

## Evaluator Command
```sh
.venv/bin/python scripts/evaluate_training_goal.py --report outputs/goal-eval/final-report.json
```

## Pass/Fail Contract
PASS iff invalid_actions=0, family_overlap=0, baseline/SFT/RL each complete 12 family-disjoint sealed cases x3 runs with format_errors=0 and unsupported_confirmations=0, and both majority_strict_correct and mean_reward are non-regressing across baseline -> SFT -> RL.

This evaluator must exist and produce concrete pass/fail evidence before the performance goal can be completed.
