#!/usr/bin/env bash
set -euo pipefail

CFG="${CFG:-/work/config.yml}"
REPO_ROOT="${REPO_ROOT:?REPO_ROOT env missing}"
export CFG REPO_ROOT

python3 - <<'PY'
import os
import pathlib
import subprocess

import yaml

cfg = yaml.safe_load(open(os.environ["CFG"], "r", encoding="utf-8")) or {}
repo_root = pathlib.Path(os.environ["REPO_ROOT"])
base = repo_root / "apk"
base.mkdir(parents=True, exist_ok=True)

for distro in cfg.get("apk", []) or []:
    if not distro.get("enabled", False):
        continue

    name = str(distro["name"]).strip()
    for mirror in distro.get("mirrors", []) or []:
        if not mirror.get("enabled", True):
            continue

        mirror_name = str(mirror["mirror_name"]).strip()
        rsync_url = str(mirror["rsync_url"]).rstrip("/")
        target = base / name / mirror_name
        target.mkdir(parents=True, exist_ok=True)

        architectures = mirror.get("architectures", distro.get("architectures", [])) or []
        if not architectures:
            architectures = [None]

        for arch in architectures:
            source = rsync_url + "/"
            destination = target
            if arch:
                source = rsync_url + "/" + str(arch).strip("/") + "/"
                destination = target / str(arch)
                destination.mkdir(parents=True, exist_ok=True)

            # Delay updates/deletes until the transfer is substantially complete so a
            # repository being served during sync spends less time in a mixed state.
            cmd = [
                "rsync",
                "-aH",
                "--partial",
                "--delay-updates",
                "--delete-delay",
                source,
                str(destination) + "/",
            ]
            print("+", " ".join(cmd), flush=True)
            subprocess.run(cmd, check=True)
PY
