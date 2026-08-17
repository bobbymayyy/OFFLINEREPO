#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-${REPO_ROOT:-}}"

if [[ -z "$TARGET" ]]; then
  echo "Usage: $0 /path/to/repo_root" >&2
  echo "       REPO_ROOT=/path/to/repo_root $0" >&2
  exit 2
fi

TARGET="$(python3 - "$TARGET" <<'PY'
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
)"

[[ -d "$TARGET" ]] || {
  echo "Repository root does not exist: $TARGET" >&2
  exit 1
}

if [[ ! -d "$TARGET/apt" && ! -d "$TARGET/rpm" && ! -d "$TARGET/apk" ]]; then
  echo "Refusing to install helpers: no apt/, rpm/, or apk/ directory under $TARGET" >&2
  exit 1
fi

for helper in serve-offlinerepo.py merge-offlinerepo.py; do
  [[ -f "$HERE/$helper" ]] || {
    echo "Missing portable helper: $HERE/$helper" >&2
    exit 1
  }
  cp "$HERE/$helper" "$TARGET/$helper"
  chmod 0755 "$TARGET/$helper" 2>/dev/null || true
done

printf 'Portable server installed: %s\n' "$TARGET/serve-offlinerepo.py"
printf 'Portable merge helper installed: %s\n' "$TARGET/merge-offlinerepo.py"
printf 'Serve on the disconnected host: python3 %q --bind 0.0.0.0 --port 8080\n' "$TARGET/serve-offlinerepo.py"
printf 'Merge this batch into a cumulative tree: python3 %q /srv/OFFLINEREPO\n' "$TARGET/merge-offlinerepo.py"
