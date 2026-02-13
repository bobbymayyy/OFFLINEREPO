#!/usr/bin/env python3
import os, sys, yaml, subprocess, pathlib

def run(cmd, check=True):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check)

def main():
    cfg_path = os.environ.get("CFG", "/work/config.yml")
    repo_root = os.environ.get("REPO_ROOT")
    if not repo_root:
        print("REPO_ROOT env missing", file=sys.stderr)
        return 2

    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f)

    rpm_sets = cfg.get("rpm", [])
    base = pathlib.Path(repo_root) / "rpm"
    base.mkdir(parents=True, exist_ok=True)

    for distro in rpm_sets:
        if not distro.get("enabled", False):
            continue
        name = distro["name"]
        releasever = str(distro.get("releasever", ""))
        arch = distro.get("arch", "x86_64")
        repos = distro.get("repos", [])

        outdir = base / name / releasever
        outdir.mkdir(parents=True, exist_ok=True)

        # reposync each repoid
        for r in repos:
            repoid = r["repoid"]
            target = outdir / repoid
            target.mkdir(parents=True, exist_ok=True)

            cmd = [
                "dnf", "-y",
                "--releasever", releasever,
                "--forcearch", arch,
                "reposync",
                "--repoid", repoid,
                "--download-path", str(target),
                "--download-metadata",
                "--delete",
            ]
            run(cmd)

            # If metadata isn't where clients expect or you later curate packages,
            # you can regenerate repo metadata:
            # createrepo_c --update <path>
            # run(["createrepo_c", "--update", str(target)])

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
