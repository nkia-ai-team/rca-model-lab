"""Run the offline joint evidence-admission judge for snapshot items.

This runner consumes immutable snapshot ``items.jsonl`` rows and asks Codex to judge only the
blind evidence item. Successful verdicts are cached by item content, rubric, and model so reruns
resume without spending quota. Transport and parse failures remain pending and are not cached as
approvals.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rca_lab.mcp.codex_cli import SessionLimitError, run_codex
from rca_lab.mcp.evidence_admission import (
    MODEL,
    RUBRIC_VERSION,
    SYSTEM_PROMPT,
    admission,
    cached_verdict,
    item_sha256,
    verdict_record,
)

FENCE_RE = re.compile(r"\A```(?:json)?\s*(\{.*\})\s*```\Z", re.DOTALL)


def strict_json_object(text: str) -> dict[str, Any]:
    """Parse exactly one JSON object, accepting an object wrapped in one fenced block."""
    stripped = text.strip()
    match = FENCE_RE.fullmatch(stripped)
    if match:
        stripped = match.group(1).strip()
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(stripped)
    if stripped[end:].strip():
        raise ValueError("response contains trailing text after JSON object")
    if not isinstance(value, dict):
        raise TypeError("response must be a JSON object")
    return value


def load_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def load_items(path: Path, ids: set[str] | None, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    found: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            audit_id = str(row.get("audit_id", line_number))
            if ids is not None and audit_id not in ids:
                continue
            found.add(audit_id)
            row["_line_number"] = line_number
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    missing = sorted((ids or set()) - found)
    return rows, missing


def cache_path(output: Path, *, item_hash: str, rubric: str, model: str) -> Path:
    safe_model = model.replace("/", "_")
    safe_rubric = rubric.replace("/", "_")
    return output / "cache" / safe_model / safe_rubric / f"{item_hash}.json"


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def quarantine_cache(path: Path) -> Path:
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.malformed-{index}")
        if not candidate.exists():
            os.replace(path, candidate)
            return candidate
    raise RuntimeError(f"too many malformed cache quarantines for {path}")


def error_artifact(
    output: Path,
    *,
    audit_id: str,
    item_hash: str,
    error_type: str,
    raw_response: str,
    detail: str,
) -> Path:
    response_hash = __import__("hashlib").sha256(raw_response.encode("utf-8")).hexdigest()
    safe_audit_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", audit_id)
    path = output / "errors" / f"{safe_audit_id}.{item_hash}.{error_type}.{response_hash}.json"
    atomic_json(
        path,
        {
            "audit_id": audit_id,
            "item_sha256": item_hash,
            "provider": "codex",
            "model": MODEL,
            "rubric_version": RUBRIC_VERSION,
            "error_type": error_type,
            "detail": detail[:1000],
            "raw_response_sha256": response_hash,
            "raw_response": raw_response,
        },
    )
    return path


def prompt_for(item: dict[str, Any]) -> str:
    return json.dumps({"item": item}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pending_record(row: dict[str, Any], item_hash: str | None, error_type: str, detail: str) -> dict[str, Any]:
    return {
        "audit_id": str(row.get("audit_id", row.get("_line_number", ""))),
        "item_sha256": item_hash,
        "status": "pending",
        "decision": "pending",
        "reason": error_type,
        "error_type": error_type,
        "detail": detail[:500],
        "cached": False,
    }


def process_row(row: dict[str, Any], output: Path, timeout: int, cache_from: tuple[Path, ...] = ()) -> dict[str, Any]:
    item = row.get("item")
    if not isinstance(item, dict):
        return pending_record(row, None, "missing_item", "row.item must be a JSON object")
    actual_hash = item_sha256(item)
    if row.get("item_sha256") != actual_hash:
        return pending_record(
            row,
            actual_hash,
            "input_hash_mismatch",
            "row.item_sha256 must match sorted spaced item JSON sha256",
        )

    path = cache_path(output, item_hash=actual_hash, rubric=RUBRIC_VERSION, model=MODEL)
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            quarantine_cache(path)
            record = None
            verdict = None
        else:
            verdict = cached_verdict(item, record)
            if verdict is not None:
                decision = admission(item, verdict)
                return {
                    "audit_id": str(row.get("audit_id", row.get("_line_number", ""))),
                    "item_sha256": actual_hash,
                    "status": "complete",
                    "decision": decision["decision"],
                    "reason": decision["reason"],
                    "error_type": None,
                    "cached": True,
                }

    # Import only a fully validated exact item/model/rubric cache. A valid local
    # verdict returned above always wins; external caches are read-only.
    for root in cache_from:
        candidate = cache_path(root, item_hash=actual_hash, rubric=RUBRIC_VERSION, model=MODEL)
        try:
            imported = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        verdict = cached_verdict(item, imported)
        if verdict is None:
            continue
        atomic_json(path, imported)
        decision = admission(item, verdict)
        return {
            "audit_id": str(row.get("audit_id", row.get("_line_number", ""))),
            "item_sha256": actual_hash,
            "status": "complete",
            "decision": decision["decision"],
            "reason": decision["reason"],
            "error_type": None,
            "cached": True,
            "cache_imported_from": str(candidate),
        }

    text, diagnostic = run_codex(
        system_prompt=SYSTEM_PROMPT,
        prompt=prompt_for(item),
        model=MODEL,
        timeout_seconds=timeout,
    )
    if text is None:
        error_type = "timeout" if diagnostic.startswith("timeout") else "missing_response"
        return pending_record(row, actual_hash, error_type, diagnostic)
    try:
        verdict = strict_json_object(text)
        record = verdict_record(item, verdict)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        artifact = error_artifact(
            output,
            audit_id=str(row.get("audit_id", row.get("_line_number", ""))),
            item_hash=actual_hash,
            error_type="malformed_response",
            raw_response=text,
            detail=str(exc),
        )
        pending = pending_record(row, actual_hash, "malformed_response", str(exc))
        pending["error_artifact"] = str(artifact)
        return pending

    atomic_json(path, record)
    decision = admission(item, record["verdict"])
    return {
        "audit_id": str(row.get("audit_id", row.get("_line_number", ""))),
        "item_sha256": actual_hash,
        "status": "complete",
        "decision": decision["decision"],
        "reason": decision["reason"],
        "error_type": None,
        "cached": False,
    }


def report_for(
    records: list[dict[str, Any]],
    *,
    input_path: Path,
    limit: int,
    ids_file: Path | None,
    expected_total: int | None = None,
    status_override: str | None = None,
) -> dict[str, Any]:
    decisions = Counter(record["decision"] for record in records)
    errors = Counter(record["error_type"] for record in records if record.get("error_type"))
    complete = [record for record in records if record["status"] == "complete"]
    total = expected_total if expected_total is not None else len(records)
    return {
        "status": status_override or ("complete" if not decisions.get("pending") else "pending"),
        "input": str(input_path),
        "limit": limit,
        "ids_file": str(ids_file) if ids_file else None,
        "provider": "codex",
        "model": MODEL,
        "rubric_version": RUBRIC_VERSION,
        "counts": {
            "approved": decisions.get("approve", 0),
            "held": decisions.get("hold", 0),
            "rejected": decisions.get("reject", 0),
            "pending": decisions.get("pending", 0),
            "total": total,
        },
        "errors": dict(sorted(errors.items())),
        "progress": {
            "processed": len(records),
            "expected_total": total,
            "complete": len(complete),
        },
        "provenance_coverage": {
            "complete": len(complete),
            "with_item_sha256": sum(1 for record in complete if record.get("item_sha256")),
            "provider_model_rubric": sum(1 for _ in complete),
        },
        "items": records,
    }


def run(input_path: Path, output: Path, *, limit: int, timeout: int,
        ids_file: Path | None = None, workers: int = 1,
        cache_from: tuple[Path, ...] = ()) -> dict[str, Any]:
    if limit < 0:
        raise ValueError("--limit must be nonnegative")
    if timeout <= 0:
        raise ValueError("--timeout must be positive")
    if not 1 <= workers <= 16:
        raise ValueError("--workers must be between 1 and 16")
    output.mkdir(parents=True, exist_ok=True)
    ids = load_ids(ids_file)
    rows, missing_ids = load_items(input_path, ids, limit)
    expected_total = len(rows) + len(missing_ids)
    lock_path = output / ".admission.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        missing = [pending_record({"audit_id": audit_id}, None, "missing_requested_id",
                                  "requested audit_id was not present in input selection")
                   for audit_id in missing_ids]
        finished: dict[int, dict[str, Any]] = {}

        def write_report(*, running: bool) -> dict[str, Any]:
            report = report_for(
                missing + [finished[index] for index in sorted(finished)],
                input_path=input_path, limit=limit, ids_file=ids_file,
                expected_total=expected_total, status_override="running" if running else None,
            )
            report["workers"] = workers
            report["cache_from"] = [str(root) for root in cache_from]
            atomic_json(output / "admission.json", report)
            return report

        # Group only hash-valid identical items. Failed input checks remain independent.
        groups: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            item = row.get("item")
            digest = item_sha256(item) if isinstance(item, dict) else None
            key = digest if digest is not None and row.get("item_sha256") == digest else f"invalid:{index}"
            groups.setdefault(key, []).append(index)
        queued = iter(groups.values())
        quota = Event()

        def execute(indices: list[int]) -> dict[str, Any]:
            row = rows[indices[0]]
            try:
                return process_row(row, output, timeout, tuple(cache_from))
            except SessionLimitError as exc:
                quota.set()  # Stop refill even if another worker completes first.
                return pending_record(row, row.get("item_sha256"), "quota", str(exc))
            except (OSError, TimeoutError) as exc:
                return pending_record(row, row.get("item_sha256"), "transport_error", str(exc))

        write_report(running=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            active = {}

            def refill() -> None:
                while len(active) < workers and not quota.is_set():
                    indices = next(queued, None)
                    if indices is None:
                        break
                    active[pool.submit(execute, indices)] = indices

            refill()
            while active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda job: active[job][0]):
                    indices = active.pop(future)
                    result = future.result()
                    for duplicate, index in enumerate(indices):
                        row = rows[index]
                        record = {**result, "audit_id": str(row.get("audit_id", row.get("_line_number", "")))}
                        if duplicate:
                            record["deduplicated_from"] = str(rows[indices[0]].get("audit_id", indices[0]))
                            if result.get("error_type") == "quota":
                                record = pending_record(row, row.get("item_sha256"), "not_started",
                                                        "stopped after quota before this duplicate was judged")
                            elif result["status"] == "complete":
                                record["cached"] = True
                        finished[index] = record
                write_report(running=True)
                refill()
        # A quota response stops new scheduling, while already-running requests finish.
        for indices in queued:
            for index in indices:
                row = rows[index]
                finished[index] = pending_record(row, row.get("item_sha256"), "not_started",
                                                 "stopped after quota before this item was judged")
        return write_report(running=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--ids-file", type=Path)
    parser.add_argument("--workers", type=int, default=1, help="bounded concurrent requests (1..16); default serial")
    parser.add_argument("--cache-from", type=Path, action="append", default=[],
                        help="prior admission output directory with cache/; repeatable, validated read-only")
    args = parser.parse_args()
    report = run(args.input, args.output, limit=args.limit, timeout=args.timeout, ids_file=args.ids_file,
                 workers=args.workers, cache_from=tuple(args.cache_from))
    print(json.dumps(report["counts"], ensure_ascii=False, sort_keys=True))
    return 2 if report["counts"]["pending"] else 0


if __name__ == "__main__":
    sys.exit(main())
