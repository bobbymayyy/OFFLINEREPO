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

SERVER_SRC="$HERE/serve-offlinerepo.py"
OFFLOAD_SRC="$HERE/lib/offload-offlinerepo.py"
[[ -f "$SERVER_SRC" ]] || { echo "Missing portable helper: $SERVER_SRC" >&2; exit 1; }
[[ -f "$OFFLOAD_SRC" ]] || { echo "Missing portable helper: $OFFLOAD_SRC" >&2; exit 1; }

install_helper() {
  local source="$1" destination="$2"
  if [[ -e "$destination" && "$source" -ef "$destination" ]]; then
    printf 'Portable helper already in place: %s\n' "$destination"
  else
    cp "$source" "$destination"
  fi
  chmod 0755 "$destination" 2>/dev/null || true
}

install_helper "$SERVER_SRC" "$TARGET/serve-offlinerepo.py"
install_helper "$OFFLOAD_SRC" "$TARGET/offload-offlinerepo.py"

printf 'Portable server installed: %s\n' "$TARGET/serve-offlinerepo.py"
printf 'Portable offload helper installed: %s\n' "$TARGET/offload-offlinerepo.py"
printf 'Serve directly: python3 %q --bind 0.0.0.0 --port 8080\n' "$TARGET/serve-offlinerepo.py"
printf 'Copy units off USB: python3 %q /srv/OFFLINEREPO\n' "$TARGET/offload-offlinerepo.py"
printf 'Move units off USB: python3 %q --move /srv/OFFLINEREPO\n' "$TARGET/offload-offlinerepo.py"
