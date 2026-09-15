#!/usr/bin/env bash
# 평가 케이스 복원 — /data/eval-cases/runbook-eval-case-restore.md의
# 절차(§1~§4)를 스크립트로 옮긴 것. 정본은 runbook이다.
#
#   ./replay/restore.sh /data/eval-cases/case-l1-blackfriday-surge
#
# 끝나면 rca-mcp용 env를 출력한다. 폐기(볼륨 포함):
#   docker compose -f replay/eval-dbs.compose.yml -p <proj> down -v
# 같은 케이스 재평가도 폐기 후 재복원이 원칙(runbook §6 — 볼륨 재사용 금지).
set -euo pipefail

CASE=${1:?사용법: restore.sh <case-dir> [project]}
PROJ=${2:-eval-$(basename "$CASE" | tr -cd 'a-z0-9-' | cut -c1-30)}
DIR=$(cd "$(dirname "$0")" && pwd)
LUCIDA_NEXT=${LUCIDA_NEXT:-$DIR/../../../../lucida-next}  # rca-model-lab 옆 디렉토리
COMPOSE=(docker compose -f "$DIR/eval-dbs.compose.yml" -p "$PROJ")

CH=(clickhouse-client -u lucida --password lucida123)

[ -d "$CASE/data" ] || { echo "케이스 데이터 없음: $CASE/data"; exit 1; }
[ -d "$LUCIDA_NEXT/database/ddl/clickhouse" ] || {
  echo "lucida-next DDL 없음: $LUCIDA_NEXT (LUCIDA_NEXT env로 지정)"; exit 1; }

echo "== [1/5] 격리 DB 기동 (project=$PROJ)"
"${COMPOSE[@]}" up -d
# Docker assigns and reserves free ports atomically. Never probe a port and
# later bind it, or infer endpoints from another compose project's containers.
VM_BIND=$("${COMPOSE[@]}" port victoriametrics 8428)
CH_BIND=$("${COMPOSE[@]}" port clickhouse 8123)
PG_BIND=$("${COMPOSE[@]}" port postgres 5432)
VM="http://$VM_BIND"
python3 "$DIR/write_connections.py" "$VM_BIND" "$CH_BIND" "$PG_BIND" "${RCA_CONNECTIONS_FILE:-}"
for i in $(seq 1 60); do
  ok=0
  curl -sf "$VM/health" >/dev/null && ok=$((ok+1)) || true
  # ⚠ SELECT 1은 부족 — CH entrypoint의 임시 서버가 lucida DB 생성 전에도
  #   응답한다(실측: DDL 전패 레이스). DB 존재까지 확인해야 준비 완료.
  "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" --query \
    "SELECT throwIf(count()=0) FROM system.databases WHERE name='lucida'" \
    >/dev/null 2>&1 && ok=$((ok+1)) || true
  "${COMPOSE[@]}" exec -T postgres pg_isready -U lucida -q >/dev/null 2>&1 && ok=$((ok+1)) || true
  [ "$ok" = 3 ] && break
  sleep 1
  [ "$i" = 60 ] && { echo "DB 기동 대기 초과"; exit 1; }
done

