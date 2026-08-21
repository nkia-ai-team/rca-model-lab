#!/usr/bin/env bash
# 동료 온보딩용: 이 스크립트 하나로 작업 환경을 맞춘다.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/2] .env"
[ -f .env ] || cp .env.example .env

echo "[2/2] uv sync (python 3.12, 데이터 작업용 — 학습 스택은 make sync-train)"
command -v uv >/dev/null || { echo "uv 가 없습니다: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
uv python install 3.12 >/dev/null
uv sync --extra dev

echo
echo "done."
