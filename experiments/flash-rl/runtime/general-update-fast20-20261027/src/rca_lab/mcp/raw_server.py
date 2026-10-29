"""MCP stdio server with raw, generic read-only access to a restored capture — the "no harness" arm of the
harness ablation (user 2026-10-02: Opus 5.5 with our 23 rca-mcp tools vs. the same model with plain database access).

Tools: SQL on PostgreSQL (target registry / metadata) and ClickHouse (logs, traces, events, DB monitoring), PromQL and
series/label discovery on VictoriaMetrics. No analysis, aggregation or anomaly logic beyond what the query itself does.
Every response is a JSON envelope with a `refs` entry (so the same `submit_rca` contract applies) and passes through
`rca-mcp -sanitize-stdin` — the same blind filter the harness applies — before the model sees it.

Usage (claude CLI MCP config): python -m rca_lab.mcp.raw_server   env: RCA_PG_DSN, RCA_CH_URL, RCA_VM_URL, RCA_MCP_BINARY
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_CHARS = 12_000
TIMEOUT_S = 30

TOOLS = [
    {
        "name": "pg_sql",
        "description": "PostgreSQL 읽기 전용 SQL (대상 레지스트리·관계·메타데이터). information_schema 로 스키마를 찾을 수 있다. 결과는 CSV, 최대 12000자.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "단일 SELECT/WITH 문"}}, "required": ["query"]},
    },
    {
        "name": "ch_sql",
        "description": "ClickHouse 읽기 전용 SQL (로그·트레이스·이벤트·DB 모니터링). system.tables / DESCRIBE 로 스키마를 찾을 수 있다. 결과는 TSV(헤더 포함), 최대 12000자.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "단일 SELECT 문"}}, "required": ["query"]},
    },
    {
        "name": "vm_query_range",
        "description": "VictoriaMetrics PromQL 범위 조회 (/api/v1/query_range). 지표는 target_id 등 라벨을 가진다. 결과는 JSON, 최대 12000자.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "start": {"type": "string", "description": "RFC3339"}, "end": {"type": "string", "description": "RFC3339"},
            "step": {"type": "string", "description": "예: 30s, 1m"}}, "required": ["query", "start", "end", "step"]},
    },
    {
        "name": "vm_series",
        "description": "VictoriaMetrics 시리즈 탐색 (/api/v1/series): match[] 셀렉터에 맞는 라벨 집합 목록. 결과는 JSON, 최대 12000자.",
        "inputSchema": {"type": "object", "properties": {
            "match": {"type": "string", "description": "예: {target_id=\"<uuid>\"} 또는 {__name__=~\"kcm.*\"}"},
            "start": {"type": "string"}, "end": {"type": "string"}}, "required": ["match"]},
    },
    {
        "name": "vm_label_values",
        "description": "VictoriaMetrics 라벨 값 목록 (/api/v1/label/<label>/values), 예: __name__ 으로 지표 이름 전체. 결과는 JSON, 최대 12000자.",
        "inputSchema": {"type": "object", "properties": {"label": {"type": "string"}, "match": {"type": "string", "description": "선택: 셀렉터로 좁히기"},
            "start": {"type": "string"}, "end": {"type": "string"}}, "required": ["label"]},
    },
]


def _ref(tool: str, args: dict[str, Any]) -> str:
    return f"raw:{tool}:{hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]}"


def _clip(text: str) -> tuple[str, bool]:
    return (text[:MAX_CHARS], True) if len(text) > MAX_CHARS else (text, False)


def _read_only_sql(query: str) -> str:
    q = query.strip().rstrip(";").strip()
    head = q.split(None, 1)[0].lower() if q else ""
    if head not in {"select", "with", "describe", "desc", "show", "explain"} or ";" in q:
        raise ValueError("단일 읽기 전용 문(SELECT/WITH/DESCRIBE/SHOW/EXPLAIN)만 허용")
    return q


def _pg_container(dsn: str) -> tuple[str, urllib.parse.ParseResult]:
    url = urllib.parse.urlparse(dsn)
    out = subprocess.run(["docker", "ps", "--filter", f"publish={url.port}", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=TIMEOUT_S, check=True)
    names = [n for n in out.stdout.split() if n]
    if not names:
        raise RuntimeError("postgres container for the capture not found")
    return names[0], url


def pg_sql(query: str) -> str:
    q = _read_only_sql(query)
    name, url = _pg_container(os.environ["RCA_PG_DSN"])
    cmd = ["docker", "exec", "-e", f"PGPASSWORD={url.password or ''}", name, "psql", "-U", url.username or "postgres", "-d", url.path.lstrip("/") or "postgres",
           "-X", "-q", "--csv", "-v", "ON_ERROR_STOP=1", "-c", "SET default_transaction_read_only = on", "-c", f"SET statement_timeout = '{TIMEOUT_S}s'", "-c", q]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S + 10, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-800:] or f"psql exit {proc.returncode}")
    return proc.stdout


def _http(url: str, *, data: bytes | None = None, auth: tuple[str, str] | None = None) -> str:
    req = urllib.request.Request(url, data=data)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S + 10) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", errors="replace")[-800:]) from exc


def ch_sql(query: str) -> str:
    q = _read_only_sql(query)
    url = urllib.parse.urlparse(os.environ["RCA_CH_URL"])
    base = f"{url.scheme}://{url.hostname}:{url.port}/"
    params = urllib.parse.urlencode({"readonly": "1", "max_execution_time": str(TIMEOUT_S), "default_format": "TSVWithNames"})
    return _http(f"{base}?{params}", data=q.encode(), auth=(url.username or "default", url.password or ""))


def _vm(path: str, params: dict[str, str]) -> str:
    return _http(os.environ["RCA_VM_URL"].rstrip("/") + path + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v}))


def _window(args: dict[str, Any]) -> tuple[str, str]:
    """Series/label discovery defaults to the incident window ±12 h (VictoriaMetrics otherwise looks at "now")."""
    import datetime as dt

    first, last = os.environ.get("RCA_FIRST_EVENT", ""), os.environ.get("RCA_LAST_EVENT", "")
    if args.get("start") or not first:
        return str(args.get("start", "")), str(args.get("end", ""))
    f = dt.datetime.fromisoformat(first.replace("Z", "+00:00")) - dt.timedelta(hours=12)
    l = dt.datetime.fromisoformat((last or first).replace("Z", "+00:00")) + dt.timedelta(hours=12)
    return f.strftime("%Y-%m-%dT%H:%M:%SZ"), str(args.get("end") or l.strftime("%Y-%m-%dT%H:%M:%SZ"))


def call(name: str, args: dict[str, Any]) -> str:
    if name == "pg_sql":
        return pg_sql(str(args["query"]))
    if name == "ch_sql":
        return ch_sql(str(args["query"]))
    if name == "vm_query_range":
        return _vm("/api/v1/query_range", {"query": args["query"], "start": args["start"], "end": args["end"], "step": args["step"]})
    if name == "vm_series":
        start, end = _window(args)
        return _vm("/api/v1/series", {"match[]": args["match"], "start": start, "end": end})
    if name == "vm_label_values":
        label = urllib.parse.quote(str(args["label"]), safe="")
        start, end = _window(args)
        return _vm(f"/api/v1/label/{label}/values", {"match[]": args.get("match", ""), "start": start, "end": end})
    raise ValueError(f"unknown tool {name}")


def sanitize(envelope: dict[str, Any]) -> str:
    binary = os.environ.get("RCA_MCP_BINARY", "")
    raw = json.dumps(envelope, ensure_ascii=False)
    if not binary:
        return raw
    proc = subprocess.run([binary, "-sanitize-stdin"], input=raw, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError("blind sanitizer failed")
    return proc.stdout.strip()


def handle(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    try:
        text, truncated = _clip(call(name, args))
        envelope = {"status": "ok", "truncated": truncated, "result": text, "refs": [_ref(name, args)]}
        return sanitize(envelope), False
    except (KeyError, ValueError, RuntimeError, subprocess.SubprocessError, OSError) as exc:
        return json.dumps({"status": "error", "error": str(exc)[:800]}, ensure_ascii=False), True


def _reply(identifier: object, result: object) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        identifier, method = message.get("id"), message.get("method")
        if identifier is None:
            continue
        if method == "initialize":
            _reply(identifier, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "rca-raw", "version": "1"}})
        elif method == "tools/list":
            _reply(identifier, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            text, is_error = handle(str(params.get("name")), params.get("arguments") or {})
            _reply(identifier, {"content": [{"type": "text", "text": text}], "isError": is_error})
        else:
            _reply(identifier, {})


if __name__ == "__main__":
    main()
