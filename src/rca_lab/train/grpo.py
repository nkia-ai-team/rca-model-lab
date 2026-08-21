"""2단계: GRPO (TRL GRPOTrainer). SFT 검증 후 구현.

설계 메모:
- prompt 셋은 RcaSample.messages[:-1] (system+user), reward 는 ground_truth 대비 채점.
- reward 함수는 rca_lab.eval.reward 에 두고 SFT 평가와 같은 채점기를 공유한다.
"""


def main() -> None:
    raise NotImplementedError("GRPO stage is not implemented yet — see module docstring")


if __name__ == "__main__":
    main()
