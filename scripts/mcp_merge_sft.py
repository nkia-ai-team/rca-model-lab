"""Merge exported SFT jsonl sets into one training file with a manifest (two-stage SFT, 2026-10-06).

  uv run python scripts/mcp_merge_sft.py --out data/processed/mcp-sft-pub-v1.jsonl data/processed/mcp-sft-pub-tt4b.jsonl ...

Rows are copied verbatim; the manifest records each source's sha256 and counts so the training config can pin
expected_examples / expected_cases / dataset_sha256. Case ids must not collide across sources.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("sources", nargs="+", type=Path)
    args = parser.parse_args()
    rows, sources, owner = [], [], {}
    for src in args.sources:
        part = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in part:
            prev = owner.setdefault(row["case_id"], src.name)
            if prev != src.name:
                raise SystemExit(f"case id {row['case_id']} appears in both {prev} and {src.name}")
        supervised = sum(1 for row in part for m in row["messages"] if m.get("role") == "assistant" and m.get("supervise") is not False)
        sources.append({"path": str(src), "sha256": sha256(src), "examples": len(part), "cases": len({r["case_id"] for r in part}), "supervised_turns": supervised})
        rows.extend(part)
    args.out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    manifest = {"examples": len(rows), "cases": len({r["case_id"] for r in rows}), "supervised_turns": sum(s["supervised_turns"] for s in sources),
                "dataset_sha256": sha256(args.out), "sources": sources}
    args.out.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("examples", "cases", "supervised_turns", "dataset_sha256")}))


if __name__ == "__main__":
    main()
