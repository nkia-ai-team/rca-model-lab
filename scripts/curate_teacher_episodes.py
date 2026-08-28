#!/usr/bin/env python3
"""Build an auditable, corrected teacher source tree from immutable episodes."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from rca_lab.data.curation import TeacherCurationSpec, curate_teacher_episodes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()

    spec_bytes = args.spec.read_bytes()
    spec = TeacherCurationSpec.model_validate(yaml.safe_load(spec_bytes))
    manifest = curate_teacher_episodes(
        source_root=args.source_root,
        output_root=args.output_root,
        spec=spec,
        spec_bytes=spec_bytes,
    )
    print(
        f"curated {manifest.trajectory_count} trajectories across "
        f"{manifest.scenario_count} scenarios -> {args.output_root}"
    )


if __name__ == "__main__":
    main()
