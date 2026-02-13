#!/usr/bin/env python3
import os, sys, yaml, subprocess, time, pathlib, random

def run(cmd, check=True, retries=0, retry_sleep=5):
    attempt = 0
    while True:
        attempt += 1
        print("+", " ".join(cmd), flush=True)
        try:
            return subprocess.run(cmd, check=check)
        except subprocess.CalledProcessError as e:
            if attempt > retries:
                raise
            # jitter + backoff
            delay = retry_sleep * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            print(f"! command failed (attempt {attempt}/{retries+1}), retrying in {delay:.1f}s", file=sys.stderr, flush=True)
            time.sleep(delay)

def sh(cmd):
    return run(cmd.split())

def _norm_list(x):
    if x is None:
        return []
    if isinstance(x, str):
        return [x]
    if isinstance(x, list):
        return [str(i) for i in x]
    raise TypeError(f"Expected string or list, got {type(x)}")

def dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def compute_keyrings(cfg, distro_cfg, mirror_cfg):
    """
    Keyring resolution order:
      1) global.apt_keyrings_default
      2) distro.apt_keyrings
      3) mirror.keyrings (append or replace via mirror.keyrings_mode)
    """
    g = cfg.get("global", {}) or {}
    base = []
    base += _norm_list(g.get("apt_keyrings_default"))
    base += _norm_list(distro_cfg.get("apt_keyrings"))

    mirror_krs = _norm_list(mirror_cfg.get("keyrings"))
    mode = (mirror_cfg.get("keyrings_mode") or "append").lower()  # append|replace

    if mirror_krs:
        if mode == "replace":
            return dedupe(mirror_krs)
        if mode == "append":
            return dedupe(base + mirror_krs)
        raise ValueError(f"Unknown keyrings_mode={mode!r}; use 'append' or 'replace'")

    return dedupe(base)

def keyring_flags(keyrings):
    # aptly accepts repeated -keyring=/path/to/keyring.gpg flags
    return [f"-keyring={p}" for p in keyrings]

def main():
    cfg_path = os.environ.get("CFG", "/work/config.yml")
    repo_root = os.environ.get("REPO_ROOT")
    if not repo_root:
        print("REPO_ROOT env missing", file=sys.stderr)
        return 2

    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f)

    # aptly state inside repo_root to keep portability
    aptly_root = pathlib.Path(repo_root) / "state" / "aptly"
    aptly_root.mkdir(parents=True, exist_ok=True)

    os.environ["APTLY_ROOT_DIR"] = str(aptly_root)

    apt_sets = cfg.get("apt", [])
    keep_n = int(cfg.get("global", {}).get("keep_snapshots", 3))
    publish_root = pathlib.Path(repo_root) / "apt"
    publish_root.mkdir(parents=True, exist_ok=True)

    # Ensure aptly config uses publish root
    # We'll publish to <repo_root>/apt/<distro>/<distribution>/
    # Using "prefix" as apt/<distro>
    for distro in apt_sets:
        if not distro.get("enabled", False):
            continue
        distro_name = distro["name"]
        mirrors = distro.get("mirrors", [])

        prefix = f"apt/{distro_name}"
        for m in mirrors:
            mirror_name = m["mirror_name"]
            url = m["url"]
            dist = m["distribution"]
            comps = m.get("components", [])
            archs = m.get("architectures", cfg.get("global", {}).get("architectures", []))
            arch_flag = ",".join(archs) if archs else ""
            
            krs = compute_keyrings(cfg, distro, m)
            if not krs:
                raise RuntimeError(
                    f"No keyrings configured for mirror {mirror_name} ({url}). "
                    "Set global.apt_keyrings_default and/or per-distro/per-mirror keyrings."
                )
            kr_flags = keyring_flags(krs)
 
            # Create mirror if missing
            # aptly mirror show <name> exits 0 if exists
            res = subprocess.run(["aptly", "mirror", "show", mirror_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode != 0:
                cmd = ["aptly", "mirror", "create"] + kr_flags
                if arch_flag:
                    cmd += ["-architectures=" + arch_flag]
                cmd += [mirror_name, url, dist] + comps
                run(cmd)

            # Update mirror
            run(["aptly","mirror","update"] + kr_flags + [mirror_name], retries=5, retry_sleep=10)

            # Snapshot with timestamp
            ts = time.strftime("%Y%m%d-%H%M%S")
            snap = f"{mirror_name}-{ts}"
            run(["aptly", "snapshot", "create", snap, "from", "mirror", mirror_name])

            # Publish snapshot (switch if already published)
            # Publish path: <prefix>/<dist>
            pub_path = f"{prefix}/{dist}"
            # "aptly publish show" returns 0 if exists
            res = subprocess.run(["aptly", "publish", "show", dist, pub_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0:
                run(["aptly", "publish", "switch", dist, pub_path, snap])
            else:
                # Publish snapshot to filesystem root; clients will use HTTP on top
                run(["aptly", "publish", "snapshot", "-distribution=" + dist, snap, pub_path])

            # Cleanup older snapshots for this mirror
            # list snapshots matching mirror_name-*
            out = subprocess.check_output(["aptly", "snapshot", "list", "-raw"]).decode().splitlines()
            matching = sorted([s for s in out if s.startswith(mirror_name + "-")])
            if len(matching) > keep_n:
                to_drop = matching[:len(matching)-keep_n]
                for s in to_drop:
                    run(["aptly", "snapshot", "drop", s], check=False)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
