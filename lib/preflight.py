#!/usr/bin/env python3
import bz2
import concurrent.futures
import gzip
import hashlib
import json
import lzma
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

USER_AGENT = "OFFLINEREPO-preflight/1.0"
URL_RE = re.compile(r"^(?:https?|ftp|file)://\S+$", re.I)


def load_cfg() -> Dict[str, Any]:
    cfg_path = os.environ.get("CFG", "/work/config.yml")
    with open(cfg_path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def settings(cfg: Dict[str, Any]) -> Tuple[int, int, int]:
    global_cfg = cfg.get("global", {}) or {}
    concurrency = max(1, int(global_cfg.get("preflight_concurrency", 8)))
    timeout = max(1, int(global_cfg.get("preflight_timeout_sec", 15)))
    metadata_timeout = max(
        1, int(global_cfg.get("preflight_metadata_timeout_sec", 120))
    )
    return concurrency, timeout, metadata_timeout


def run(cmd: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _request(
    url: str,
    timeout: int,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
):
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    request = urllib.request.Request(url, headers=merged, method=method)
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_bytes(url: str, timeout: int) -> Tuple[bytes, str, int]:
    with _request(url, timeout, "GET") as response:
        data = response.read()
        return data, response.geturl(), int(getattr(response, "status", 200) or 200)


def probe_url(url: str, timeout: int) -> Tuple[bool, str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        path = pathlib.Path(urllib.request.url2pathname(parsed.path))
        if path.is_file():
            return True, f"file exists ({path.stat().st_size} bytes)"
        return False, "file does not exist"

    if parsed.scheme not in ("http", "https", "ftp"):
        return False, f"unsupported probe scheme: {parsed.scheme or '(none)'}"

    if parsed.scheme in ("http", "https"):
        try:
            with _request(url, timeout, "HEAD") as response:
                status = int(getattr(response, "status", 200) or 200)
                if 200 <= status < 400:
                    return True, f"HTTP {status}"
        except Exception:
            pass

    try:
        headers = (
            {"Range": "bytes=0-0"} if parsed.scheme in ("http", "https") else {}
        )
        with _request(url, timeout, "GET", headers=headers) as response:
            status = int(getattr(response, "status", 200) or 200)
            response.read(1)
            if 200 <= status < 400:
                return True, f"{parsed.scheme.upper()} {status}"
            return False, f"unexpected status {status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"URL error: {exc.reason}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def human_size(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} TiB"


def probe_payloads(
    family: str,
    repo_label: str,
    payloads: List[Dict[str, Any]],
    concurrency: int,
    timeout: int,
) -> Tuple[int, int]:
    if not payloads:
        print(
            f"[WARN] {family} {repo_label}: no package payloads were enumerated",
            flush=True,
        )
        return 0, 0

    total_bytes = sum(int(item.get("size") or 0) for item in payloads)
    print(
        f"[PLAN] {family} {repo_label}: {len(payloads)} package payload(s), "
        f"known size {human_size(total_bytes)}",
        flush=True,
    )

    def check(item: Dict[str, Any]):
        ok, detail = probe_url(str(item["url"]), timeout)
        return item, ok, detail

    ok_count = 0
    fail_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for item, ok, detail in pool.map(check, payloads):
            name = item.get("name") or pathlib.PurePosixPath(
                urllib.parse.urlparse(item["url"]).path
            ).name
            prefix = "OK" if ok else "FAIL"
            print(
                f"[{prefix}] {family} {repo_label} :: {name} :: "
                f"{item['url']} :: {detail}",
                flush=True,
            )
            if ok:
                ok_count += 1
            else:
                fail_count += 1

    print(
        f"[SUMMARY] {family} {repo_label}: reachable={ok_count} "
        f"failed={fail_count} total={len(payloads)}",
        flush=True,
    )
    return ok_count, fail_count


def parse_release_sha256(text: str) -> Dict[str, Tuple[str, int]]:
    entries: Dict[str, Tuple[str, int]] = {}
    in_sha256 = False
    for raw in text.splitlines():
        if raw == "SHA256:":
            in_sha256 = True
            continue
        if in_sha256:
            if raw.startswith(" "):
                parts = raw.split()
                if len(parts) >= 3:
                    try:
                        entries[parts[2]] = (parts[0].lower(), int(parts[1]))
                    except ValueError:
                        pass
                continue
            in_sha256 = False
    return entries


def cleartext_from_inrelease(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    marker = "-----BEGIN PGP SIGNED MESSAGE-----"
    signature = "-----BEGIN PGP SIGNATURE-----"
    if marker not in text or signature not in text:
        return text
    body = text.split("\n\n", 1)[1].split(signature, 1)[0]
    lines = []
    for line in body.splitlines():
        lines.append(line[2:] if line.startswith("- ") else line)
    return "\n".join(lines).rstrip() + "\n"


def decompress_index(path: str, data: bytes) -> bytes:
    if path.endswith(".xz"):
        return lzma.decompress(data)
    if path.endswith(".gz"):
        return gzip.decompress(data)
    if path.endswith(".bz2"):
        return bz2.decompress(data)
    return data


def parse_control_stanzas(data: bytes) -> Iterable[Dict[str, str]]:
    fields: Dict[str, str] = {}
    current: Optional[str] = None
    text = data.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line:
            if fields:
                yield fields
            fields = {}
            current = None
            continue
        if line[:1].isspace() and current:
            fields[current] = fields.get(current, "") + "\n" + line[1:]
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current = key
        fields[key] = value.lstrip()
    if fields:
        yield fields


def choose_index(
    release_entries: Dict[str, Tuple[str, int]], candidates: List[str]
) -> Optional[str]:
    for candidate in candidates:
        if candidate in release_entries:
            return candidate
    return None


def fetch_and_verify_index(
    release_base: str,
    release_entries: Dict[str, Tuple[str, int]],
    candidates: List[str],
    timeout: int,
) -> Tuple[str, bytes]:
    selected = choose_index(release_entries, candidates)
    if selected:
        url = urllib.parse.urljoin(release_base, selected)
        raw, final_url, _ = fetch_bytes(url, timeout)
        expected_hash, expected_size = release_entries[selected]
        actual_hash = hashlib.sha256(raw).hexdigest().lower()
        if len(raw) != expected_size:
            raise RuntimeError(
                f"metadata size mismatch for {selected}: expected "
                f"{expected_size}, got {len(raw)}"
            )
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"metadata SHA256 mismatch for {selected}: expected "
                f"{expected_hash}, got {actual_hash}"
            )
        print(f"[OK] APT metadata :: {selected} :: {final_url}", flush=True)
        return selected, decompress_index(selected, raw)

    # Some flat repositories expose package indexes without listing every
    # compression variant in Release. Probe standard index names as fallback.
    last_error: Optional[Exception] = None
    for candidate in candidates:
        url = urllib.parse.urljoin(release_base, candidate)
        try:
            raw, final_url, _ = fetch_bytes(url, timeout)
            print(f"[OK] APT metadata :: {candidate} :: {final_url}", flush=True)
            return candidate, decompress_index(candidate, raw)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        f"none of the expected package indexes were reachable: "
        f"{', '.join(candidates)}; last error: {last_error}"
    )


def apt_tool_check(
    cfg: Dict[str, Any],
    distro: Dict[str, Any],
    mirror: Dict[str, Any],
    metadata_timeout: int,
) -> None:
    import sync_apt

    mirror_name = str(mirror["mirror_name"])
    url = str(mirror["url"])
    distribution = str(mirror["distribution"])
    if distribution == "/":
        distribution = "./"
    global_cfg = cfg.get("global", {}) or {}
    archs = sync_apt._norm_list(
        mirror.get("architectures", global_cfg.get("architectures", []))
    )
    keyrings = sync_apt.compute_keyrings(cfg, distro, mirror)
    if not keyrings:
        raise RuntimeError(
            f"no upstream verification keyring configured for {mirror_name}"
        )
    sync_apt.ensure_paths_exist(keyrings, f"preflight {mirror_name}")

    with tempfile.TemporaryDirectory(prefix="offlinerepo-apt-preflight-") as temp_dir:
        temp = pathlib.Path(temp_dir)
        aptly_cfg = temp / "aptly.conf"
        aptly_cfg.write_text(
            json.dumps(
                {
                    "rootDir": str(temp / "state"),
                    "architectures": archs,
                    "gpgProvider": "gpg",
                    "skipLegacyPool": True,
                }
            ),
            encoding="utf-8",
        )

        specs = sync_apt.mirror_specs(mirror_name, distribution, mirror)
        for index, spec in enumerate(specs, 1):
            name = f"preflight-{index}-{sync_apt._safe_name(spec['aptly_name'])}"
            cmd = [
                "aptly",
                f"-config={aptly_cfg}",
                "mirror",
                "create",
                *sync_apt.keyring_flags(keyrings),
            ]
            if archs:
                cmd.append("-architectures=" + ",".join(archs))
            if mirror.get("with_sources", False):
                cmd.append("-with-sources")
            if mirror.get("with_udebs", False):
                cmd.append("-with-udebs")
            if mirror.get("with_installer", False):
                cmd.append("-with-installer")
            cmd += [name, url, distribution]
            if spec.get("source_component"):
                cmd.append(str(spec["source_component"]))
            cp = run(cmd, timeout=metadata_timeout)
            if cp.returncode != 0:
                detail = (
                    cp.stderr or cp.stdout or "aptly mirror create failed"
                ).strip()
                raise RuntimeError(detail[-2000:])
            print(
                f"[OK] APT tool check :: aptly accepted {mirror_name} "
                f"component={spec.get('source_component') or '(flat/all)'}",
                flush=True,
            )


def apt_release(
    cfg_mirror: Dict[str, Any], timeout: int
) -> Tuple[str, Dict[str, Tuple[str, int]]]:
    base_url = str(cfg_mirror["url"]).rstrip("/") + "/"
    distribution = str(cfg_mirror["distribution"])
    flat = distribution in ("./", "/")
    release_dir = (
        base_url
        if flat
        else urllib.parse.urljoin(base_url, f"dists/{distribution}/")
    )

    try:
        raw, final_url, _ = fetch_bytes(
            urllib.parse.urljoin(release_dir, "Release"), timeout
        )
        text = raw.decode("utf-8", errors="replace")
        print(f"[OK] APT Release :: {final_url}", flush=True)
    except Exception:
        raw, final_url, _ = fetch_bytes(
            urllib.parse.urljoin(release_dir, "InRelease"), timeout
        )
        text = cleartext_from_inrelease(raw)
        print(f"[OK] APT InRelease :: {final_url}", flush=True)
    return release_dir, parse_release_sha256(text)


def apt_binary_payloads(
    base_url: str,
    release_dir: str,
    entries: Dict[str, Tuple[str, int]],
    component: Optional[str],
    arch: str,
    timeout: int,
) -> List[Dict[str, Any]]:
    if component:
        stem = f"{component}/binary-{arch}/Packages"
    else:
        stem = "Packages"
    candidates = [stem + ext for ext in (".xz", ".gz", ".bz2", "")]
    _, index_data = fetch_and_verify_index(
        release_dir, entries, candidates, timeout
    )
    payloads = []
    for stanza in parse_control_stanzas(index_data):
        filename = stanza.get("Filename", "").strip()
        if not filename:
            continue
        package = stanza.get("Package", pathlib.PurePosixPath(filename).name)
        version = stanza.get("Version", "")
        package_arch = stanza.get("Architecture", arch)
        try:
            size = int(stanza.get("Size", "0") or 0)
        except ValueError:
            size = 0
        payloads.append(
            {
                "name": f"{package}_{version}_{package_arch}",
                "url": urllib.parse.urljoin(base_url, filename),
                "size": size,
            }
        )
    return payloads


def apt_source_payloads(
    base_url: str,
    release_dir: str,
    entries: Dict[str, Tuple[str, int]],
    component: str,
    timeout: int,
) -> List[Dict[str, Any]]:
    stem = f"{component}/source/Sources"
    candidates = [stem + ext for ext in (".xz", ".gz", ".bz2", "")]
    _, index_data = fetch_and_verify_index(
        release_dir, entries, candidates, timeout
    )
    payloads: List[Dict[str, Any]] = []
    for stanza in parse_control_stanzas(index_data):
        directory = stanza.get("Directory", "").strip().rstrip("/")
        source = stanza.get("Package") or stanza.get("Source") or "source"
        version = stanza.get("Version", "")
        checksums = stanza.get("Checksums-Sha256", "")
        for raw in checksums.splitlines():
            parts = raw.split()
            if len(parts) != 3:
                continue
            _, size_text, filename = parts
            try:
                size = int(size_text)
            except ValueError:
                size = 0
            relative = f"{directory}/{filename}" if directory else filename
            payloads.append(
                {
                    "name": f"{source}_{version}/{filename}",
                    "url": urllib.parse.urljoin(base_url, relative),
                    "size": size,
                }
            )
    return payloads


def apt_udeb_payloads(
    base_url: str,
    release_dir: str,
    entries: Dict[str, Tuple[str, int]],
    component: str,
    arch: str,
    timeout: int,
) -> List[Dict[str, Any]]:
    stem = f"{component}/debian-installer/binary-{arch}/Packages"
    candidates = [stem + ext for ext in (".xz", ".gz", ".bz2", "")]
    _, index_data = fetch_and_verify_index(
        release_dir, entries, candidates, timeout
    )
    payloads = []
    for stanza in parse_control_stanzas(index_data):
        filename = stanza.get("Filename", "").strip()
        if not filename:
            continue
        package = stanza.get("Package", pathlib.PurePosixPath(filename).name)
        version = stanza.get("Version", "")
        package_arch = stanza.get("Architecture", arch)
        try:
            size = int(stanza.get("Size", "0") or 0)
        except ValueError:
            size = 0
        payloads.append(
            {
                "name": f"{package}_{version}_{package_arch}.udeb",
                "url": urllib.parse.urljoin(base_url, filename),
                "size": size,
            }
        )
    return payloads


def preflight_apt(cfg: Dict[str, Any]) -> int:
    import sync_apt

    concurrency, timeout, metadata_timeout = settings(cfg)
    global_cfg = cfg.get("global", {}) or {}
    failures = 0
    for distro in cfg.get("apt", []) or []:
        if not distro.get("enabled", False):
            continue
        distro_name = str(distro.get("name", "apt"))
        for mirror in distro.get("mirrors", []) or []:
            if not mirror.get("enabled", True):
                continue
            mirror_name = str(mirror.get("mirror_name", "unnamed"))
            label = f"{distro_name}/{mirror_name}"
            print(f"\n=== APT PREFLIGHT {label} ===", flush=True)
            try:
                apt_tool_check(cfg, distro, mirror, metadata_timeout)
                release_dir, release_entries = apt_release(mirror, timeout)
                base_url = str(mirror["url"]).rstrip("/") + "/"
                distribution = str(mirror["distribution"])
                if distribution == "/":
                    distribution = "./"
                archs = sync_apt._norm_list(
                    mirror.get(
                        "architectures", global_cfg.get("architectures", [])
                    )
                ) or ["amd64"]
                specs = sync_apt.mirror_specs(
                    mirror_name, distribution, mirror
                )
                payloads: List[Dict[str, Any]] = []

                if distribution == "./":
                    payloads.extend(
                        apt_binary_payloads(
                            base_url,
                            release_dir,
                            release_entries,
                            None,
                            archs[0],
                            timeout,
                        )
                    )
                else:
                    for spec in specs:
                        component = spec.get("source_component")
                        if not component:
                            raise RuntimeError(
                                "cannot enumerate non-flat APT mirror without "
                                f"a source component: {mirror_name}"
                            )
                        for arch in archs:
                            payloads.extend(
                                apt_binary_payloads(
                                    base_url,
                                    release_dir,
                                    release_entries,
                                    str(component),
                                    arch,
                                    timeout,
                                )
                            )
                            if mirror.get("with_udebs", False):
                                payloads.extend(
                                    apt_udeb_payloads(
                                        base_url,
                                        release_dir,
                                        release_entries,
                                        str(component),
                                        arch,
                                        timeout,
                                    )
                                )
                        if mirror.get("with_sources", False):
                            payloads.extend(
                                apt_source_payloads(
                                    base_url,
                                    release_dir,
                                    release_entries,
                                    str(component),
                                    timeout,
                                )
                            )

                if mirror.get("with_installer", False):
                    print(
                        f"[WARN] APT {label}: aptly with_installer=true also "
                        "downloads non-package installer files; package preflight "
                        "does not enumerate those auxiliary files",
                        flush=True,
                    )

                unique: Dict[str, Dict[str, Any]] = {}
                for item in payloads:
                    unique.setdefault(str(item["url"]), item)
                _, failed = probe_payloads(
                    "APT", label, list(unique.values()), concurrency, timeout
                )
                failures += failed
            except Exception as exc:
                failures += 1
                print(
                    f"[FAIL] APT {label}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
    return 1 if failures else 0


def rpm_urls(
    distro: Dict[str, Any], repo: Dict[str, Any], metadata_timeout: int
) -> List[str]:
    dnf_bin = shutil.which("dnf5") or shutil.which("dnf")
    if not dnf_bin:
        raise RuntimeError("neither dnf5 nor dnf is available")

    releasever = str(distro.get("releasever", ""))
    arch = str(distro.get("arch", "x86_64"))
    repoid = str(repo["repoid"]).strip()
    baseurl = str(repo.get("baseurl", "")).strip()

    cmd = [dnf_bin, "-q", "--refresh"]
    if releasever:
        cmd += ["--releasever", releasever]
    if arch:
        cmd += ["--forcearch", arch]
    if baseurl:
        cmd += [f"--repofrompath={repoid},{baseurl}"]
    cmd += [f"--setopt={repoid}.skip_if_unavailable=false"]
    cmd += [
        "reposync",
        "--repoid",
        repoid,
        "--arch",
        arch,
        "--arch",
        "noarch",
        "--urls",
    ]

    cp = run(cmd, timeout=metadata_timeout)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout or "dnf reposync --urls failed").strip()
        raise RuntimeError(detail[-3000:])

    urls = []
    for line in (cp.stdout or "").splitlines():
        candidate = line.strip()
        if URL_RE.match(candidate):
            urls.append(candidate)
    if not urls:
        raise RuntimeError(
            "dnf reposync --urls succeeded but returned no package URLs; "
            "check repository content, architecture, and DNF output"
        )
    return list(dict.fromkeys(urls))


def preflight_rpm(cfg: Dict[str, Any]) -> int:
    concurrency, timeout, metadata_timeout = settings(cfg)
    only_name = os.environ.get("ONLY_RPM_NAME", "").strip().lower()
    failures = 0
    for distro in cfg.get("rpm", []) or []:
        if not distro.get("enabled", False):
            continue
        distro_name = str(distro.get("name", "rpm"))
        if only_name and distro_name.lower() != only_name:
            continue
        for repo in distro.get("repos", []) or []:
            if not repo.get("enabled", True):
                continue
            repoid = str(repo.get("repoid", "unnamed"))
            label = f"{distro_name}/{repoid}"
            print(f"\n=== RPM PREFLIGHT {label} ===", flush=True)
            try:
                key_url = str(repo.get("gpgkey_url", "")).strip()
                if key_url:
                    ok, detail = probe_url(key_url, timeout)
                    print(
                        f"[{'OK' if ok else 'FAIL'}] RPM {label} GPG key :: "
                        f"{key_url} :: {detail}",
                        flush=True,
                    )
                    if not ok:
                        failures += 1
                urls = rpm_urls(distro, repo, metadata_timeout)
                payloads = [
                    {
                        "name": pathlib.PurePosixPath(
                            urllib.parse.urlparse(url).path
                        ).name
                        or url,
                        "url": url,
                        "size": 0,
                    }
                    for url in urls
                ]
                _, failed = probe_payloads(
                    "RPM", label, payloads, concurrency, timeout
                )
                failures += failed
            except Exception as exc:
                failures += 1
                print(
                    f"[FAIL] RPM {label}: {type(exc).__name__}: {exc}",
                    flush=True,
                )
    return 1 if failures else 0


def preflight_apk(cfg: Dict[str, Any]) -> int:
    _, _, metadata_timeout = settings(cfg)
    failures = 0
    for distro in cfg.get("apk", []) or []:
        if not distro.get("enabled", False):
            continue
        distro_name = str(distro.get("name", "apk"))
        for mirror in distro.get("mirrors", []) or []:
            if not mirror.get("enabled", True):
                continue
            mirror_name = str(mirror.get("mirror_name", "unnamed"))
            rsync_url = str(mirror.get("rsync_url", "")).rstrip("/")
            architectures = (
                mirror.get("architectures", distro.get("architectures", []))
                or [None]
            )
            for arch in architectures:
                source = rsync_url + "/"
                if arch:
                    source = f"{rsync_url}/{str(arch).strip('/')}/"
                label = f"{distro_name}/{mirror_name}" + (
                    f"/{arch}" if arch else ""
                )
                print(f"\n=== APK PREFLIGHT {label} ===", flush=True)
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="offlinerepo-apk-preflight-"
                    ) as temp_dir:
                        cmd = [
                            "rsync",
                            "--recursive",
                            "--dry-run",
                            "--out-format=%n",
                            source,
                            temp_dir + "/",
                        ]
                        cp = run(cmd, timeout=metadata_timeout)
                    if cp.returncode != 0:
                        detail = (
                            cp.stderr or cp.stdout or "rsync dry-run failed"
                        ).strip()
                        raise RuntimeError(detail[-3000:])
                    packages = []
                    for raw in (cp.stdout or "").splitlines():
                        path = raw.strip()
                        if path.endswith(".apk"):
                            packages.append(path)
                    if not packages:
                        raise RuntimeError(
                            "rsync dry-run returned no .apk package files"
                        )
                    print(
                        f"[PLAN] APK {label}: {len(packages)} package payload(s)",
                        flush=True,
                    )
                    for path in packages:
                        package_url = source + path.lstrip("./")
                        print(
                            f"[OK] APK {label} :: "
                            f"{pathlib.PurePosixPath(path).name} :: "
                            f"{package_url} :: listed by rsync dry-run",
                            flush=True,
                        )
                    print(
                        f"[SUMMARY] APK {label}: reachable={len(packages)} "
                        f"failed=0 total={len(packages)}",
                        flush=True,
                    )
                except Exception as exc:
                    failures += 1
                    print(
                        f"[FAIL] APK {label}: {type(exc).__name__}: {exc}",
                        flush=True,
                    )
    return 1 if failures else 0


def main() -> int:
    cfg = load_cfg()
    family = os.environ.get("PREFLIGHT_FAMILY", "all").strip().lower()
    valid = {"all", "apt", "rpm", "apk"}
    if family not in valid:
        print(
            f"Unknown PREFLIGHT_FAMILY={family!r}; expected one of "
            f"{sorted(valid)}",
            file=sys.stderr,
        )
        return 2

    rc = 0
    if family in ("all", "apt"):
        rc |= preflight_apt(cfg)
    if family in ("all", "rpm"):
        rc |= preflight_rpm(cfg)
    if family in ("all", "apk"):
        rc |= preflight_apk(cfg)

    if rc:
        print(
            "\nPREFLIGHT RESULT: FAIL - one or more repositories/packages "
            "were unreachable.",
            flush=True,
        )
        return 1
    print(
        "\nPREFLIGHT RESULT: SUCCESS - every enumerated package payload "
        "was reachable.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
