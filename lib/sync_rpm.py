#!/usr/bin/env python3
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import urllib.request

import yaml

from unit_state import record_unit


def stop_process_group(proc: subprocess.Popen, grace_sec: int = 10) -> None:
    """Forward an interrupt to the whole child process group, then reap it."""
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

    print("! child did not stop after SIGINT; sending SIGTERM", file=sys.stderr, flush=True)
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
        print("! child did not stop after SIGTERM; sending SIGKILL", file=sys.stderr, flush=True)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                return
        proc.wait()


def run(cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, start_new_session=True)
    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        print(
            "! interrupted: stopping reposync cleanly; completed RPMs remain in place and will be reused on restart",
            file=sys.stderr,
            flush=True,
        )
        stop_process_group(proc)
        raise

    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd)
    return subprocess.CompletedProcess(cmd, returncode)


def main():
    cfg_path = os.environ.get("CFG", "/work/config.yml")
    repo_root = os.environ.get("REPO_ROOT")
    only_name = os.environ.get("ONLY_RPM_NAME", "").strip().lower()

    if not repo_root:
        print("REPO_ROOT env missing", file=sys.stderr)
        return 2

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    base = pathlib.Path(repo_root) / "rpm"
    base.mkdir(parents=True, exist_ok=True)

    for distro in cfg.get("rpm", []) or []:
        if not distro.get("enabled", False):
            continue

        name = str(distro["name"]).strip()
        if only_name and name.lower() != only_name:
            continue

        releasever = str(distro.get("releasever", ""))
        arch = str(distro.get("arch", "x86_64"))
        outdir = base / name / releasever
        outdir.mkdir(parents=True, exist_ok=True)

        for repo in distro.get("repos", []) or []:
            if not repo.get("enabled", True):
                continue

            repoid = str(repo["repoid"]).strip()
            baseurl = str(repo.get("baseurl", "")).strip()
            gpgkey_url = str(repo.get("gpgkey_url", "")).strip()
            local_gpgkey = None

            if gpgkey_url:
                key_dir = pathlib.Path(repo_root) / "keys" / "rpm"
                key_dir.mkdir(parents=True, exist_ok=True)
                local_gpgkey = key_dir / f"{name}-{repoid}.pub"
                tmp_key = local_gpgkey.with_suffix(local_gpgkey.suffix + ".tmp")
                print(f"+ refresh {gpgkey_url} -> {local_gpgkey}", flush=True)
                with urllib.request.urlopen(gpgkey_url, timeout=60) as response:
                    tmp_key.write_bytes(response.read())
                if tmp_key.stat().st_size < 256:
                    tmp_key.unlink(missing_ok=True)
                    raise RuntimeError(f"Downloaded GPG key looks too small: {gpgkey_url}")
                tmp_key.replace(local_gpgkey)

            dnf_bin = shutil.which("dnf5") or shutil.which("dnf")
            if not dnf_bin:
                raise RuntimeError("Neither dnf5 nor dnf is available")
            is_dnf5 = pathlib.Path(dnf_bin).name == "dnf5"

            cmd = [dnf_bin, "-y", "--refresh"]
            if releasever:
                cmd += ["--releasever", releasever]
            if arch:
                cmd += ["--forcearch", arch]
            if baseurl:
                cmd += [f"--repofrompath={repoid},{baseurl}"]
            if local_gpgkey is not None:
                cmd += [
                    f"--setopt={repoid}.gpgcheck=1",
                    f"--setopt={repoid}.gpgkey=file://{local_gpgkey}",
                ]

            # reposync is deliberately incremental: both DNF4 and DNF5 avoid
            # re-downloading RPM payloads that are already present in the
            # destination. --remote-time preserves upstream timestamps.
            #
            # The first pass downloads payloads only. It does not refresh the
            # published repodata and does not delete stale packages, so Ctrl+C
            # leaves any previously complete repository metadata usable. Once
            # every current payload is present, the second pass refreshes
            # metadata and performs the stale-package prune.
            cmd += [
                "reposync",
                "--repoid", repoid,
                "--arch", arch,
                "--arch", "noarch",
                "--destdir" if is_dnf5 else "--download-path", str(outdir),
                "--remote-time",
            ]

            if repo.get("verify_packages", False):
                cmd.append("--gpgcheck")

            print(
                f"\n=== RPM {name}/{repoid}: incremental payload sync ===\n"
                "Completed RPMs are reused after restart; existing repodata and stale packages are left untouched until this pass succeeds.",
                flush=True,
            )
            run(cmd)

            print(
                f"\n=== RPM {name}/{repoid}: metadata refresh and stale-package prune ===",
                flush=True,
            )
            run(cmd + ["--download-metadata", "--delete"])

            # DNF reposync stores each repository under a subdirectory named
            # after its repo ID. That directory is a complete, independently
            # transferable repository unit. It is recorded only after both the
            # payload sync and metadata/prune pass complete successfully.
            unit_dir = outdir / repoid
            if not unit_dir.is_dir():
                raise RuntimeError(
                    f"reposync completed but expected repository directory is missing: {unit_dir}"
                )
            record_unit(
                repo_root,
                family="rpm",
                profile=name,
                name=repoid,
                relative_path=unit_dir.relative_to(pathlib.Path(repo_root)).as_posix(),
                metadata={
                    "repoid": repoid,
                    "releasever": releasever,
                    "architecture": arch,
                    "baseurl": baseurl or None,
                    "package_gpgcheck": bool(repo.get("verify_packages", False)),
                    "gpgkey_url": gpgkey_url or None,
                },
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
