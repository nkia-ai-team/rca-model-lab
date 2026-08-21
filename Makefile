.PHONY: help setup sync sync-train test lint scenarios synth build sft

help:             ## 타겟 목록
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*##' '{printf "  %-12s %s\n", $$1, $$2}'

setup:            ## 최초 1회 (로컬/데이터 작업용): .env + 의존성
	bash scripts/bootstrap.sh

sync:             ## 의존성만 재설치 (데이터 작업용)
	uv sync --extra dev

sync-train:       ## 클라우드 GPU 에서: 학습 스택까지 설치
	uv sync --extra dev --extra train

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

scenarios:        ## 시나리오 인벤토리 출력
	uv run rca-lab scenarios list

synth:            ## 교사 모델로 SFT 샘플 합성 (TEACHER=claude|codex)
	uv run rca-lab synth run --teacher $(or $(TEACHER),claude) --config configs/synth/default.yaml

build:            ## data/synth → data/processed (train/eval split)
	uv run rca-lab data build

sft:              ## SFT 학습 (CONFIG 로 오버라이드)
	uv run accelerate launch -m rca_lab.train.sft --config $(or $(CONFIG),configs/sft/default.yaml)
