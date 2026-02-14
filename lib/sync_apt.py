#!/usr/bin/env python3
import os, sys, yaml, subprocess, time, pathlib, random, re
from typing import List, Optional, Dict, Any


RETRYABLE_PATTERNS = [
    r"i/o timeout",
    r"connection timed out",
    r"temporary failure",
    r"temporary failure resolving",
    r"connection reset",
    r"TLS handshake timeout",
    r"EOF",
    r"503 Service Unavailable",
    r"502 Bad Gateway",
    r"504 Gateway Time-out",
]

def _is_retryable(stderr: str, stdout: str) -> bool:
    s = (stderr or "") + "\n" + (stdout or "")
    s = s.lower()
    return any(re.search(p, s) for p in RETRYABLE_PATTERNS)

def run(
    cmd: List[str],
    check: bool = True,
    retries: int = 0,
    retry_sleep: int = 5,
    timeout: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """
    Run command with optional retries, capturing output for better error reporting.
    Retries only happen when stderr/stdout looks like a transient network error.
    """
    attempt = 0
    last_exc = None
    while True:
        attempt += 1
        print("+", " ".join(cmd), flush=True)
        try:
            cp = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if cp.returncode == 0:
                return cp

            # Non-zero
            if retries > 0 and attempt <= (retries + 1) and _is_retryable(cp.stderr, cp.stdout):
                delay = retry_sleep * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
                print(
                    f"! retryable failure (attempt {attempt}/{retries+1}), sleeping {delay:.1f}s\n"
                    f"  exit={cp.returncode}\n  stderr={cp.stderr.strip()[:500]}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                continue

            if check:
                # Print useful context then raise
                print(f"! command failed exit={cp.returncode}", file=sys.stderr, flush=True)
                if cp.stdout:
                    print("! stdout:\n" + cp.stdout[-2000:], file=sys.stderr, flush=True)
                if cp.stderr:
                    print("! stderr:\n" + cp.stderr[-2000:], file=sys.stderr, flush=True)
                raise subprocess.CalledProcessError(cp.returncode, cmd, output=cp.stdout, stderr=cp.stderr)
            return cp

        except subprocess.TimeoutExpired as e:
            last_exc = e
            if attempt > retries + 1:
                raise
            delay = retry_sleep * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            print(f"! timeout (attempt {attempt}/{retries+1}), sleeping {delay:.1f}s", file=sys.stderr, flush=True)
            time.sleep(delay)

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
    return [f"-keyring={p}" for p in keyrings]

def must_get(d: Dict[str, Any], key: str, ctx: str) -> Any:
    if key not in d or d[key] in (None, ""):
        raise ValueError(f"Missing required key '{key}' in {ctx}")
    return d[key]

def ensure_paths_exist(paths: List[str], ctx: str):
    missing = [p for p in paths if not pathlib.Path(p).exists()]
    if missing:
        raise FileNotFoundError(f"{ctx}: missing keyring files: {missing}")

def setup_gpg(repo_root: str, cfg: Dict[str, Any]) -> str:
    """
    Make GNUPGHOME persistent on the drive and import the signing key from:
      <repo_root>/keys/repo-signing-private.asc
    Returns gpg_key fingerprint/id to use for aptly publish -gpg-key=...
    """
    g = cfg.get("global", {}) or {}
    gpg_key = g.get("aptly_gpg_key")
    if not gpg_key:
        raise RuntimeError("global.aptly_gpg_key must be set to sign published repos")

    gnupg_home = pathlib.Path(repo_root) / "state" / "gnupg"
    gnupg_home.mkdir(parents=True, exist_ok=True)
    os.chmod(gnupg_home, 0o700)
    os.environ["GNUPGHOME"] = str(gnupg_home)

    key_file = pathlib.Path(repo_root) / "keys" / "repo-signing-private.asc"
    if not key_file.exists():
        raise FileNotFoundError(f"Signing key not found: {key_file} (expected on removable drive)")

    # Import (safe to repeat)
    run(["gpg", "--batch", "--import", str(key_file)], check=True, retries=2, retry_sleep=2, timeout=60)

    # Sanity check that a secret key exists
    cp = run(["gpg", "--batch", "--list-secret-keys", "--keyid-format", "LONG"], check=True, timeout=30)
    if "sec" not in (cp.stdout or ""):
        raise RuntimeError("No secret keys available in GNUPGHOME after import (gpg --list-secret-keys shows none)")

    return str(gpg_key)

def main():
    cfg_path = os.environ.get("CFG", "/work/config.yml")
    repo_root = os.environ.get("REPO_ROOT")
    if not repo_root:
        print("REPO_ROOT env missing", file=sys.stderr)
        return 2

    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    # Portability roots
    aptly_root = pathlib.Path(repo_root) / "state" / "aptly"
    aptly_root.mkdir(parents=True, exist_ok=True)
    os.environ["APTLY_ROOT_DIR"] = str(aptly_root)

    publish_root = pathlib.Path(repo_root) / "apt"
    publish_root.mkdir(parents=True, exist_ok=True)

    g = cfg.get("global", {}) or {}
    keep_n = int(g.get("keep_snapshots", 3))
    fail_fast = bool(g.get("fail_fast", False))  # optional
    cmd_timeout = int(g.get("cmd_timeout_sec", 0)) or None

    # Set up signing (import key, ensure secret key exists)
    gpg_key = setup_gpg(repo_root, cfg)
    pub_gpg_flags = [f"-gpg-key={gpg_key}"]

    apt_sets = cfg.get("apt", []) or []
    failures = []

    for distro in apt_sets:
        if not distro.get("enabled", False):
            continue

        distro_name = must_get(distro, "name", "apt distro")
        mirrors = distro.get("mirrors", []) or []
        prefix = f"apt/{distro_name}"

        for m in mirrors:
            mirror_name = must_get(m, "mirror_name", f"apt[{distro_name}].mirrors[]")
            url = must_get(m, "url", f"mirror {mirror_name}")
            dist = must_get(m, "distribution", f"mirror {mirror_name}")
            comps = m.get("components", []) or []
            archs = m.get("architectures", g.get("architectures", [])) or []
            arch_flag = ",".join(archs) if archs else ""

            try:
                # Keyrings for verifying upstream
                krs = compute_keyrings(cfg, distro, m)
                if not krs:
                    raise RuntimeError(
                        f"No keyrings configured for mirror {mirror_name} ({url}). "
                        "Set global.apt_keyrings_default and/or per-distro/per-mirror keyrings."
                    )
                ensure_paths_exist(krs, f"mirror {mirror_name}")
                kr_flags = keyring_flags(krs)

                # Create mirror if missing
                res = run(["aptly", "mirror", "show", mirror_name], check=False, timeout=cmd_timeout)
                if res.returncode != 0:
                    cmd = ["aptly", "mirror", "create"] + kr_flags
                    if arch_flag:
                        cmd += ["-architectures=" + arch_flag]
                    cmd += [mirror_name, url, dist] + comps
                    run(cmd, timeout=cmd_timeout)

                # Update mirror (retryable)
                run(
                    ["aptly", "mirror", "update"] + kr_flags + [mirror_name],
                    retries=5,
                    retry_sleep=10,
                    timeout=cmd_timeout,
                )

                # Snapshot
                ts = time.strftime("%Y%m%d-%H%M%S")
                snap = f"{mirror_name}-{ts}"
                run(["aptly", "snapshot", "create", snap, "from", "mirror", mirror_name], timeout=cmd_timeout)

                # Publish (switch if exists)
                pub_path = f"{prefix}/{dist}"
                res = run(["aptly", "publish", "show", dist, pub_path], check=False, timeout=cmd_timeout)
                if res.returncode == 0:
                    run(["aptly", "publish", "switch"] + pub_gpg_flags + [dist, pub_path, snap], timeout=cmd_timeout)
                else:
                    run(
                        ["aptly", "publish", "snapshot"] + pub_gpg_flags + ["-distribution=" + dist, snap, pub_path],
                        timeout=cmd_timeout,
                    )

                # Cleanup old snapshots (best-effort)
                out = run(["aptly", "snapshot", "list", "-raw"], timeout=cmd_timeout).stdout.splitlines()
                matching = sorted([s for s in out if s.startswith(mirror_name + "-")])
                if len(matching) > keep_n:
                    to_drop = matching[: len(matching) - keep_n]
                    for s in to_drop:
                        run(["aptly", "snapshot", "drop", s], check=False, timeout=cmd_timeout)

            except Exception as e:
                failures.append((distro_name, mirror_name, str(e)))
                print(f"! FAILED {distro_name}/{mirror_name}: {e}", file=sys.stderr, flush=True)
                if fail_fast:
                    break

        if fail_fast and failures:
            break

    if failures:
        print("\n=== FAILURES ===", file=sys.stderr)
        for distro_name, mirror_name, err in failures:
            print(f"- {distro_name}/{mirror_name}: {err}", file=sys.stderr)
        return 1

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
