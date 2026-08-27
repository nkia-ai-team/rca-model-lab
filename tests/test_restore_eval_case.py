from pathlib import Path

RESTORE_SCRIPT = Path(__file__).parents[1] / "scripts" / "restore_eval_case.sh"


def test_historical_clickhouse_rows_are_protected_from_production_ttl() -> None:
    script = RESTORE_SCRIPT.read_text(encoding="utf-8")

    remove_ttl = "ALTER TABLE lucida.$table REMOVE TTL"
    truncate = "TRUNCATE TABLE lucida.$table"
    insert = "INSERT INTO lucida.$table FROM INFILE"

    assert remove_ttl in script
    assert script.index(remove_ttl) < script.index(truncate) < script.index(insert)
    assert "position(create_table_query, ' TTL ') > 0" in script
    assert 'if [[ "$has_ttl" == 1 ]]' in script


def test_clickhouse_table_names_are_validated_before_sql_interpolation() -> None:
    script = RESTORE_SCRIPT.read_text(encoding="utf-8")

    validation = '[[ ! "$table" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]'
    remove_ttl = "ALTER TABLE lucida.$table REMOVE TTL"

    assert validation in script
    assert script.index(validation) < script.index(remove_ttl)


def test_missing_captured_tables_require_an_evaluator_schema() -> None:
    script = RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert 'schema="$ch_schema_dir/$table.sql"' in script
    assert "missing ClickHouse table and evaluator schema" in script
    assert "--multiquery" in script


def test_projection_schema_omits_production_retention_ttl() -> None:
    schema = (
        Path(__file__).parents[1]
        / "configs"
        / "eval"
        / "clickhouse"
        / "event_cluster_projection_local.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS lucida.event_cluster_projection_local" in schema
    assert "ReplacingMergeTree(desired_version)" in schema
    assert "\nTTL " not in schema


def test_restore_separates_raw_capture_and_replacing_logical_counts() -> None:
    script = RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert "SELECT count() FROM file('/tmp/$table.parquet', Parquet)" in script
    assert '[[ "$parquet_rows" != "$expected" ]]' in script
    assert '[[ "$engine" == ReplacingMergeTree ]]' in script
    assert "SELECT count() FROM lucida.$table FINAL" in script
    assert script.index("parquet_rows=") < script.index("INSERT INTO lucida.$table")


def test_victoriametrics_visibility_is_verified_with_bounded_retry() -> None:
    script = RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert "for _ in $(seq 1 30)" in script
    assert "vm_metric_count=$(jq -r '(.data // []) | length'" in script
    assert script.index("for _ in $(seq 1 30)") < script.index(
        "VictoriaMetrics import verification found no metrics"
    )


def test_postgres_restore_is_parallel_and_fail_closed() -> None:
    script = RESTORE_SCRIPT.read_text(encoding="utf-8")

    assert "pg_restore_jobs=${RCA_EVAL_PG_RESTORE_JOBS:-4}" in script
    assert "dropdb -U lucida --if-exists --force lucida" in script
    assert "createdb -U lucida -O lucida lucida" in script
    assert "--no-privileges" in script
    assert '--jobs="$pg_restore_jobs"' in script
    restore_command = script.split('docker exec "$pg_container" pg_restore', 1)[1].split(
        "while IFS=", 1
    )[0]
    assert "--clean" not in restore_command
    assert "pg_restore_rc=$?" in restore_command
    assert "unexpected_error_count" in restore_command
    assert "violates foreign key constraint" in restore_command
    assert 'exit "$pg_restore_rc"' in restore_command
