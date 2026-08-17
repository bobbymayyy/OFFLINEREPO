#!/usr/bin/env python3
"""Serve an OFFLINEREPO tree with only Python's standard library."""

import argparse
import functools
import pathlib
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


# Keep the legacy shared APT state path hidden too. Current repository-unit
# state uses dot-prefixed .state/ directories and is blocked generically below.
PRIVATE_PREFIXES = (
    ("apt", "state"),
    ("logs",),
)


class RepoHandler(SimpleHTTPRequestHandler):
    """Static handler that hides OFFLINEREPO operational state."""

    def _request_parts(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        return tuple(part for part in pathlib.PurePosixPath(path).parts if part != "/")

    def _is_private(self):
        parts = self._request_parts()
        if any(part.startswith(".") for part in parts):
            return True
        return any(parts[: len(prefix)] == prefix for prefix in PRIVATE_PREFIXES)

    def send_head(self):
        if self._is_private():
            self.send_error(404, "Not found")
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404, "Directory listing disabled")
        return None


def parse_args():
    default_root = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Serve an OFFLINEREPO package tree over static HTTP."
    )
    parser.add_argument(
        "--root",
        default=str(default_root),
        help="repository root (default: directory containing this script)",
    )
    parser.add_argument("--bind", default="0.0.0.0", help="address to bind")
    parser.add_argument("--port", type=int, default=8080, help="TCP port")
    return parser.parse_args()


def main():
    args = parse_args()
    root = pathlib.Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Repository root does not exist or is not a directory: {root}", file=sys.stderr)
        return 2

    known = [name for name in ("apt", "rpm", "apk", "keys") if (root / name).exists()]
    if not known:
        print(
            f"Warning: {root} does not currently contain apt/, rpm/, apk/, or keys/.",
            file=sys.stderr,
        )

    handler = functools.partial(RepoHandler, directory=str(root))
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    display_host = args.bind if args.bind not in ("0.0.0.0", "::") else "HOST"
    print(f"Serving OFFLINEREPO root: {root}")
    print(f"Client base URL: http://{display_host}:{server.server_port}")
    print("Hidden from HTTP: .state/, state manifests, legacy apt/state/, logs/, and other dot-prefixed paths")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
