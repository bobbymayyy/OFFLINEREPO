#!/usr/bin/env python3
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import yaml

from unit_state import record_unit


RETRYABLE_PATTERNS = [
    r"i/o timeout",
    r"connection timed out",
    r"temporary failure",
    r"temporary failure resolving",
    r"connection reset",
    r"tls handshake timeout",
    r"\beof\b",
    r"503 service unavailable",
    r"502 bad gateway",
    r"504 gateway time-out",
]


def _is_retryable(stderr: str, stdout: str) -> bool:
    text = ((stderr or "") + "\n" + (stdout or "")).lower()
    return any(re.search(pattern, text) for pattern in RETRYABLE_PATTERNS)


def run(
    cmd: List[str],
    check: bool = True,
    retries: int = 0,
    retry_sleep: int = 5,
    timeout: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    attempt = 0
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

            if retries and attempt <= retries and _is_retryable(cp.stderr, cp.stdout):
                delay = retry_sleep * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
                print(
                    f"! retryable failure (attempt {attempt}/{retries + 1}), "
                    f"sleeping {delay:.1f}s\n"
                    f"  exit={cp.returncode}\n  stderr={(cp.stderr or '').strip()[:500]}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
                continue

            if check:
                print(f"! command failed exit={cp.returncode}", file=sys.stderr, flush=True)
                if cp.stdout:
                    print("! stdout:\n" + cp.stdout[-2000:], file=sys.stderr, flush=True)
                if cp.stderr:
                    print("! stderr:\n" + cp.stderr[-2000:], file=sys.stderr, flush=True)
                raise subprocess.CalledProcessError(
                    cp.returncode, cmd, output=cp.stdout, stderr=cp.stderr
                )
            return cp

        except subprocess.TimeoutExpired:
            if attempt > retries:
                raise
            delay = retry_sleep * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            print(
                f"! timeout (attempt {attempt}/{retries + 1}), sleeping {delay:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)


def _norm_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise TypeError(f"Expected string or list, got {type(value)}")


def dedupe(seq):
    seen = set()
    out = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def compute_keyrings(cfg, distro_cfg, mirror_cfg):
    """Return only the keyrings intended to authenticate this upstream."""
    global_keys = _norm_list((cfg.get("global", {}) or {}).get("apt_keyrings_default"))
    distro_keys = _norm_list(distro_cfg.get("apt_keyrings"))
    base = distro_keys if distro_keys else global_keys

    mirror_keys = _norm_list(mirror_cfg.get("keyrings"))
    if not mirror_keys:
        return dedupe(base)

    mode = str(mirror_cfg.get("keyrings_mode", "replace")).lower()
    if mode == "replace":
        return dedupe(mirror_keys)
    if mode == "append":
        return dedupe(base + mirror_keys)
    raise ValueError(f"Unknown keyrings_mode={mode!r}; use 'append' or 'replace'")


def keyring_flags(keyrings):
    return [f"-keyring={path}" for path in keyrings]


def must_get(mapping: Dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ValueError(f"Missing required key '{key}' in {context}")
    return mapping[key]


def ensure_paths_exist(paths: List[str], context: str):
    missing = [path for path in paths if not pathlib.Path(path).exists()]
    if missing:
        raise FileNotFoundError(f"{context}: missing keyring files: {missing}")


def _normalized_fingerprint(value: str) -> str:
    return re.sub(r"[^0-9A-F]", "", value.upper())


def setup_gpg(repo_root: str, cfg: Dict[str, Any]):
    """Import the publishing secret into an ephemeral GNUPGHOME.

    The secret key never gets written into repo_root. Only the public key is
    exported there for offline clients.
    """
    global_cfg = cfg.get("global", {}) or {}
    gpg_key = str(global_cfg.get("aptly_gpg_key", "")).strip()
    if not gpg_key or gpg_key.startswith("YOUR_"):
        raise RuntimeError(
            "global.aptly_gpg_key must contain the full fingerprint of the "
            "OFFLINEREPO publishing key"
        )

    key_file = pathlib.Path(
        os.environ.get(
            "OFFLINEREPO_SIGNING_KEY_FILE",
            "/run/secrets/offline-repo-signing-private.asc",
        )
    )
    if not key_file.is_file():
        raise FileNotFoundError(
            f"Signing key not found at {key_file}. Keep it on the sync host and "
            "mount it read-only into the container."
        )

    gnupg_home = pathlib.Path(tempfile.mkdtemp(prefix="offline-repo-gnupg-"))
    os.chmod(gnupg_home, 0o700)
    os.environ["GNUPGHOME"] = str(gnupg_home)

    try:
        run(
            ["gpg", "--batch", "--import", str(key_file)],
            retries=2,
            retry_sleep=2,
            timeout=60,
        )
        cp = run(
            [
                "gpg",
                "--batch",
                "--with-colons",
                "--fingerprint",
                "--list-secret-keys",
                gpg_key,
            ],
            timeout=30,
        )
        fingerprints = [
            line.split(":")[9]
            for line in (cp.stdout or "").splitlines()
            if line.startswith("fpr:") and len(line.split(":")) > 9
        ]
        requested = _normalized_fingerprint(gpg_key)
        if requested not in {_normalized_fingerprint(fp) for fp in fingerprints}:
            raise RuntimeError(
                f"Secret key fingerprint {gpg_key} was not found after import"
            )

        keys_dir = pathlib.Path(repo_root) / "keys"
        keys_dir.mkdir(parents=True, exist_ok=True)
        public_asc = keys_dir / "offline-repo-signing-public.asc"
        public_gpg = keys_dir / "offline-repo-signing-public.gpg"
        run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--armor",
                "--output",
                str(public_asc),
                "--export",
                gpg_key,
            ],
            timeout=30,
        )
        run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--output",
                str(public_gpg),
                "--export",
                gpg_key,
            ],
            timeout=30,
        )
    except Exception:
        shutil.rmtree(gnupg_home, ignore_errors=True)
        raise

    return gpg_key, gnupg_home


