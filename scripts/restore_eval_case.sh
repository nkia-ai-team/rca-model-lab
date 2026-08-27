#!/usr/bin/env bash
set -euo pipefail

case_id=${1:?usage: restore_eval_case.sh CASE_ID}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
case_root=${RCA_EVAL_CASE_ROOT:-/data/eval-cases}
case_dir="$case_root/$case_id"
pg_container=${RCA_EVAL_PG_CONTAINER:-eval-batch-postgres-1}
ch_container=${RCA_EVAL_CH_CONTAINER:-eval-batch-clickhouse-1}
vm_container=${RCA_EVAL_VM_CONTAINER:-eval-batch-victoriametrics-1}
seed_bin=${RCA_INCIDENT_SEED:-/tmp/incident-seed-v2}
ch_schema_dir=${RCA_EVAL_CH_SCHEMA_DIR:-$script_dir/../configs/eval/clickhouse}
pg_restore_jobs=${RCA_EVAL_PG_RESTORE_JOBS:-4}

test -f "$case_dir/meta.json"
test -x "$seed_bin"

docker cp "$case_dir/data/postgres.dump" "$pg_container:/tmp/rca-eval.dump" >/dev/null
# Rebuild the dedicated evaluator database instead of applying --clean over a
# previous capture. Cross-table constraints in the old capture can otherwise
# block object drops and leave a mixed database. ACLs are intentionally omitted
# because production roles do not exist in the isolated evaluator.
docker exec "$pg_container" dropdb -U lucida --if-exists --force lucida
docker exec "$pg_container" createdb -U lucida -O lucida lucida
set +e
docker exec "$pg_container" pg_restore -U lucida -d lucida \
  --no-owner --no-privileges --jobs="$pg_restore_jobs" \
  /tmp/rca-eval.dump >/tmp/rca-eval-pg-restore.log 2>&1
pg_restore_rc=$?
set -e
if (( pg_restore_rc != 0 )); then
  restore_error_count=$(grep -c '^pg_restore: error:' /tmp/rca-eval-pg-restore.log || true)
  unexpected_error_count=$(
    grep '^pg_restore: error:' /tmp/rca-eval-pg-restore.log \
      | grep -Evc 'violates foreign key constraint' || true
  )
  # Captures can contain orphaned historical rows in tables outside the sealed
  # incident. PostgreSQL restores those rows but cannot recreate their FK
  # constraints. Allow only that precisely classified post-data failure; the
  # incident seeder below still verifies the RCA rows and checksum fail-closed.
  if (( restore_error_count == 0 || unexpected_error_count != 0 )); then
    cat /tmp/rca-eval-pg-restore.log >&2
    exit "$pg_restore_rc"
  fi
  printf 'warning: tolerated %s captured FK constraint restore errors; RCA checksum verification remains mandatory\n' \
    "$restore_error_count" >&2
fi

