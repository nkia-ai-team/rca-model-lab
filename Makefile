.PHONY: help setup sync sync-train test lint

help:             ## 타겟 목록
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*##' '{printf "  %-12s %s\n", $$1, $$2}'

setup:            ## 최초 1회 (로컬/데이터 작업용): .env + 의존성
	bash scripts/bootstrap.sh

sync:             ## 의존성 재설치 (데이터 작업용)
	uv sync --extra dev

sync-train:       ## 클라우드 GPU 에서: 학습 스택까지 설치
	uv sync --extra dev --extra train

test:             ## pytest
	uv run pytest -q

lint:             ## ruff
	uv run ruff check src tests