def copy_public_keys_to_unit(repo_root: str, unit_root: pathlib.Path) -> None:
    for name in ("offline-repo-signing-public.asc", "offline-repo-signing-public.gpg"):
        source = pathlib.Path(repo_root) / "keys" / name
        if source.is_file():
            shutil.copy2(source, unit_root / name)


def write_aptly_config(unit_root: pathlib.Path, cfg: Dict[str, Any]) -> pathlib.Path:
    """Create an aptly database and publish endpoint dedicated to one APT unit."""
    state_root = unit_root / ".state" / "aptly"
    state_root.mkdir(parents=True, exist_ok=True)

    global_cfg = cfg.get("global", {}) or {}
    aptly_cfg = {
        "rootDir": str(state_root),
        "downloadConcurrency": int(global_cfg.get("aptly_download_concurrency", 4)),
        "downloadSpeedLimit": int(
            global_cfg.get("aptly_download_speed_limit_kbps", 0)
        ),
        "architectures": global_cfg.get("architectures", []) or [],
        "gpgProvider": "gpg",
        "skipLegacyPool": True,
        "FileSystemPublishEndpoints": {
            "unit": {
                "rootDir": str(unit_root),
                "linkMethod": str(
                    global_cfg.get("aptly_publish_link_method", "hardlink")
                ),
            }
        },
    }

    cfg_path = unit_root / ".state" / "aptly.conf"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(aptly_cfg, indent=2) + "\n", encoding="utf-8")
    return cfg_path


def aptly_cmd(aptly_cfg_path: pathlib.Path, *args: str) -> List[str]:
    return ["aptly", f"-config={aptly_cfg_path}"] + list(args)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "component"


def mirror_specs(mirror_name: str, distribution: str, mirror_cfg: Dict[str, Any]):
    components = _norm_list(mirror_cfg.get("components"))
    flat = distribution in ("./", "/")

    if flat:
        return [
            {
                "aptly_name": mirror_name,
                "source_component": None,
                "publish_component": str(
                    mirror_cfg.get("publish_component", "main")
                ),
            }
        ]

    if not components:
        return [
            {
                "aptly_name": mirror_name,
                "source_component": None,
                "publish_component": str(
                    mirror_cfg.get("publish_component", "main")
                ),
            }
        ]

    return [
        {
            "aptly_name": f"{mirror_name}--{_safe_name(component)}",
            "source_component": component,
            "publish_component": component,
        }
        for component in components
    ]


