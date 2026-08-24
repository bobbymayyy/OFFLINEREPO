#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
from typing import Any, Dict, Iterable

UNIT_FILE = ".offlinerepo-unit.json"
INDEX_FILE = ".offlinerepo-index.json"
FAMILIES = {"apt", "rpm", "apk"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _atomic_json(path: pathlib.Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _safe_relative(path: str) -> pathlib.PurePosixPath:
    rel = pathlib.PurePosixPath(path)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise ValueError(f"unsafe repository unit path: {path!r}")
    if rel.parts[0] not in FAMILIES:
        raise ValueError(f"repository unit path must begin with apt/, rpm/, or apk/: {path!r}")
    return rel


def _install_portable_offload(repo_root: pathlib.Path) -> None:
    source = pathlib.Path(__file__).resolve().with_name("offload-offlinerepo.py")
    if not source.is_file():
        return
    destination = repo_root / "offload-offlinerepo.py"
    shutil.copy2(source, destination)
    try:
        destination.chmod(0o755)
    except OSError:
        pass


def record_unit(
    repo_root: str | pathlib.Path,
    *,
    family: str,
    profile: str,
    name: str,
    relative_path: str,
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if family not in FAMILIES:
        raise ValueError(f"unsupported family: {family}")
    rel = _safe_relative(relative_path)
    root = pathlib.Path(repo_root)
    unit_root = root.joinpath(*rel.parts)
    unit_root.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    data: Dict[str, Any] = {
        "format": 1,
        "unit_id": f"{family}:{profile}:{name}",
        "family": family,
        "profile": profile,
        "name": name,
        "path": rel.as_posix(),
        "synced_at": now,
    }
    run_id = os.environ.get("OFFLINEREPO_RUN_ID", "").strip()
    if run_id:
        data["run_id"] = run_id
    if metadata:
        data["repository"] = metadata
    _atomic_json(unit_root / UNIT_FILE, data)
    _install_portable_offload(root)
    rebuild_index(root)
    return data


def iter_units(repo_root: str | pathlib.Path) -> Iterable[Dict[str, Any]]:
    root = pathlib.Path(repo_root)
    if not root.is_dir():
        return []
    found = []
    for family in sorted(FAMILIES):
        family_root = root / family
        if not family_root.is_dir():
            continue
        for manifest in family_root.rglob(UNIT_FILE):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                rel = manifest.parent.relative_to(root).as_posix()
                if data.get("path") != rel or data.get("family") != family:
                    continue
                found.append(data)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    return found


def rebuild_index(repo_root: str | pathlib.Path) -> Dict[str, Any]:
    root = pathlib.Path(repo_root)
    units = {str(item["unit_id"]): item for item in iter_units(root) if item.get("unit_id")}
    data = {
        "format": 1,
        "updated_at": utc_now(),
        "units": dict(sorted(units.items())),
    }
    root.mkdir(parents=True, exist_ok=True)
    _atomic_json(root / INDEX_FILE, data)
    return data


def show(repo_root: str | pathlib.Path) -> int:
    data = rebuild_index(repo_root)
    units = data.get("units", {})
    if not units:
        print("No synchronized repository units are present.")
        return 0
    print("UNIT\tSYNCED\tPATH\tDETAILS")
    for unit_id, item in units.items():
        meta = item.get("repository", {}) if isinstance(item.get("repository"), dict) else {}
        details = ""
        if item.get("family") == "apt":
            comps = ",".join(str(x) for x in meta.get("components", []) or [])
            details = f"suite={meta.get('distribution', '')} components={comps}".strip()
        elif item.get("family") == "rpm":
            details = f"repoid={meta.get('repoid', item.get('name', ''))}"
        elif item.get("family") == "apk":
            archs = ",".join(str(x) for x in meta.get("architectures", []) or [])
            details = f"architectures={archs}".strip()
        print(f"{unit_id}\t{item.get('synced_at', '')}\t{item.get('path', '')}\t{details}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Record and inspect OFFLINEREPO repository-unit state.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_show = sub.add_parser("show")
    p_show.add_argument("repo_root")
    p_index = sub.add_parser("index")
    p_index.add_argument("repo_root")
    p_has = sub.add_parser("has")
    p_has.add_argument("repo_root")
    p_has.add_argument("unit_id")
    args = parser.parse_args()
    if args.command == "show":
        return show(args.repo_root)
    if args.command == "has":
        return 0 if any(item.get("unit_id") == args.unit_id for item in iter_units(args.repo_root)) else 1
    rebuild_index(args.repo_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
