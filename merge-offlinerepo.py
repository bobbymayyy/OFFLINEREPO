#!/usr/bin/env python3
"""Non-destructively merge one portable OFFLINEREPO batch into a cumulative tree."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import tempfile
from typing import Iterable

STATE_FILE = ".offlinerepo-state.json"
ALLOWED_TOP_LEVEL = {"apt", "rpm", "apk", "keys"}
HELPERS = {"serve-offlinerepo.py", "merge-offlinerepo.py"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def should_skip(rel: pathlib.PurePath) -> bool:
    parts = rel.parts
    if not parts:
        return True
    if parts[0] == "apt" and len(parts) > 1 and parts[1] == "state":
        return True
    if "logs" in parts or ".rsync-partial" in parts:
        return True
    if any(part.startswith(".") for part in parts):
        return True
    return False


def allowed(rel: pathlib.PurePath) -> bool:
    if not rel.parts:
        return False
    return rel.parts[0] in ALLOWED_TOP_LEVEL or (len(rel.parts) == 1 and rel.name in HELPERS)


def metadata_priority(rel: pathlib.PurePath) -> tuple[int, int, str]:
    """Payloads first, repository metadata later, root switching metadata last."""
    parts = rel.parts
    name = rel.name
    if parts and parts[0] == "apt" and "dists" in parts:
        last = name in {"Release", "Release.gpg", "InRelease"}
        return (2 if last else 1, 1 if last else 0, str(rel))
    if parts and parts[0] == "rpm" and "repodata" in parts:
        last = name == "repomd.xml"
        return (2 if last else 1, 1 if last else 0, str(rel))
    if parts and parts[0] == "apk" and not name.endswith(".apk"):
        return (2, 0, str(rel))
    return (0, 0, str(rel))


def iter_files(source: pathlib.Path) -> Iterable[tuple[pathlib.PurePath, pathlib.Path]]:
    items: list[tuple[pathlib.PurePath, pathlib.Path]] = []
    for top in sorted(ALLOWED_TOP_LEVEL):
        root = source / top
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = pathlib.Path(dirpath)
            rel_dir = current.relative_to(source)
            dirnames[:] = [
                d for d in dirnames
                if not should_skip(rel_dir / d) and not (current / d).is_symlink()
            ]
            for filename in filenames:
                path = current / filename
                rel = path.relative_to(source)
                if should_skip(rel) or path.is_symlink() or not allowed(rel):
                    continue
                if path.is_file():
                    items.append((rel, path))
    for helper in sorted(HELPERS):
        path = source / helper
        if path.is_file() and not path.is_symlink():
            items.append((pathlib.PurePath(helper), path))
    items.sort(key=lambda item: metadata_priority(item[0]))
    return items


def unchanged(src: pathlib.Path, dst: pathlib.Path) -> bool:
    if not dst.is_file():
        return False
    try:
        a = src.stat()
        b = dst.stat()
        return a.st_size == b.st_size and a.st_mtime_ns == b.st_mtime_ns
    except OSError:
        return False


def atomic_copy(src: pathlib.Path, dst: pathlib.Path, dry_run: bool) -> int:
    size = src.stat().st_size
    if dry_run:
        return size
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".offlinerepo-merge-", suffix=".part", dir=str(dst.parent))
    os.close(fd)
    temp = pathlib.Path(temp_name)
    try:
        shutil.copy2(src, temp)
        os.replace(temp, dst)
    finally:
        temp.unlink(missing_ok=True)
    return size


def load_state(path: pathlib.Path) -> dict:
    if not path.is_file():
        return {"format": 1, "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"format": 1, "profiles": {}}
    except Exception:
        return {"format": 1, "profiles": {}}


def merge_state(source: pathlib.Path, destination: pathlib.Path, dry_run: bool) -> None:
    src_state = load_state(source / STATE_FILE)
    dst_state = load_state(destination / STATE_FILE)
    src_profiles = src_state.get("profiles", {}) if isinstance(src_state.get("profiles", {}), dict) else {}
    dst_profiles = dst_state.get("profiles", {}) if isinstance(dst_state.get("profiles", {}), dict) else {}
    merged = dict(dst_profiles)
    merged.update(src_profiles)
    now = utc_now()
    imports = dst_state.get("imports", [])
    if not isinstance(imports, list):
        imports = []
    imports = imports[-127:] + [{
        "imported_at": now,
        "source_updated_at": src_state.get("updated_at"),
        "profiles": src_state.get("last_batch", sorted(src_profiles)),
    }]
    result = {
        "format": 1,
        "updated_at": now,
        "profiles": dict(sorted(merged.items())),
        "last_batch": src_state.get("last_batch", sorted(src_profiles)),
        "imports": imports,
    }
    if dry_run:
        return
    destination.mkdir(parents=True, exist_ok=True)
    state_path = destination / STATE_FILE
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, state_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge a portable OFFLINEREPO batch into a cumulative repository without deleting other profiles."
    )
    parser.add_argument("destination", help="cumulative disconnected repository root")
    parser.add_argument(
        "--source",
        default=str(pathlib.Path(__file__).resolve().parent),
        help="portable batch root (default: directory containing this script)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = pathlib.Path(args.source).expanduser().resolve()
    destination = pathlib.Path(args.destination).expanduser().resolve()
    if source == destination:
        parser.error("source and destination must be different")
    if not source.is_dir():
        parser.error(f"source does not exist: {source}")
    if not any((source / name).is_dir() for name in ("apt", "rpm", "apk")):
        parser.error(f"source does not look like an OFFLINEREPO tree: {source}")

    copied = skipped = copied_bytes = 0
    for rel, src in iter_files(source):
        dst = destination / rel
        if unchanged(src, dst):
            skipped += 1
            continue
        copied_bytes += atomic_copy(src, dst, args.dry_run)
        copied += 1
        print(f"{'WOULD COPY' if args.dry_run else 'COPY'} {rel}")

    merge_state(source, destination, args.dry_run)
    verb = "would copy" if args.dry_run else "copied"
    print(f"Merge complete: {verb} {copied} file(s), skipped {skipped} unchanged file(s), payload {copied_bytes} bytes.")
    print("No destination-only files were deleted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
