from __future__ import annotations

import fcntl
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

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


def test_restore_failure_cleans_new_compose_project(tmp_path: Path) -> None:
    case = tmp_path / "case-fake"
    (case / "data/clickhouse").mkdir(parents=True)
    (case / "data/clickhouse/missing_table.parquet").write_bytes(b"fake parquet")
    (case / "data/victoriametrics.export").write_text("", encoding="utf-8")
    (case / "data/postgres.dump").write_bytes(b"")
    (case / "meta.json").write_text(
        json.dumps({"capture_start": "2026-01-01T00:00:00Z", "capture_end": "2026-01-01T00:10:00Z"}),
        encoding="utf-8",
    )
    lucida_next = tmp_path / "lucida-next"
    (lucida_next / "database/ddl/clickhouse").mkdir(parents=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        fake_bin / "docker",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"

        if [[ "$*" == *" ps --all -q"* ]]; then exit 0; fi
        if [[ "$*" == *" port victoriametrics 8428"* ]]; then echo "127.0.0.1:18428"; exit 0; fi
        if [[ "$*" == *" port clickhouse 8123"* ]]; then echo "127.0.0.1:18123"; exit 0; fi
        if [[ "$*" == *" port postgres 5432"* ]]; then echo "127.0.0.1:15432"; exit 0; fi
        if [[ "$*" == *"system.databases"* ]]; then exit 0; fi
        if [[ "$*" == *"pg_isready"* ]]; then exit 0; fi
        if [[ "$*" == *"system.tables"* ]]; then exit 1; fi
        if [[ "$*" == *" down -v"* ]]; then exit 0; fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "curl",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        echo "ok"
        """,
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "LUCIDA_NEXT": str(lucida_next),
        "RCA_CONNECTIONS_FILE": str(tmp_path / "connections.json"),
        "FAKE_DOCKER_LOG": str(docker_log),
    }

    result = subprocess.run(["bash", str(RESTORE), str(case), "cleanup-test"], env=env, capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert "down -v" in docker_log.read_text(encoding="utf-8")
    assert "실패/중단 cleanup" in result.stderr


def test_restore_refuses_existing_project_before_cleanup_is_armed(tmp_path: Path) -> None:
    case = tmp_path / "case-fake"
    (case / "data").mkdir(parents=True)
    lucida_next = tmp_path / "lucida-next"
    (lucida_next / "database/ddl/clickhouse").mkdir(parents=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        fake_bin / "docker",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
        if [[ "$*" == *" ps --all -q"* ]]; then echo "existing-container"; exit 0; fi
        if [[ "$*" == *" down -v"* ]]; then exit 2; fi
        exit 0
        """,
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "LUCIDA_NEXT": str(lucida_next),
        "FAKE_DOCKER_LOG": str(docker_log),
    }

    result = subprocess.run(["bash", str(RESTORE), str(case), "live-project"], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 75
    assert "이미 존재하는 replay DB project 거부" in result.stdout
    assert "down -v" not in docker_log.read_text(encoding="utf-8")


def test_concurrent_restore_refused_before_docker_access(tmp_path):
    case = tmp_path / "case"
    (case / "data").mkdir(parents=True)
    ddl = tmp_path / "lucida" / "database/ddl/clickhouse"
    ddl.mkdir(parents=True)
    locks = tmp_path / "rca-replay-project-locks"
    locks.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(fake_bin / "docker", '#!/bin/sh\necho called >> "$FAKE_DOCKER_LOG"\nexit 99\n')
    env = {**os.environ, "TMPDIR": str(tmp_path), "LUCIDA_NEXT": str(tmp_path / "lucida"),
           "PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(docker_log)}
    with (locks / "same-project.lock").open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(['bash', str(RESTORE), str(case), 'same-project'], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 75
    assert not docker_log.exists()


@pytest.mark.parametrize("failure", ["preflight", "partial-up", "signal"])
def test_restore_ownership_boundary_on_failure_and_signal(tmp_path, failure):
    case = tmp_path / "case"
    (case / "data").mkdir(parents=True)
    (tmp_path / "lucida/database/ddl/clickhouse").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    _write_executable(fake_bin / "docker", '''
        #!/usr/bin/env bash
        echo "$*" >> "$FAKE_DOCKER_LOG"
        if [[ "$*" == *" ps --all -q" ]]; then
            [[ "$FAILURE" != preflight ]]; exit $?
        fi
        if [[ "$*" == *" up -d" ]]; then
            if [[ "$FAILURE" == signal ]]; then kill -TERM "$PPID"; exit 0; fi
            exit 1
        fi
        exit 0
    ''')
    env = {**os.environ, "TMPDIR": str(tmp_path), "LUCIDA_NEXT": str(tmp_path / "lucida"),
           "PATH": f"{fake_bin}:{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(log), "FAILURE": failure}
    result = subprocess.run(['bash', str(RESTORE), str(case), 'boundary'], env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode != 0
    assert ('down -v' in log.read_text()) == (failure != 'preflight')
