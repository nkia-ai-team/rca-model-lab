from pathlib import Path

import yaml

from rca_lab.scenarios import load_scenarios


def test_load_from_fixture(tmp_path: Path):
    d = tmp_path / "scenarios" / "services" / "shop"
    (d / "scripts").mkdir(parents=True)
    (d / "scripts" / "s1.sh").write_text("#!/bin/bash\necho hi\n")
    (d / "service-spec.yaml").write_text(
        yaml.safe_dump(
            {
                "service": {"name": "shop"},
                "scenarios": [
                    {
                        "id": "scenario-01",
                        "file": "s1.sh",
                        "title": "T",
                        "root_cause": "RC",
                        "expected_alarms": ["a1"],
                        "difficulty": 3,
                    }
                ],
            }
        )
    )
    [s] = load_scenarios(tmp_path)
    assert s.key == "shop/scenario-01"
    assert s.root_cause == "RC" and s.difficulty == 3
    assert "echo hi" in s.script_text()
