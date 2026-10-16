"""Bind completed Polestar data to the Qwen recipe and launch on the reserved KT GPU."""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import tarfile
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

REMOTE_RUNNER = '''import json,netrc,os,time
from pathlib import Path
from rca_lab.train.mcp_sft import load_config,load_rows,build_examples,train
from rca_lab.train.checkpoint import load_training_tokenizer
runtime=Path(os.environ['TRAIN_RUNTIME'])
config_path=Path(os.environ['TRAIN_CONFIG'])
def state(phase,**extra):
 (runtime/'training-state.json').write_text(json.dumps({'phase':phase,'utc':time.time(),'pid':os.getpid(),**extra},indent=2))
try:
 state('preprocessing')
 auth=netrc.netrc(runtime/'wandb.netrc').authenticators('api.wandb.ai')
 os.environ['WANDB_API_KEY']=auth[2]
 config=load_config(config_path)
 rows=load_rows(config)
 tokenizer=load_training_tokenizer(config)
 examples,manifest=build_examples(config,tokenizer,rows,return_manifest=True)
 (runtime/'preprocess-probe.json').write_text(json.dumps(manifest,indent=2))
 if any(item['dropped_by_length'] for item in manifest['drops']):
  raise RuntimeError('Training length would drop turns; adjust recipe before training')
 state('training',examples=len(examples),dataset_sha256=config.dataset_sha256)
 train(config_path)
 state('complete',output_dir=config.output_dir,dataset_sha256=config.dataset_sha256)
except Exception as exc:
 state('failed',error=str(exc))
 raise
'''


def launch(path: Path) -> int:
    cfg = yaml.safe_load(path.read_text())
    marker_path, dataset = ROOT / cfg["marker"], ROOT / cfg["dataset"]
    if not marker_path.exists() or not dataset.exists():
        print("waiting for completed Polestar dataset")
        return 75
    marker = json.loads(marker_path.read_text())
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    if digest != marker["sha256"] or marker["cases"] != 44:
        raise ValueError("dataset completion identity/count mismatch")
    remote = cfg["remote"]
    ssh = ["ssh", "-i", remote["key"], "-p", str(remote["port"]), "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           f"{remote['user']}@{remote['host']}"]
    result = subprocess.run([*ssh, "cat " + shlex.quote(remote["runtime"] + "/probe-state.json")], capture_output=True, text=True, check=False)
    if result.returncode or json.loads(result.stdout).get("phase") != "passed":
        print("waiting for Qwen tokenizer/load/backward runtime probe")
        return 75
    memory = subprocess.check_output([*ssh, f"nvidia-smi -i {int(remote['gpu'])} --query-gpu=memory.used --format=csv,noheader,nounits"], text=True).strip()
    if int(memory) > 1024:
        print("waiting for reserved GPU to become free")
        return 75
    workspace = ROOT / cfg["workspace"]
    bound = yaml.safe_load((ROOT / cfg["template"]).read_text())
    bound.update(dataset_sha256=digest, expected_examples=marker["examples"], expected_cases=marker["cases"])
    config_path = workspace / "bound-training.yaml"
    config_path.write_text(yaml.safe_dump(bound, sort_keys=False))
    runner = workspace / "remote_train.py"
    runner.write_text(REMOTE_RUNNER)
    with tempfile.TemporaryFile() as archive_file:
        with tarfile.open(fileobj=archive_file, mode="w") as archive:
            for local in [dataset, config_path, runner, ROOT / "src/rca_lab", ROOT / "scripts/train_mcp_sft.py"]:
                archive.add(local, arcname=str(local.relative_to(ROOT)), filter=lambda item: None if "__pycache__" in item.name else item)
        archive_file.seek(0)
        subprocess.run([*ssh, "tar -xf - -C " + shlex.quote(remote["root"])], stdin=archive_file, check=True)
    remote_config = remote["root"] + "/" + str(config_path.relative_to(ROOT))
    remote_runner = remote["root"] + "/" + str(runner.relative_to(ROOT))
    code = f'''import os,subprocess,json
from pathlib import Path
runtime=Path({remote['runtime']!r})
pidfile=runtime/'training.pid'
if pidfile.exists() and Path('/proc',pidfile.read_text().strip()).exists():
 raise RuntimeError('training process already exists')
env=dict(os.environ,CUDA_VISIBLE_DEVICES={str(remote['gpu'])!r},PYTHONPATH='src',TRAIN_RUNTIME=str(runtime),TRAIN_CONFIG={remote_config!r})
proc=subprocess.Popen([{remote['python']!r},{remote_runner!r}],cwd={remote['root']!r},env=env,stdin=subprocess.DEVNULL,stdout=(runtime/'training.log').open('a'),stderr=subprocess.STDOUT,start_new_session=True)
pidfile.write_text(str(proc.pid))
print(json.dumps({{'pid':proc.pid,'dataset_sha256':{digest!r}}}))
'''
    result = subprocess.run([*ssh, "python3 -"], input=code, text=True, capture_output=True, check=True)
    (workspace / "launch.json").write_text(result.stdout)
    print(result.stdout)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    raise SystemExit(launch(parser.parse_args().config))
