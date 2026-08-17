#!/usr/bin/env bash
set -euo pipefail

CFG="${CFG:-/work/config.yml}"
REPO_ROOT="${REPO_ROOT:?REPO_ROOT env missing}"
export CFG REPO_ROOT

python3 - <<'PY'
import os
import pathlib
import subprocess
import sys

import yaml

sys.path.insert(0, "/work/lib")
from unit_state import record_unit

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

            # rsync's normal quick-check skips payload data when destination size
            # and mtime already match the source. Keep partial transfers in a
            # hidden side directory so an interrupted .apk never looks complete.
            # Do not use --ignore-existing: Alpine indexes must still refresh and
            # a same-path upstream correction must be allowed to replace old data.
            cmd = [
                "rsync",
                "-aH",
                "--partial-dir=.rsync-partial",
                "--delay-updates",
                "--delete-delay",
                source,
                str(destination) + "/",
            ]
            print("+", " ".join(cmd), flush=True)
            subprocess.run(cmd, check=True)

        record_unit(
            repo_root,
            family="apk",
            profile=name,
            name=mirror_name,
            relative_path=target.relative_to(repo_root).as_posix(),
            metadata={
                "rsync_url": rsync_url,
                "architectures": [str(x) for x in architectures if x is not None],
            },
        )
PY
