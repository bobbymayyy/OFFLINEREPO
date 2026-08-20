#!/usr/bin/env python3
"""Copy or move complete OFFLINEREPO repository units between storage roots."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import tempfile
from dataclasses import dataclass
from typing import Dict, Tuple

UNIT_FILE = ".offlinerepo-unit.json"
INDEX_FILE = ".offlinerepo-index.json"
FAMILIES = {"apt", "rpm", "apk"}
ROOT_FILES = {"serve-offlinerepo.py", "offload-offlinerepo.py"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


@dataclass(frozen=True)
class Unit:
    unit_id: str
    relative: pathlib.PurePosixPath
    root: pathlib.Path
    manifest: dict


def discover_units(source: pathlib.Path) -> list[Unit]:
    units: list[Unit] = []
    seen: set[str] = set()
    for family in sorted(FAMILIES):
        family_root = source / family
        if not family_root.is_dir():
            continue
        for manifest_path in family_root.rglob(UNIT_FILE):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            try:
                rel = pathlib.PurePosixPath(manifest_path.parent.relative_to(source).as_posix())
            except ValueError:
                continue
            unit_id = str(data.get("unit_id", "")).strip()
            declared = str(data.get("path", "")).strip()
            if not unit_id or declared != rel.as_posix() or data.get("family") != family:
                continue
            if unit_id in seen:
                raise RuntimeError(f"duplicate repository unit id: {unit_id}")
            seen.add(unit_id)
            units.append(Unit(unit_id, rel, manifest_path.parent, data))
    units.sort(key=lambda unit: unit.unit_id)
    return units


def file_priority(unit: Unit, rel: pathlib.PurePath) -> tuple[int, int, str]:
    """Copy payload/state first and client switching metadata last."""
    parts = rel.parts
    name = rel.name
    if name == UNIT_FILE:
        return (4, 0, str(rel))
    if unit.manifest.get("family") == "apt" and "dists" in parts:
        if name in {"Release", "Release.gpg", "InRelease"}:
            return (3, 0, str(rel))
        return (2, 0, str(rel))
    if unit.manifest.get("family") == "rpm" and "repodata" in parts:
        if name == "repomd.xml":
            return (3, 0, str(rel))
        return (2, 0, str(rel))
    if unit.manifest.get("family") == "apk" and not name.endswith(".apk"):
        return (3, 0, str(rel))
    return (1, 0, str(rel))


def iter_unit_files(unit: Unit) -> list[tuple[pathlib.PurePath, pathlib.Path]]:
    files: list[tuple[pathlib.PurePath, pathlib.Path]] = []
    for dirpath, dirnames, filenames in os.walk(unit.root, followlinks=False):
        current = pathlib.Path(dirpath)
        for dirname in list(dirnames):
            if (current / dirname).is_symlink():
                raise RuntimeError(f"symlinked directory is not supported in repository unit {unit.unit_id}: {current / dirname}")
        for filename in filenames:
            path = current / filename
            if path.is_symlink():
                raise RuntimeError(f"symlinked file is not supported in repository unit {unit.unit_id}: {path}")
            if path.is_file():
                files.append((pathlib.PurePath(path.relative_to(unit.root).as_posix()), path))
    files.sort(key=lambda item: file_priority(unit, item[0]))
    return files


def unchanged(src: pathlib.Path, dst: pathlib.Path) -> bool:
    if not dst.is_file():
        return False
    try:
        source_stat, destination_stat = src.stat(), dst.stat()
        return source_stat.st_size == destination_stat.st_size and source_stat.st_mtime_ns == destination_stat.st_mtime_ns
    except OSError:
        return False


def atomic_copy(src: pathlib.Path, dst: pathlib.Path, *, dry_run: bool, inode_map: Dict[Tuple[int, int], pathlib.Path]) -> int:
    stat = src.stat()
    inode_key = (stat.st_dev, stat.st_ino)
    if dry_run:
        return stat.st_size
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".offlinerepo-copy-", suffix=".part", dir=str(dst.parent))
    os.close(fd)
    temp = pathlib.Path(temp_name)
    try:
        linked = False
        first_dst = inode_map.get(inode_key) if stat.st_nlink > 1 else None
        if first_dst is not None and first_dst.is_file():
            try:
                temp.unlink(missing_ok=True)
                os.link(first_dst, temp)
                linked = True
            except OSError:
                linked = False
        if not linked:
            shutil.copy2(src, temp)
        os.replace(temp, dst)
        inode_map.setdefault(inode_key, dst)
    finally:
        temp.unlink(missing_ok=True)
    return stat.st_size


def source_paths(unit: Unit) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    dirs: set[str] = {"."}
    for dirpath, dirnames, filenames in os.walk(unit.root, followlinks=False):
        current = pathlib.Path(dirpath)
        rel_dir = current.relative_to(unit.root)
        dirs.add(rel_dir.as_posix() if rel_dir.parts else ".")
        for dirname in dirnames:
            dirs.add((rel_dir / dirname).as_posix())
        for filename in filenames:
            files.add((rel_dir / filename).as_posix())
    return files, dirs


def delete_stale(unit: Unit, destination_unit: pathlib.Path, dry_run: bool) -> tuple[int, int]:
    if not destination_unit.is_dir():
        return 0, 0
    src_files, src_dirs = source_paths(unit)
    deleted_files = deleted_dirs = 0
    for dirpath, dirnames, filenames in os.walk(destination_unit, topdown=False, followlinks=False):
        current = pathlib.Path(dirpath)
        rel_dir = current.relative_to(destination_unit)
        for filename in filenames:
            rel = (rel_dir / filename).as_posix()
            if rel not in src_files:
                deleted_files += 1
                print(f"{'WOULD DELETE' if dry_run else 'DELETE'} {unit.relative.as_posix()}/{rel}")
                if not dry_run:
                    (current / filename).unlink(missing_ok=True)
        for dirname in dirnames:
            rel = (rel_dir / dirname).as_posix()
            path = current / dirname
            if rel not in src_dirs:
                deleted_dirs += 1
                print(f"{'WOULD DELETE' if dry_run else 'DELETE'} {unit.relative.as_posix()}/{rel}/")
                if not dry_run:
                    if path.is_symlink():
                        path.unlink(missing_ok=True)
                    elif path.is_dir():
                        shutil.rmtree(path)
    return deleted_files, deleted_dirs


def verify_unit(unit: Unit, destination_unit: pathlib.Path) -> None:
    for rel, src in iter_unit_files(unit):
        dst = destination_unit.joinpath(*rel.parts)
        if not unchanged(src, dst):
            raise RuntimeError(f"verification failed after copy: {unit.unit_id} {rel}")


def sync_unit(unit: Unit, destination: pathlib.Path, dry_run: bool) -> tuple[int, int, int]:
    destination_unit = destination.joinpath(*unit.relative.parts)
    copied = skipped = copied_bytes = 0
    inode_map: Dict[Tuple[int, int], pathlib.Path] = {}
    for rel, src in iter_unit_files(unit):
        dst = destination_unit.joinpath(*rel.parts)
        stat = src.stat()
        inode_key = (stat.st_dev, stat.st_ino)
        if unchanged(src, dst):
            skipped += 1
            if stat.st_nlink > 1:
                inode_map.setdefault(inode_key, dst)
            continue
        copied_bytes += atomic_copy(src, dst, dry_run=dry_run, inode_map=inode_map)
        copied += 1
        print(f"{'WOULD COPY' if dry_run else 'COPY'} {unit.relative.as_posix()}/{rel}")
    delete_stale(unit, destination_unit, dry_run)
    if not dry_run:
        verify_unit(unit, destination_unit)
    return copied, skipped, copied_bytes


def copy_root_extras(source: pathlib.Path, destination: pathlib.Path, dry_run: bool) -> None:
    # Public keys are cumulative and tiny; never delete destination-only keys.
    src_keys = source / "keys"
    if src_keys.is_dir():
        for dirpath, _, filenames in os.walk(src_keys):
            current = pathlib.Path(dirpath)
            for filename in filenames:
                src = current / filename
                if not src.is_file() or src.is_symlink():
                    continue
                rel = src.relative_to(source)
                dst = destination / rel
                if unchanged(src, dst):
                    continue
                print(f"{'WOULD COPY' if dry_run else 'COPY'} {rel.as_posix()}")
                if not dry_run:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
    for name in sorted(ROOT_FILES):
        src = source / name
        if not src.is_file() or src.is_symlink():
            continue
        dst = destination / name
        if unchanged(src, dst):
            continue
        print(f"{'WOULD COPY' if dry_run else 'COPY'} {name}")
        if not dry_run:
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            try:
                dst.chmod(0o755)
            except OSError:
                pass


def rebuild_index(root: pathlib.Path, dry_run: bool = False) -> None:
    units = discover_units(root) if root.is_dir() else []
    data = {
        "format": 1,
        "updated_at": utc_now(),
        "units": {unit.unit_id: unit.manifest for unit in units},
    }
    if not dry_run:
        atomic_json(root / INDEX_FILE, data)


def prune_empty_family_dirs(source: pathlib.Path) -> None:
    for family in FAMILIES:
        root = source / family
        if not root.exists():
            continue
        for dirpath, _, _ in os.walk(root, topdown=False):
            path = pathlib.Path(dirpath)
            try:
                path.rmdir()
            except OSError:
                pass


def paths_overlap(a: pathlib.Path, b: pathlib.Path) -> bool:
    """Return True when paths are identical or either resolved path contains the other."""
    if a == b:
        return True
    try:
        a.relative_to(b)
        return True
    except ValueError:
        pass
    try:
        b.relative_to(a)
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy complete OFFLINEREPO repository units to another storage root without combining their package-manager state.")
    parser.add_argument("destination", help="destination storage/serving root")
    parser.add_argument(
        "--source",
        default=str(pathlib.Path(__file__).resolve().parent),
        help="source OFFLINEREPO root (default: directory containing this script)",
    )
    parser.add_argument("--move", action="store_true", help="remove each source unit only after it has copied and verified successfully")
    parser.add_argument(
        "--unit",
        action="append",
        default=[],
        help="transfer only this repository unit ID; may be repeated",
    )
    parser.add_argument(
        "--ignore-missing-units",
        action="store_true",
        help="do not fail if a requested --unit is not present in the source",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = pathlib.Path(args.source).expanduser().resolve()
    destination = pathlib.Path(args.destination).expanduser().resolve()
    if paths_overlap(source, destination):
        parser.error(
            "source and destination must be separate, non-nested storage roots; "
            f"got source={source} destination={destination}"
        )
    if not source.is_dir():
        parser.error(f"source does not exist: {source}")

    units = discover_units(source)
    if args.unit:
        requested = list(dict.fromkeys(args.unit))
        by_id = {unit.unit_id: unit for unit in units}
        missing = [unit_id for unit_id in requested if unit_id not in by_id]
        if missing and not args.ignore_missing_units:
            parser.error("requested repository unit(s) not found: " + ", ".join(missing))
        units = [by_id[unit_id] for unit_id in requested if unit_id in by_id]
        if missing:
            for unit_id in missing:
                print(f"SKIP MISSING {unit_id}")
    elif not units:
        parser.error(f"no repository units found under {source}; run offline-repoctl sync first")

    if not units:
        print("No matching repository units to transfer.")
        return 0

    total_copied = total_skipped = total_bytes = 0
    for unit in units:
        print(f"\n=== {'MOVE' if args.move else 'COPY'} {unit.unit_id} ===")
        copied, skipped, copied_bytes = sync_unit(unit, destination, args.dry_run)
        total_copied += copied
        total_skipped += skipped
        total_bytes += copied_bytes
        if args.move and not args.dry_run:
            shutil.rmtree(unit.root)
            print(f"REMOVED SOURCE {unit.relative.as_posix()}/")

    copy_root_extras(source, destination, args.dry_run)
    if not args.dry_run:
        rebuild_index(destination)
        if args.move:
            prune_empty_family_dirs(source)
            rebuild_index(source)

    verb = "would copy" if args.dry_run else "copied"
    print(f"\nOffload complete: {verb} {total_copied} changed file(s), skipped {total_skipped} unchanged file(s), {total_bytes} byte(s) transferred.")
    if args.move and not args.dry_run:
        print("Source repository units were removed only after successful copy verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
