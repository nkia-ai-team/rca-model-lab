# Current RCA harness snapshot and product integration

Snapshot: 2026-10-13. This branch preserves the current observation tools, Python
investigation runtime, evaluation contracts, trajectory export helpers and regression
tests. It does not deploy a product service or publish model weights, captured
telemetry, credentials, local runtime state or the running training jobs.

## Evaluation boundary

Comparison conditions are recorded per run, not inferred from the model or policy name.
Legacy runs have used different prompt budgets, server context limits and turn caps.
Recent Gemma base comparisons use legacy, 60 turns, no soft prompt-token limit,
a 6,000-character tool response limit and a 65,536-token server context. These settings
do not retroactively apply to Muse/Qwen or historical Opus runs. Native Qwen/Gemma
calls and Muse's guided format use different model adapters.

The latest source now supports reverse host-to-service discovery through the existing
`expand_topology` tool. Typed application `meta.host_target_id` references resolve
directly to server targets without requiring spans (`current_inventory;not_historical`).
CH/pod lookup failures retain these direct relations while reporting degraded sources.
Additional windowed spans are joined to current K8s inventory; this is
not a complete service/process inventory or proof of historical placement. The edge
remains app-to-host. See the [current architecture](mcp-harness-architecture.md).
Frozen evaluation binaries do not gain this behavior merely because source was updated.
Record a new binary hash/run identity when applying new behavior.

The current tools include fleet-wide DB inspection, blocking sessions and clients,
K8s changes and post-rollout resource usage, deterministic topology ordering and
corrected blind filtering. Canonical implementations:

- `tools/rca-mcp/tools/registry.go`: shared observation tool catalog.
- `tools/rca-mcp/cmd/rca-mcp/main.go`: MCP stdio transport.
- `src/rca_lab/mcp/student.py`: model investigation loop.
- `src/rca_lab/mcp/context.py`, `alias.py`, `answer.py`: context, identity and submission contracts.
- `scripts/mcp_case_run.py`: isolated capture replay and evaluation (not a production entrypoint).
- `scripts/mcp_judge_runs.py`: incremental mechanism grading; explicitly select Codex / gpt-6.1-sol for new grading.

See [architecture](mcp-harness-architecture.md) and
[historical experiment log](mcp-student-pipeline.md) for background. Those documents
contain historical configurations; this snapshot does not make every historical
experiment the active recipe.

## lucida-next reuse

The tools already query lucida-next PostgreSQL, ClickHouse and VictoriaMetrics
schemas. Reuse is feasible, but the product currently has its own RCA pipeline.
Inspection of `backend/services/ai/features/operator/rca/` in lucida-next found:

- `module.go`: execute/read permissions, APIs, queue and progress SSE.
- `runner/pipeline.go`: existing eight-stage investigation pipeline.
- `runner/runner.go`: execution budget context.
- `model/report.go`: product UIReport result contract.

Keep those product lifecycle boundaries and integrate the research investigation
engine behind an adapter. Share and pin the tool implementation instead of
maintaining independent copies for training, evaluation and production.

Required before product rollout:

1. Enforce tenant/user/authorized-target scope at every datastore query boundary.
   The current Stores/Toolset API has no caller identity; connected snapshot scope
   is not an authorization boundary. Use read-only datastore credentials.
2. Propagate cancellation, deadlines and concurrency budgets through model and MCP
   calls. The current stdio server uses context.Background() for tool execution.
3. Map submit_rca to UIReport, retaining evidence refs and restoring target UUIDs.
4. Isolate per-run settings. CaptureStart/NavHints package globals are unsuitable
   for concurrent in-process investigations with differing configurations;
   separate stdio processes can preserve isolation initially.
5. Validate live incident seed/time windows, schema compatibility and missing data
   against product fixtures before a controlled product rollout.

Publishing these source changes does not deploy product code or replace running evaluation binaries.
Spine INDEX, topology overview and lucida-rca-agent repo card were consulted;
the older repo card was supplemented with current lucida-next source inspection.
