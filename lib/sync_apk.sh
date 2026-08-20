#!/usr/bin/env bash
set -euo pipefail

CFG="${CFG:-/work/config.yml}"
REPO_ROOT="${REPO_ROOT:?REPO_ROOT env missing}"
export CFG REPO_ROOT

exec python3 - <<'PY'
import os
import pathlib
import signal
import subprocess
import sys

import yaml

sys.path.insert(0, "/work/lib")
from unit_state import record_unit


def stop_process_group(proc: subprocess.Popen, grace_sec: int = 10) -> None:
    """Forward Ctrl+C to rsync and its children, then reap them."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return

    try:
        proc.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass

    print("! rsync did not stop after SIGINT; sending SIGTERM", file=sys.stderr, flush=True)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print("! rsync did not stop after SIGTERM; sending SIGKILL", file=sys.stderr, flush=True)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        proc.wait()


def run_rsync(cmd):
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, start_new_session=True)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        print(
            "! interrupted: stopping rsync cleanly; .rsync-partial data is retained and will be reused on restart",
            file=sys.stderr,
            flush=True,
        )
        stop_process_group(proc)
        raise

    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd)


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

            # rsync's quick-check skips payload data when destination size and
            # mtime already match the source. --partial-dir preserves an
            # interrupted transfer as a reusable basis file instead of exposing
            # it as a complete .apk. --delay-updates keeps replacements hidden
            # until the transfer completes, and --delete-delay postpones stale
            # deletion until the end of a successful transfer.
            cmd = [
                "rsync",
                "-aH",
                "--human-readable",
                "--info=progress2",
                "--stats",
                "--partial-dir=.rsync-partial",
                "--delay-updates",
                "--delete-delay",
                source,
                str(destination) + "/",
            ]
            print(
                f"\n=== APK {name}/{mirror_name}{'/' + str(arch) if arch else ''}: resumable rsync ===\n"
                "Completed files are skipped on restart and interrupted payloads remain under .rsync-partial.",
                flush=True,
            )
            run_rsync(cmd)

        # Only advertise the repository unit after every requested architecture
        # has completed successfully. An interrupted run leaves payload/state on
        # disk for the next invocation but does not claim a completed unit.
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
