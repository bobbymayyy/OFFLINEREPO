#!/usr/bin/env python3
import os, sys, yaml, subprocess, pathlib

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

    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    rpm_sets = cfg.get("rpm", [])
    base = pathlib.Path(repo_root) / "rpm"
    base.mkdir(parents=True, exist_ok=True)

    for distro in rpm_sets:
        if not distro.get("enabled", False):
            continue

        name = str(distro["name"]).strip()
        if only_name and name.lower() != only_name:
            continue

        releasever = str(distro.get("releasever", ""))
        arch = distro.get("arch", "x86_64")
        repos = distro.get("repos", [])

        outdir = base / name / releasever
        outdir.mkdir(parents=True, exist_ok=True)

        for r in repos:
            repoid = r["repoid"]

            cmd = [
                "dnf", "-y",
                "--releasever", releasever,
                "--forcearch", arch,
                "--setopt=metadata_expire=0",
                "--setopt=keepcache=1",
                "reposync",
                "--repoid", repoid,
                "--download-path", str(outdir),
                "--download-metadata",
                "--downloadcomps",
                "--delete",
            ]
            run(cmd)

            repo_path = outdir / repoid
            if repo_path.exists():
                run(["createrepo_c", "--update", str(repo_path)], check=False)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
