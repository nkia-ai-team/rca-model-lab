from pathlib import Path

from rca_lab.data.io import read_jsonl, write_jsonl
from rca_lab.data.schema import Message, RcaSample


def test_roundtrip(tmp_path: Path):
    s = RcaSample(
        id="abc",
        scenario_key="d/s-01",
        ground_truth="x",
        messages=[Message(role="user", content="u"), Message(role="assistant", content="a")],
    )
    p = tmp_path / "x.jsonl"
    assert write_jsonl(p, [s]) == 1
    assert list(read_jsonl(p)) == [s]
    assert s.to_trl() == {
        "messages": [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
    }
