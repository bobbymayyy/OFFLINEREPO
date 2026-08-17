#!/usr/bin/env python3
import argparse
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

DATA_PREFIX = "[CAPACITY-DATA] "
PLAN_RE = re.compile(
    r"^\[PLAN\] (?P<family>APT|RPM|APK) (?P<label>[^:]+): "
    r"(?P<count>\d+) package payload\(s\)(?:,.*)?$"
)
SUMMARY_RE = re.compile(
    r"^\[SUMMARY\] (?P<family>APT|RPM|APK) (?P<label>[^:]+): "
    r"reachable=(?P<reachable>\d+) failed=(?P<failed>\d+) total=(?P<total>\d+)$"
)


def load_cfg(path: Optional[str] = None) -> Dict[str, Any]:
    cfg_path = path or os.environ.get("CFG", "/work/config.yml")
    with open(cfg_path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def human_size(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if amount < 1024.0 or unit == "PiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{amount:.2f} PiB"


def emit_record(family: str, label: str, payloads: int, byte_count: int) -> None:
    record = {
        "family": family,
        "label": label,
        "payloads": int(payloads),
        "bytes": int(byte_count),
        "complete": True,
    }
    print(DATA_PREFIX + json.dumps(record, sort_keys=True), flush=True)


def run(cmd: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def collect_apt(cfg: Dict[str, Any]) -> int:
    import preflight
    import sync_apt

    _, timeout, metadata_timeout = preflight.settings(cfg)
    failures = 0
    global_cfg = cfg.get("global", {}) or {}

    for distro in cfg.get("apt", []) or []:
        if not distro.get("enabled", False):
            continue
        distro_name = str(distro.get("name", "apt"))
        for mirror in distro.get("mirrors", []) or []:
            if not mirror.get("enabled", True):
                continue
            mirror_name = str(mirror.get("mirror_name", "unnamed"))
            label = f"{distro_name}/{mirror_name}"
            try:
                quiet = io.StringIO()
                with contextlib.redirect_stdout(quiet):
                    release_dir, release_entries = preflight.apt_release(
                        mirror, metadata_timeout
                    )
                base_url = str(mirror["url"]).rstrip("/") + "/"
                distribution = str(mirror["distribution"])
                if distribution == "/":
                    distribution = "./"
                archs = sync_apt._norm_list(
                    mirror.get(
                        "architectures", global_cfg.get("architectures", [])
                    )
                ) or ["amd64"]
                specs = sync_apt.mirror_specs(mirror_name, distribution, mirror)
                payloads: List[Dict[str, Any]] = []

                if distribution == "./":
                    with contextlib.redirect_stdout(quiet):
                        flat_payloads = preflight.apt_binary_payloads(
                            base_url,
                            release_dir,
                            release_entries,
                            None,
                            archs[0],
                            timeout,
                        )
                    payloads.extend(flat_payloads)
                else:
                    for spec in specs:
                        component = spec.get("source_component")
                        if not component:
                            raise RuntimeError(
                                "cannot size non-flat APT mirror without a source component"
                            )
                        for arch in archs:
                            with contextlib.redirect_stdout(quiet):
                                binary_payloads = preflight.apt_binary_payloads(
                                    base_url,
                                    release_dir,
                                    release_entries,
                                    str(component),
                                    arch,
                                    timeout,
                                )
                            payloads.extend(binary_payloads)
                            if mirror.get("with_udebs", False):
                                with contextlib.redirect_stdout(quiet):
                                    udeb_payloads = preflight.apt_udeb_payloads(
                                        base_url,
                                        release_dir,
                                        release_entries,
                                        str(component),
                                        arch,
                                        timeout,
                                    )
                                payloads.extend(udeb_payloads)
                        if mirror.get("with_sources", False):
                            with contextlib.redirect_stdout(quiet):
                                source_payloads = preflight.apt_source_payloads(
                                    base_url,
                                    release_dir,
                                    release_entries,
                                    str(component),
                                    timeout,
                                )
                            payloads.extend(source_payloads)

                unique: Dict[str, Dict[str, Any]] = {}
                for item in payloads:
                    unique.setdefault(str(item["url"]), item)
                values = list(unique.values())
                total_bytes = sum(int(item.get("size") or 0) for item in values)
                emit_record("APT", label, len(values), total_bytes)
            except Exception as exc:
                failures += 1
                print(
                    f"[WARN] CAPACITY APT {label}: unable to determine exact payload size: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
    return 1 if failures else 0


def dnf_prefix(
    dnf_bin: str, distro: Dict[str, Any], repo: Dict[str, Any]
) -> Tuple[List[str], str, str]:
    releasever = str(distro.get("releasever", ""))
    arch = str(distro.get("arch", "x86_64"))
    repoid = str(repo["repoid"]).strip()
    baseurl = str(repo.get("baseurl", "")).strip()

    # Reachability preflight refreshed this repository immediately before
    # capacity collection, so reuse the same container/host metadata cache.
    cmd = [dnf_bin, "-q"]
    if releasever:
        cmd += ["--releasever", releasever]
    if arch:
        cmd += ["--forcearch", arch]
    if baseurl:
        cmd += [f"--repofrompath={repoid},{baseurl}"]
    cmd += [f"--setopt={repoid}.skip_if_unavailable=false"]
    return cmd, repoid, arch


def rpm_location_path(value: str) -> str:
    value = value.strip()
    parsed = urllib.parse.urlparse(value)
    path = parsed.path if parsed.scheme else value
    return urllib.parse.unquote(path).lstrip("/")


def collect_rpm(cfg: Dict[str, Any]) -> int:
    import preflight

    dnf_bin = shutil.which("dnf5") or shutil.which("dnf")
    if not dnf_bin:
        print(
            "[WARN] CAPACITY RPM: neither dnf5 nor dnf is available; size metadata unavailable",
            flush=True,
        )
        return 1

    only_name = os.environ.get("ONLY_RPM_NAME", "").strip().lower()
    metadata_timeout = max(
        1,
        int((cfg.get("global", {}) or {}).get("preflight_metadata_timeout_sec", 120)),
    )
    failures = 0
    is_dnf5 = pathlib.Path(dnf_bin).name == "dnf5"

    for distro in cfg.get("rpm", []) or []:
        if not distro.get("enabled", False):
            continue
        distro_name = str(distro.get("name", "rpm"))
        if only_name and distro_name.lower() != only_name:
            continue
        for repo in distro.get("repos", []) or []:
            if not repo.get("enabled", True):
                continue
            base_cmd, repoid, arch = dnf_prefix(dnf_bin, distro, repo)
            label = f"{distro_name}/{repoid}"

            # reposync is the source of truth for what OFFLINEREPO will mirror.
            # Query location + downloadsize metadata separately, then attach a
            # byte count only to the exact URLs reposync says it would download.
            try:
                reposync_urls = preflight.rpm_urls(distro, repo, metadata_timeout)
            except Exception as exc:
                failures += 1
                print(
                    f"[WARN] CAPACITY RPM {label}: unable to reproduce reposync payload set: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                continue

            query = list(base_cmd)
            query_format = "%{location}|%{downloadsize}\\n"
            if is_dnf5:
                query += [
                    f"--repo={repoid}",
                    "repoquery",
                    f"--arch={arch},noarch",
                    f"--queryformat={query_format}",
                ]
            else:
                query += [
                    "repoquery",
                    "--disable-modular-filtering",
                    "--repo",
                    repoid,
                    "--arch",
                    f"{arch},noarch",
                    "--queryformat",
                    query_format,
                ]
            cp = run(query, timeout=metadata_timeout)
            if cp.returncode != 0:
                failures += 1
                detail = (cp.stderr or cp.stdout or "dnf repoquery failed").strip()
                print(
                    f"[WARN] CAPACITY RPM {label}: DNF could not report location/downloadsize: "
                    f"{detail[-2000:]}",
                    flush=True,
                )
                continue

            by_basename: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
            bad_lines = 0
            metadata_rows = 0
            for raw in (cp.stdout or "").splitlines():
                value = raw.strip()
                if not value:
                    continue
                if "|" not in value:
                    bad_lines += 1
                    continue
                location_text, size_text = value.rsplit("|", 1)
                location = rpm_location_path(location_text)
                if not location:
                    bad_lines += 1
                    continue
                try:
                    byte_count = int(size_text.strip())
                except ValueError:
                    bad_lines += 1
                    continue
                basename = pathlib.PurePosixPath(location).name
                by_basename[basename].append((location, byte_count))
                metadata_rows += 1

            if not metadata_rows or bad_lines:
                failures += 1
                print(
                    f"[WARN] CAPACITY RPM {label}: DNF location/size output was incomplete "
                    f"(rows={metadata_rows}, unparsable={bad_lines})",
                    flush=True,
                )
                continue

            matched_sizes: List[int] = []
            unmatched: List[str] = []
            ambiguous: List[str] = []
            for url in reposync_urls:
                url_path = rpm_location_path(url)
                basename = pathlib.PurePosixPath(url_path).name
                candidates = [
                    (location, byte_count)
                    for location, byte_count in by_basename.get(basename, [])
                    if url_path.endswith(location)
                ]
                if len(candidates) == 1:
                    matched_sizes.append(candidates[0][1])
                elif not candidates:
                    unmatched.append(url)
                else:
                    # Duplicate metadata entries for an identical location are
                    # harmless only when they agree on the payload byte count.
                    sizes = {byte_count for _, byte_count in candidates}
                    if len(sizes) == 1:
                        matched_sizes.append(next(iter(sizes)))
                    else:
                        ambiguous.append(url)

            if unmatched or ambiguous or len(matched_sizes) != len(reposync_urls):
                failures += 1
                print(
                    f"[WARN] CAPACITY RPM {label}: could not map exact DNF size metadata "
                    f"to the reposync payload set (reposync={len(reposync_urls)}, "
                    f"matched={len(matched_sizes)}, unmatched={len(unmatched)}, "
                    f"ambiguous={len(ambiguous)}, metadata_rows={metadata_rows})",
                    flush=True,
                )
                for url in unmatched[:5]:
                    print(f"[WARN] CAPACITY RPM {label}: unmatched payload :: {url}", flush=True)
                for url in ambiguous[:5]:
                    print(f"[WARN] CAPACITY RPM {label}: ambiguous payload :: {url}", flush=True)
                continue

            emit_record("RPM", label, len(reposync_urls), sum(matched_sizes))
    return 1 if failures else 0


def collect_apk(cfg: Dict[str, Any]) -> int:
    metadata_timeout = max(
        1,
        int((cfg.get("global", {}) or {}).get("preflight_metadata_timeout_sec", 120)),
    )
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
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="offlinerepo-apk-size-"
                    ) as temp_dir:
                        cmd = [
                            "rsync",
                            "--recursive",
                            "--dry-run",
                            "--out-format=%l %n",
                            source,
                            temp_dir + "/",
                        ]
                        cp = run(cmd, timeout=metadata_timeout)
                    if cp.returncode != 0:
                        raise RuntimeError(
                            (cp.stderr or cp.stdout or "rsync dry-run failed").strip()[-2000:]
                        )
                    count = 0
                    total_bytes = 0
                    for raw in (cp.stdout or "").splitlines():
                        parts = raw.split(maxsplit=1)
                        if len(parts) != 2:
                            continue
                        size_text, path = parts
                        if not path.strip().endswith(".apk"):
                            continue
                        try:
                            total_bytes += int(size_text.strip())
                        except ValueError:
                            raise RuntimeError(f"invalid rsync file size: {size_text!r}")
                        count += 1
                    if not count:
                        raise RuntimeError("rsync size listing returned no .apk files")
                    emit_record("APK", label, count, total_bytes)
                except Exception as exc:
                    failures += 1
                    print(
                        f"[WARN] CAPACITY APK {label}: unable to determine package sizes: "
                        f"{type(exc).__name__}: {exc}",
                        flush=True,
                    )
    return 1 if failures else 0


def parse_records(lines: Iterable[str]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    records: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for raw in lines:
        if not raw.startswith(DATA_PREFIX):
            continue
        try:
            record = json.loads(raw[len(DATA_PREFIX) :])
            key = (str(record["family"]), str(record["label"]))
            records[key] = record
        except (ValueError, KeyError, TypeError):
            continue
    return records


def rewrite_plans(
    lines: List[str], records: Dict[Tuple[str, str], Dict[str, Any]]
) -> List[str]:
    rewritten: List[str] = []
    for raw in lines:
        if raw.startswith(DATA_PREFIX):
            continue
        match = PLAN_RE.match(raw)
        if match:
            key = (match.group("family"), match.group("label"))
            record = records.get(key)
            expected_count = int(match.group("count"))
            if (
                record
                and record.get("complete", False)
                and int(record.get("payloads", -1)) == expected_count
            ):
                byte_count = int(record.get("bytes", 0))
                raw = (
                    f"[PLAN] {key[0]} {key[1]}: {expected_count} package payload(s), "
                    f"known size {human_size(byte_count)} ({byte_count} bytes)"
                )
        rewritten.append(raw)
    return rewritten


def summarize(args: argparse.Namespace) -> int:
    cfg = load_cfg(args.config)
    log_path = pathlib.Path(args.log)
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    records = parse_records(lines)
    clean_lines = rewrite_plans(lines, records)
    log_path.write_text("\n".join(clean_lines).rstrip() + "\n", encoding="utf-8")

    summaries: Dict[Tuple[str, str], Dict[str, int]] = {}
    for raw in clean_lines:
        match = SUMMARY_RE.match(raw)
        if not match:
            continue
        key = (match.group("family"), match.group("label"))
        summaries[key] = {
            "reachable": int(match.group("reachable")),
            "failed": int(match.group("failed")),
            "total": int(match.group("total")),
        }

    warning_lines = [line for line in clean_lines if line.startswith("[WARN]")]
    total_payloads = sum(item["total"] for item in summaries.values())
    total_failed = sum(item["failed"] for item in summaries.values())
    known_bytes = 0
    unknown_repos: List[str] = []
    by_family: Dict[str, Dict[str, List[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0])
    )

    for key, summary in summaries.items():
        family, label = key
        distro = label.split("/", 1)[0]
        record = records.get(key)
        by_family[family][distro][0] += summary["total"]
        if (
            record
            and record.get("complete", False)
            and int(record.get("payloads", -1)) == summary["total"]
        ):
            byte_count = int(record.get("bytes", 0))
            known_bytes += byte_count
            by_family[family][distro][1] += byte_count
        else:
            unknown_repos.append(f"{family} {label}")

    apt_bytes = sum(v[1] for v in by_family.get("APT", {}).values())
    link_method = str(
        (cfg.get("global", {}) or {}).get("aptly_publish_link_method", "hardlink")
    ).strip().lower()
    publish_duplication = apt_bytes if link_method == "copy" else 0
    estimated_footprint = known_bytes + publish_duplication

    repo_root = pathlib.Path(os.path.abspath(os.path.expanduser(args.repo_root)))
    repo_root.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(repo_root)
    free_bytes = int(disk.free)

    print("\n=== PREFLIGHT GRAND SUMMARY ===")
    print(f"Repositories checked: {len(summaries)}")
    print(f"Warnings/skips recorded: {len(warning_lines)}")
    print(f"Package payloads checked: {total_payloads}")
    print(f"Package reachability failures: {total_failed}")

    for family in ("APT", "RPM", "APK"):
        groups = by_family.get(family, {})
        if not groups:
            continue
        print(f"\n{family}")
        for distro, (count, byte_count) in groups.items():
            print(
                f"  {distro:<16} {count:>8} payload(s)  {human_size(byte_count):>12}"
            )

    print("\nCAPACITY")
    print(f"  Known package payload:      {human_size(known_bytes)}")
    if link_method == "copy" and apt_bytes:
        print(
            f"  Aptly copy-mode duplicate:  {human_size(publish_duplication)}"
        )
    print(f"  Estimated package footprint:{human_size(estimated_footprint):>13}")
    print(f"  Repository root:            {repo_root}")
    print(f"  Filesystem free now:        {human_size(free_bytes)}")

    if unknown_repos:
        print(
            f"[WARN] CAPACITY: exact size metadata is missing for {len(unknown_repos)} "
            "checked repository/repositories; the comparison below is a known-size lower bound."
        )
        for label in unknown_repos:
            print(f"  size unknown: {label}")

    delta = free_bytes - estimated_footprint
    if delta < 0:
        print(
            f"[WARN] CAPACITY: estimated known package footprint already exceeds "
            f"current free space by {human_size(-delta)}."
        )
    elif unknown_repos:
        print(
            f"[WARN] CAPACITY: the known-size lower bound fits with {human_size(delta)} "
            "remaining, but overall fit cannot be confirmed until every checked "
            "repository has exact size metadata."
        )
    else:
        print(
            f"[CAPACITY] PASS: current free space exceeds the estimated package "
            f"footprint by {human_size(delta)}."
        )

    print(
        "[CAPACITY] NOTE: comparison is payload-focused. Repository metadata, "
        "filesystem overhead, temporary sync space, and already-present reusable "
        "packages are not modeled exactly."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OFFLINEREPO preflight capacity helper")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect")
    collect.add_argument("family", choices=("apt", "rpm", "apk"))

    summary = sub.add_parser("summarize")
    summary.add_argument("--config", required=True)
    summary.add_argument("--log", required=True)
    summary.add_argument("--repo-root", required=True)

    args = parser.parse_args()
    if args.command == "collect":
        cfg = load_cfg()
        if args.family == "apt":
            return collect_apt(cfg)
        if args.family == "rpm":
            return collect_rpm(cfg)
        return collect_apk(cfg)
    return summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())
