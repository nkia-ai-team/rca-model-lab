"""Switch the loop to data v5 and add the new13 eval track (2026-10-23, user: "큰 평가 끝나면 반영하고 시작 시점 모델도 새 검증셋으로 재").

Run only after milestone 39 is recorded in state.json (the waiter apply_v5_waiter.sh does that). The loop re-executes at its
next phase boundary; measure_new_track_references() then measures the start policy on new13 before the next regular eval.
"""
import json
import os
import shutil
import time
from pathlib import Path

WORK = Path(__file__).resolve().parents[1]
V5 = WORK / "data-v5"
CONTRACT = "configs/eval/train-family-v5-expanded-20261023.yaml"
REMOTE = "/home/work/flash-rl-quant-probe-20261014/data-v5"

cfg_path = WORK / "config.json"
cfg = json.loads(cfg_path.read_text())
if cfg["data"]["version"] == "v5":
    raise SystemExit("already on v5")
shutil.copy2(cfg_path, WORK / f"config.json.pre-v5-{time.strftime('%Y%m%dT%H%M%S')}")

split = yaml_split = None
import yaml  # noqa: E402
split = yaml.safe_load((V5 / "split-v5-20261023.yaml").read_text())
cfg["data"] = {**cfg["data"], "version": "v5",
               "local_preparation": str(V5 / "preparation-v5-20261023.json"),
               "remote_preparation": f"{REMOTE}/preparation-v5-20261023.json",
               "remote_split_source": f"{REMOTE}/split-v5-20261023.yaml"}
cfg["eval"]["tracks"]["new13"] = {
    "runs": 2,
    "items": [{"case": c, "cases_root": "/data/eval-cases", "contract": CONTRACT, "split": str(V5 / "split-v5-20261023.yaml")}
              for c in split["validation_new13"]],
}
cfg["data_v5_note"] = ("2026-10-23 user: data v5 (train 59 -> 70, new validation track new13 = families f60, f63-f69 never trained; "
                       "validation10 kept). Start-policy reference on new13 measured on first use.")
tmp = cfg_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
os.replace(tmp, cfg_path)
print("applied v5:", len(cfg["eval"]["tracks"]["new13"]["items"]), "new13 cases")

# 2026-10-23 user ("평가 다 끝나고 넣어"): per-case judging inside evaluate() (data-v5/loop.py.prejudge), installed only over the
# loop.py it was derived from, so a later operator edit is never overwritten.
import hashlib  # noqa: E402
BASE_SHA = "3eaebf4b67cc1bfe37132e1d41ea80b1703ed6ce17348116f1bec6d8adcf414e"
loop_path = WORK / "loop.py"
if hashlib.sha256(loop_path.read_bytes()).hexdigest() == BASE_SHA:
    shutil.copy2(loop_path, WORK / f"loop.py.pre-prejudge-{time.strftime('%Y%m%dT%H%M%S')}")
    tmp = loop_path.with_suffix(".py.tmp")
    tmp.write_text((V5 / "loop.py.prejudge").read_text())
    os.replace(tmp, loop_path)
    print("installed loop.py: per-case judging, tunnel keeper thread, eval retries/failure count, milestone repair")
    # the loop now keeps the tunnel itself; the stop-gap keeper (tunnel_keeper.sh) would only race it
    import subprocess  # noqa: E402
    subprocess.run(["pkill", "-f", "tunnel_keeper.sh"], check=False)
else:
    print("loop.py changed since the patch was made; prejudge patch NOT installed")