def trim_snapshots(aptly_cfg_path, actual_mirror_name, keep_n, cmd_timeout):
    if keep_n < 1:
        return
    snapshots = run(
        aptly_cmd(aptly_cfg_path, "snapshot", "list", "-raw"),
        timeout=cmd_timeout,
    ).stdout.splitlines()
    snapshot_pattern = re.compile(
        rf"^{re.escape(actual_mirror_name)}-\d{{8}}-\d{{6}}$"
    )
    matching = sorted(
        snapshot for snapshot in snapshots if snapshot_pattern.fullmatch(snapshot)
    )
    for snapshot in matching[: max(0, len(matching) - keep_n)]:
        run(
            aptly_cmd(aptly_cfg_path, "snapshot", "drop", snapshot),
            check=False,
            timeout=cmd_timeout,
        )


def main():
    cfg_path = os.environ.get("CFG", "/work/config.yml")
    repo_root = os.environ.get("REPO_ROOT")
    if not repo_root:
        print("REPO_ROOT env missing", file=sys.stderr)
        return 2

    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    enabled_work = [
        (distro, mirror)
        for distro in cfg.get("apt", []) or []
        if distro.get("enabled", False)
        for mirror in distro.get("mirrors", []) or []
        if mirror.get("enabled", True)
    ]
    if not enabled_work:
        print("No enabled APT mirrors; nothing to do.")
        return 0

    apt_root = pathlib.Path(repo_root) / "apt"
    apt_root.mkdir(parents=True, exist_ok=True)
    legacy_state = apt_root / "state" / "aptly"
    if legacy_state.exists():
        print(
            f"! NOTICE: legacy shared APT state exists at {legacy_state}; "
            "new syncs use per-repository .state directories and do not modify that legacy database.",
            file=sys.stderr,
            flush=True,
        )

    global_cfg = cfg.get("global", {}) or {}
    keep_n = int(global_cfg.get("keep_snapshots", 2))
    fail_fast = bool(global_cfg.get("fail_fast", False))
    cmd_timeout = int(global_cfg.get("cmd_timeout_sec", 0)) or None

    gpg_key, gnupg_home = setup_gpg(repo_root, cfg)
    publish_gpg_flags = [f"-gpg-key={gpg_key}", "-batch"]
    passphrase_file = os.environ.get("OFFLINEREPO_SIGNING_PASSPHRASE_FILE", "")
    if passphrase_file:
        pass_path = pathlib.Path(passphrase_file)
        if not pass_path.is_file():
            shutil.rmtree(gnupg_home, ignore_errors=True)
            raise FileNotFoundError(f"GPG passphrase file not found: {pass_path}")
        publish_gpg_flags.append(f"-passphrase-file={passphrase_file}")

    failures = []

    try:
        for distro in cfg.get("apt", []) or []:
            if not distro.get("enabled", False):
                continue

            distro_name = str(must_get(distro, "name", "apt distro"))
            for mirror in distro.get("mirrors", []) or []:
                if not mirror.get("enabled", True):
                    continue

                mirror_name = str(
                    must_get(mirror, "mirror_name", f"apt[{distro_name}].mirrors[]")
                )
                url = str(must_get(mirror, "url", f"mirror {mirror_name}"))
                distribution = str(
                    must_get(mirror, "distribution", f"mirror {mirror_name}")
                )
                if distribution == "/":
                    distribution = "./"
                publish_distribution = str(
                    mirror.get(
                        "publish_distribution",
                        mirror_name if distribution == "./" else distribution,
                    )
                )
                archs = _norm_list(
                    mirror.get("architectures", global_cfg.get("architectures", []))
                )
                arch_flag = ",".join(archs)
                unit_root = apt_root / distro_name / mirror_name
                unit_root.mkdir(parents=True, exist_ok=True)
                aptly_cfg_path = write_aptly_config(unit_root, cfg)

                try:
                    keyrings = compute_keyrings(cfg, distro, mirror)
                    if not keyrings:
                        raise RuntimeError(
                            f"No upstream verification keyring configured for "
                            f"{distro_name}/{mirror_name}"
                        )
                    ensure_paths_exist(keyrings, f"mirror {mirror_name}")
                    keyring_args = keyring_flags(keyrings)

                    specs = mirror_specs(mirror_name, distribution, mirror)
                    snapshot_names = []
                    publish_components = []
                    actual_mirror_names = []
                    timestamp = time.strftime("%Y%m%d-%H%M%S")

                    for spec in specs:
                        actual_name = spec["aptly_name"]
                        actual_mirror_names.append(actual_name)
                        publish_components.append(spec["publish_component"])

                        show = run(
                            aptly_cmd(aptly_cfg_path, "mirror", "show", actual_name),
                            check=False,
                            timeout=cmd_timeout,
                        )
                        if show.returncode != 0:
                            create = aptly_cmd(
                                aptly_cfg_path,
                                "mirror",
                                "create",
                                *keyring_args,
                            )
                            if mirror.get("force_components", False):
                                create.append("-force-components")
                            if arch_flag:
                                create.append(f"-architectures={arch_flag}")
                            if mirror.get("with_sources", False):
                                create.append("-with-sources")
                            if mirror.get("with_udebs", False):
                                create.append("-with-udebs")
                            if mirror.get("with_installer", False):
                                create.append("-with-installer")
                            if mirror.get("filter"):
                                create.append(f"-filter={mirror['filter']}")
                                if mirror.get("filter_with_deps", False):
                                    create.append("-filter-with-deps")

                            create += [actual_name, url, distribution]
                            if spec["source_component"]:
                                create.append(spec["source_component"])
                            run(create, timeout=cmd_timeout)

                        run(
                            aptly_cmd(
                                aptly_cfg_path,
                                "mirror",
                                "update",
                                *keyring_args,
                                actual_name,
                            ),
                            retries=5,
                            retry_sleep=10,
                            timeout=cmd_timeout,
                        )

                        snapshot = f"{actual_name}-{timestamp}"
                        run(
                            aptly_cmd(
                                aptly_cfg_path,
                                "snapshot",
                                "create",
                                snapshot,
                                "from",
                                "mirror",
                                actual_name,
                            ),
                            timeout=cmd_timeout,
                        )
                        snapshot_names.append(snapshot)

                    # Each configured APT mirror/suite is its own publication,
                    # with its own aptly database and package pool under .state.
                    # Multiple selected components are signed together as one
                    # standard APT distribution for this unit.
                    prefix = "filesystem:unit:."
                    component_arg = "-component=" + ",".join(publish_components)
                    published = run(
                        aptly_cmd(
                            aptly_cfg_path,
                            "publish",
                            "show",
                            publish_distribution,
                            prefix,
                        ),
                        check=False,
                        timeout=cmd_timeout,
                    )

                    if published.returncode == 0:
                        run(
                            aptly_cmd(
                                aptly_cfg_path,
                                "publish",
                                "switch",
                                *publish_gpg_flags,
                                component_arg,
                                publish_distribution,
                                prefix,
                                *snapshot_names,
                            ),
                            timeout=cmd_timeout,
                        )
                    else:
                        run(
                            aptly_cmd(
                                aptly_cfg_path,
                                "publish",
                                "snapshot",
                                *publish_gpg_flags,
                                "-acquire-by-hash",
                                f"-distribution={publish_distribution}",
                                component_arg,
                                *snapshot_names,
                                prefix,
                            ),
                            timeout=cmd_timeout,
                        )

                    for actual_name in actual_mirror_names:
                        trim_snapshots(
                            aptly_cfg_path,
                            actual_name,
                            keep_n,
                            cmd_timeout,
                        )

                    copy_public_keys_to_unit(repo_root, unit_root)
                    record_unit(
                        repo_root,
                        family="apt",
                        profile=distro_name,
                        name=mirror_name,
                        relative_path=unit_root.relative_to(pathlib.Path(repo_root)).as_posix(),
                        metadata={
                            "source_url": url,
                            "distribution": publish_distribution,
                            "upstream_distribution": distribution,
                            "components": publish_components,
                            "architectures": archs,
                            "signing_fingerprint": gpg_key,
                            "signed_release": True,
                        },
                    )

                except Exception as exc:
                    failures.append((distro_name, mirror_name, str(exc)))
                    print(
                        f"! FAILED {distro_name}/{mirror_name}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if fail_fast:
                        break

            if fail_fast and failures:
                break
    finally:
        shutil.rmtree(gnupg_home, ignore_errors=True)

    if failures:
        print("\n=== FAILURES ===", file=sys.stderr)
        for distro_name, mirror_name, error in failures:
            print(f"- {distro_name}/{mirror_name}: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
