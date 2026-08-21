"""rca_lab — RCA 특화 모델 파인튜닝 실험 패키지.

하위 패키지는 역할 자리만 잡아둔 상태다. 구현은 각 파트 담당자가 채운다.
  scenarios/  rca-scenario-runner 의 시나리오(ground truth) 읽기
  synth/      교사 모델(claude / codex CLI)로 학습 샘플 합성
  data/       샘플 스키마, 합성→학습셋 정제·분할
  train/      SFT (TRL + PEFT), 이후 GRPO
  eval/       채점기 — SFT 평가와 RL reward 가 공유
"""
