#!/usr/bin/env python3
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request

import yaml


def run(cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)


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
                print(f"+ download {gpgkey_url} -> {local_gpgkey}", flush=True)
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

            cmd += [
                "reposync",
                "--repoid", repoid,
                "--arch", arch,
                "--arch", "noarch",
                "--destdir" if is_dnf5 else "--download-path", str(outdir),
                "--download-metadata",
                "--delete",
            ]

            if repo.get("verify_packages", False):
                cmd.append("--gpgcheck")

            run(cmd)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
