"""RCA agent loop on the rca-mcp tool surface (tools/rca-mcp, 23 read-only tools).

Teacher (claude CLI + MCP) and student (vLLM chat completions + MCP client) share the same
observed-alarm seed, the same tool catalog, the same final-answer contract (`submit_rca`) and
the same scorer, so their trajectories are directly comparable and distillable.
"""