echo "== [2/5] ClickHouse DDL 적용 (runbook §2 — Parquet엔 스키마가 없다)"
# init 완료 직후 entrypoint가 임시 서버를 내리고 본 서버를 재기동하는 짧은
# 다운 창이 있다. 고정 sleep 재시도로는 부족했다(2026-07-30 실측:
# 022_trace_path_signatures.sql이 3회 전패 → 그 테이블만 없는 채로 [3/5]가
# 진행돼 INSERT에서 죽었다). 재시도 전에 **서버 준비를 다시 확인**한다.
ch_wait() {
  for _ in $(seq 1 30); do
    "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" --query \
      "SELECT throwIf(count()=0) FROM system.databases WHERE name='lucida'" \
      >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
ddl_failed=()
for f in "$LUCIDA_NEXT"/database/ddl/clickhouse/*.sql; do
  applied=0
  for try in 1 2 3 4 5; do
    if "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" --multiquery < "$f" >/dev/null 2>&1; then
      applied=1; break
    fi
    ch_wait || true
  done
  if [ "$applied" = 0 ]; then
    ddl_failed+=("$(basename "$f")")
    echo "  ⚠ DDL 실패(5회 재시도 후 계속): $(basename "$f")"
  fi
done
# 실패가 남았으면 서버가 안정된 뒤 한 바퀴 더 — 여기서도 안 되면 진짜 오류다.
if [ ${#ddl_failed[@]} -gt 0 ]; then
  echo "  · 실패분 재시도(${#ddl_failed[@]}건)"
  ch_wait || true
  for name in "${ddl_failed[@]}"; do
    if "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" --multiquery \
        < "$LUCIDA_NEXT/database/ddl/clickhouse/$name" 2>&1 | head -3; then
      echo "    ✔ $name"
    else
      echo "    ✗ $name — 이 파일의 테이블을 쓰는 복원은 실패한다"
    fi
  done
fi

# 덤프에 있는 테이블의 DDL이 없으면 여기서 멈춘다. 안 멈추면 [3/5]의
# INSERT가 "Table ... does not exist"로 죽는데, 그 메시지만 보면 원인이
# 캡처 손상처럼 보인다 — 실제 원인은 **DDL 체크아웃이 캡처보다 낡은 것**이다
# (2026-07-30 실측: 기본 LUCIDA_NEXT가 021에서 멈춘 구 체크아웃이라
# v3 캡처의 trace_path_signatures_local DDL이 아예 없었다).
missing=()
for p in "$CASE"/data/clickhouse/*.parquet; do
  [ -e "$p" ] || continue
  t=$(basename "$p" .parquet)
  "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" --query \
    "SELECT throwIf(count()=0) FROM system.tables WHERE database='lucida' AND name='$t'" \
    >/dev/null 2>&1 || missing+=("$t")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "✗ 덤프에 있으나 DDL이 만들지 못한 테이블: ${missing[*]}"
  echo "  DDL 출처: $LUCIDA_NEXT/database/ddl/clickhouse (파일 $(ls "$LUCIDA_NEXT"/database/ddl/clickhouse/*.sql 2>/dev/null | wc -l)개)"
  echo "  캡처보다 낡은 체크아웃일 수 있다 — LUCIDA_NEXT=/data/lucida-next 로 다시 실행하거나 체크아웃을 갱신하라."
  exit 1
fi

echo "== [3/5] 복원"
echo "  - VictoriaMetrics"
curl -sS -X POST "$VM/api/v1/import" -T "$CASE/data/victoriametrics.export"
echo "  - ClickHouse (파일명 = 테이블명)"
for f in "$CASE"/data/clickhouse/*.parquet; do
  t=$(basename "$f" .parquet)
  # These are newly created isolated replay tables, never live services.
  # Old event-time partitions must survive merges throughout evaluation.
  "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" \
    --query "ALTER TABLE lucida.${t} REMOVE TTL"
  "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" \
    --query "INSERT INTO lucida.${t} FORMAT Parquet" < "$f"
  echo "    ${t}"
done
echo "  - PostgreSQL (전체 스키마+데이터)"
"${COMPOSE[@]}" exec -T postgres \
  pg_restore -U lucida -d lucida --no-owner --if-exists --clean \
  < "$CASE/data/postgres.dump" 2>/dev/null \
  || echo "  ⚠ pg_restore 경고 있음(--clean의 존재하지 않는 객체 등) — 아래 검증으로 판단"

echo "== [4/5] 복원 검증 (runbook §4 — meta.json 시간창 대조)"
read -r T0 T1 < <(python3 -c "
import json,sys
m=json.load(open('$CASE/meta.json'))
# v3(schema 2.0)에서 timeline은 dict가 아니라 문자열 'continuous'다 —
# 구 형식 판별을 타입으로 한다(2026-07-30 실측: .get 호출이 str에서 죽었다).
tl=m.get('timeline')
w=tl.get('capture_window') if isinstance(tl, dict) else None
if w:  # 구 형식 (blackfriday 등)
    print(w['t0'], w['t1'])
else:  # schema 1.2+ 평면 필드
    print(m['capture_start'], m['capture_end'])")
echo "  시간창: $T0 ~ $T1"

echo "  - VM 시계열:"
curl -sS "$VM/api/v1/query_range" \
  --data-urlencode 'query=count({__name__!=""})' \
  --data-urlencode "start=$T0" --data-urlencode "end=$T1" \
  --data-urlencode 'step=5m' | python3 -c "
import json,sys
r=json.load(sys.stdin)['data']['result']
vals=[float(v[1]) for s in r for v in s['values']]
print(f'    시계열 표본 {len(vals)}개, max count={max(vals) if vals else 0:.0f}', '✔' if vals and max(vals)>0 else '✘ 비어 있음(VM retention 함정 의심)')"

echo "  - CH 테이블:"
for f in "$CASE"/data/clickhouse/*.parquet; do
  t=$(basename "$f" .parquet)
  "${COMPOSE[@]}" exec -T clickhouse "${CH[@]}" --query \
    "SELECT '    ${t}: ' || toString(count()) || '행' FROM lucida.${t}"
done

echo "  - PG:"
"${COMPOSE[@]}" exec -T postgres psql -U lucida -d lucida -tA -c \
  "SELECT '    incidents=' || (SELECT count(*) FROM incidents) ||
          ' members=' || (SELECT count(*) FROM incident_members) ||
          ' targets=' || (SELECT count(*) FROM targets);"
echo "    (targets=0이면 복원 실패. incidents=0이면 케이스 자격 요건 미달 — 캡처 설계 참조)"

echo "== [5/5] rca-mcp 접속 env (이 격리 세트를 가리킴)"
cat <<ENV
export RCA_PG_DSN='postgres://lucida:lucida123@$PG_BIND/lucida?sslmode=disable'
export RCA_CH_URL='http://lucida:lucida123@$CH_BIND'
export RCA_VM_URL='$VM'
ENV
echo "폐기: docker compose -f $DIR/eval-dbs.compose.yml -p $PROJ down -v"
