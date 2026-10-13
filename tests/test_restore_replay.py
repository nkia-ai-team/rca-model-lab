from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESTORE = ROOT / "tools/rca-mcp/replay/restore.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def test_restore_skips_remove_ttl_when_clickhouse_table_has_no_ttl(tmp_path: Path) -> None:
    case = tmp_path / "case-fake"
    (case / "data/clickhouse").mkdir(parents=True)
    (case / "data/clickhouse/events_with_ttl.parquet").write_bytes(b"fake parquet")
    (case / "data/clickhouse/logs_without_ttl.parquet").write_bytes(b"fake parquet")
    (case / "data/victoriametrics.export").write_text("", encoding="utf-8")
    (case / "data/postgres.dump").write_bytes(b"")
    (case / "meta.json").write_text(
        json.dumps({"capture_start": "2026-01-01T00:00:00Z", "capture_end": "2026-01-01T00:10:00Z"}),
        encoding="utf-8",
    )

    lucida_next = tmp_path / "lucida-next"
    (lucida_next / "database/ddl/clickhouse").mkdir(parents=True)
    (lucida_next / "database/ddl/clickhouse/001.sql").write_text(
        "CREATE TABLE lucida.events_with_ttl (x UInt8) ENGINE = MergeTree ORDER BY x TTL now();",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    remove_ttl_log = tmp_path / "remove-ttl.log"
    _write_executable(
        fake_bin / "docker",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"

        if [[ "$*" == *" port victoriametrics 8428"* ]]; then echo "127.0.0.1:18428"; exit 0; fi
        if [[ "$*" == *" port clickhouse 8123"* ]]; then echo "127.0.0.1:18123"; exit 0; fi
        if [[ "$*" == *" port postgres 5432"* ]]; then echo "127.0.0.1:15432"; exit 0; fi

        if [[ "$*" == *"SHOW CREATE TABLE lucida."* ]]; then
          if [[ "$*" == *"events_with_ttl"* ]]; then
            echo "CREATE TABLE lucida.events_with_ttl\\n(\\n    x UInt8\\n)\\nENGINE = MergeTree\\nORDER BY x\\nTTL now() + toIntervalDay(1)"
          else
            echo "CREATE TABLE lucida.logs_without_ttl\\n(\\n    x UInt8\\n)\\nENGINE = MergeTree\\nORDER BY x"
          fi
          exit 0
        fi

        if [[ "$*" == *"REMOVE TTL"* ]]; then
          printf '%s\n' "$*" >> "${FAKE_REMOVE_TTL_LOG:?}"
          exit 0
        fi

        if [[ "$*" == *"toString(count())"* ]]; then
          echo "    fake_table: 1행"
          exit 0
        fi

        if [[ "$*" == *"psql"* ]]; then
          echo "    incidents=1 members=1 targets=1"
          exit 0
        fi

        exit 0
        """,
    )
    _write_executable(
        fake_bin / "curl",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [[ "$*" == *"/api/v1/query_range"* ]]; then
          echo '{"data":{"result":[{"values":[[0,"1"]]}]}}'
        else
          echo "ok"
        fi
        """,
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "LUCIDA_NEXT": str(lucida_next),
        "RCA_CONNECTIONS_FILE": str(tmp_path / "connections.json"),
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_REMOVE_TTL_LOG": str(remove_ttl_log),
    }
    result = subprocess.run(
        ["bash", str(RESTORE), str(case), "ttl-test"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    remove_ttl_calls = remove_ttl_log.read_text(encoding="utf-8")
    all_docker_calls = docker_log.read_text(encoding="utf-8")
    assert "events_with_ttl" in remove_ttl_calls
    assert "logs_without_ttl" not in remove_ttl_calls
    assert "INSERT INTO lucida.logs_without_ttl FORMAT Parquet" in all_docker_calls
    assert "logs_without_ttl" in result.stdout
