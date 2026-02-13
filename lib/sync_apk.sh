#!/usr/bin/env bash
set -euo pipefail

CFG="${CFG:-/work/config.yml}"
REPO_ROOT="${REPO_ROOT:?REPO_ROOT env missing}"

python3 - <<'PY'
import os, yaml
cfg = yaml.safe_load(open(os.environ["CFG"], "r"))
apk = cfg.get("apk", [])
enabled = []
for d in apk:
    if d.get("enabled", False):
        enabled.append(d)
print(len(enabled))
PY

# We will parse via python to avoid depending on yq
python3 - <<'PY'
import os, yaml, subprocess, pathlib, sys
cfg = yaml.safe_load(open(os.environ["CFG"], "r"))
repo_root = pathlib.Path(os.environ["REPO_ROOT"])
base = repo_root / "apk"
base.mkdir(parents=True, exist_ok=True)

for distro in cfg.get("apk", []):
    if not distro.get("enabled", False):
        continue
    name = distro["name"]
    mirrors = distro.get("mirrors", [])
    for m in mirrors:
        mirror_name = m["mirror_name"]
        rsync_url = m["rsync_url"]
        target = base / name / mirror_name
        target.mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-av", "--delete", rsync_url + "/", str(target) + "/"]
        print("+", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True)
PY
