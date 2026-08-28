"""Launch the same Go RCA actor used by sealed evaluation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import verifiers.v1 as vf
from pydantic import Field

from rca_lab.prime_rl.paths import project_path


class RCAStudentHarnessConfig(vf.HarnessConfig):
    agent_path: Path = Field(default_factory=lambda: project_path("bin/rca-agent-v6"))
    actor_temperature: float = 1.0
    postgres_dsn: str = "postgres://lucida:lucida123@localhost:55432/lucida"
    clickhouse_addr: str = "localhost:57001"
    clickhouse_db: str = "lucida"
    clickhouse_user: str = "lucida"
    clickhouse_password: str = "lucida123"
    vm_query_url: str = "http://localhost:58428"


class RCAStudentHarness(vf.Harness[RCAStudentHarnessConfig]):
    APPENDS_SYSTEM_PROMPT = True
    EXECUTES_CODE = False
    NEEDS_CONTAINER = False

    async def setup(self, runtime: vf.Runtime) -> None:
        result = await runtime.run(["test", "-x", str(self.config.agent_path)], {})
        if result.exit_code:
            raise FileNotFoundError(f"RCA actor is not executable: {self.config.agent_path}")

    async def launch(
        self,
        ctx: vf.ModelContext,
        trace: vf.Trace,
        runtime: vf.Runtime,
        endpoint: str,
        secret: str,
        mcp_urls: dict[str, str],
        data: vf.TaskData,
    ) -> vf.ProgramResult:
        if mcp_urls:
            raise ValueError("RCA student owns its typed tools; external MCP tools are forbidden")
        seed = int.from_bytes(hashlib.sha256(trace.id.encode()).digest()[:8], "big")
        env = {
            **self.config.resolved_env,
            "LUCIDA_AI_RCA_PROBE_URL": endpoint,
            "LUCIDA_AI_MODEL_RCA_PROBE": ctx.model,
            "LUCIDA_AI_URL": endpoint,
            "LUCIDA_AI_MODEL": ctx.model,
            "LUCIDA_QDRANT_URL": "http://localhost:1",
            "LUCIDA_LLM_PROVIDER": "vllm",
            "LUCIDA_SECRET_KEY": secret,
            "POSTGRES_DSN": self.config.postgres_dsn,
            "CLICKHOUSE_ADDR": self.config.clickhouse_addr,
            "CLICKHOUSE_DB": self.config.clickhouse_db,
            "CLICKHOUSE_USER": self.config.clickhouse_user,
            "CLICKHOUSE_PASSWORD": self.config.clickhouse_password,
            "VM_QUERY_URL": self.config.vm_query_url,
            "RCA_TRAJECTORY_DIR": "trajectory",
        }
        command = (
            'incident=$(cat -- "$1") || exit 1; shift; '
            'agent=$1; shift; "$agent" "$@" "$incident"; rc=$?; '
            'set -- trajectory/agent-*.jsonl; '
            '[ "$#" -eq 1 ] && cp -- "$1" episode.jsonl; exit "$rc"'
        )
        return await runtime.run_program(
            [
                "sh",
                "-c",
                command,
                "rca-student",
                ".rca-incident-id",
                str(self.config.agent_path),
                "--actor-temperature",
                str(self.config.actor_temperature),
                "--actor-seed",
                str(seed),
            ],
            env,
        )