while IFS=$'\t' read -r table expected; do
  if [[ ! "$table" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    printf 'unsafe ClickHouse table identifier: %s\n' "$table" >&2
    exit 1
  fi
  parquet="$case_dir/data/clickhouse/$table.parquet"
  test -f "$parquet" || continue
  exists=$(docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
    --query "EXISTS TABLE lucida.$table" | tr -d '[:space:]')
  if [[ "$exists" != 1 ]]; then
    schema="$ch_schema_dir/$table.sql"
    if [[ ! -f "$schema" ]]; then
      printf 'missing ClickHouse table and evaluator schema: lucida.%s\n' "$table" >&2
      exit 1
    fi
    docker exec -i "$ch_container" clickhouse-client -u lucida --password lucida123 \
      --multiquery <"$schema"
    exists=$(docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
      --query "EXISTS TABLE lucida.$table" | tr -d '[:space:]')
    if [[ "$exists" != 1 ]]; then
      printf 'ClickHouse evaluator schema did not create lucida.%s\n' "$table" >&2
      exit 1
    fi
  fi
  # Evaluation captures intentionally replay historical time windows. Production
  # retention TTLs would otherwise delete those rows during/just after INSERT,
  # making the evidence pool shrink while an episode is running. This stack is
  # dedicated to evaluation, so remove the table TTL before loading the capture.
  has_ttl=$(docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
    --query "SELECT position(create_table_query, ' TTL ') > 0 FROM system.tables WHERE database = 'lucida' AND name = '$table'" \
    | tr -d '[:space:]')
  if [[ "$has_ttl" == 1 ]]; then
    docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
      --query "ALTER TABLE lucida.$table REMOVE TTL" >/dev/null
  fi
  docker cp "$parquet" "$ch_container:/tmp/$table.parquet" >/dev/null
  parquet_rows=$(docker exec "$ch_container" clickhouse-local \
    --query "SELECT count() FROM file('/tmp/$table.parquet', Parquet)" \
    | tr -d '[:space:]')
  if [[ "$parquet_rows" != "$expected" ]]; then
    printf '%s capture row mismatch: parquet=%s want=%s\n' \
      "$table" "$parquet_rows" "$expected" >&2
    exit 1
  fi
  docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
    --query "TRUNCATE TABLE lucida.$table"
  docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
    --query "INSERT INTO lucida.$table FROM INFILE '/tmp/$table.parquet' FORMAT Parquet"
  engine=$(docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
    --query "SELECT engine FROM system.tables WHERE database = 'lucida' AND name = '$table'" \
    | tr -d '[:space:]')
  if [[ "$engine" == ReplacingMergeTree ]]; then
    # ReplacingMergeTree may merge duplicate sorting keys immediately, so its
    # stable contract is the FINAL logical view. The raw Parquet count above
    # proves capture completeness and INSERT is atomic in ClickHouse.
    actual=$(docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
      --query "SELECT count() FROM lucida.$table FINAL" | tr -d '[:space:]')
    if (( actual > expected )) || { (( expected > 0 )) && (( actual == 0 )); }; then
      printf '%s logical row mismatch: final=%s raw=%s\n' \
        "$table" "$actual" "$expected" >&2
      exit 1
    fi
  else
    actual=$(docker exec "$ch_container" clickhouse-client -u lucida --password lucida123 \
      --query "SELECT count() FROM lucida.$table" | tr -d '[:space:]')
    if [[ "$actual" != "$expected" ]]; then
      printf '%s row mismatch: got=%s want=%s\n' "$table" "$actual" "$expected" >&2
      exit 1
    fi
  fi
done < <(jq -r '.clickhouse_tables[] | [.table, .rows] | @tsv' "$case_dir/meta.json")

# VictoriaMetrics import is append-only. Recreate only the dedicated evaluator
# service so a case can never observe time series imported for a previous case.
vm_compose_file=$(docker inspect "$vm_container" \
  --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
vm_project=$(docker inspect "$vm_container" \
  --format '{{index .Config.Labels "com.docker.compose.project"}}')
vm_service=$(docker inspect "$vm_container" \
  --format '{{index .Config.Labels "com.docker.compose.service"}}')
test -f "$vm_compose_file"
docker compose -p "$vm_project" -f "$vm_compose_file" up -d \
  --force-recreate --no-deps "$vm_service" >/dev/null
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:58428/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:58428/health >/dev/null
curl -fsS -X POST http://127.0.0.1:58428/api/v1/import \
  -T "$case_dir/data/victoriametrics.export" >/tmp/rca-eval-vm-import.log
vm_start=$(jq -r '.capture_start' "$case_dir/meta.json")
vm_end=$(jq -r '.capture_end' "$case_dir/meta.json")
vm_metric_count=0
# VictoriaMetrics acknowledges a completed import before the label index is
# guaranteed to be visible. Verify with a bounded retry so that a fresh
# evaluator container cannot fail spuriously immediately after import.
for _ in $(seq 1 30); do
  vm_labels=$(curl -fsSG http://127.0.0.1:58428/api/v1/label/__name__/values \
    --data-urlencode "start=$vm_start" --data-urlencode "end=$vm_end" || true)
  vm_metric_count=$(jq -r '(.data // []) | length' <<<"$vm_labels" 2>/dev/null || printf '0')
  if [[ "$vm_metric_count" -ge 1 ]]; then
    break
  fi
  sleep 1
done
if [[ "$vm_metric_count" -lt 1 ]]; then
  printf 'VictoriaMetrics import verification found no metrics in %s..%s\n' \
    "$vm_start" "$vm_end" >&2
  exit 1
fi

"$seed_bin" --case "$case_dir" \
  --pg postgres://lucida:lucida123@127.0.0.1:55432/lucida \
  --ch-http http://127.0.0.1:57123 --replace
