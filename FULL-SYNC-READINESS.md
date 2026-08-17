# Full-sync readiness and portability

This note captures the contract OFFLINEREPO should preserve before a large mirror run: refresh repository metadata, but do not re-download unchanged package payloads; keep synchronization state with the repository tree; and make the resulting tree usable from removable storage, a disconnected host, or the original connected host.

## Incremental payload contract

Repository metadata must be refreshed on every synchronization so OFFLINEREPO can discover new, removed, or replaced content. The optimization applies to package payloads, not to metadata discovery.

| Family | Incremental behavior | Interrupted transfer behavior | Removal behavior |
|---|---|---|---|
| APT / aptly | `aptly mirror update` reuses package files already present in aptly's package pool. The pool is deduplicated across mirrors. | Aptly mirror updates can be restarted safely. OFFLINEREPO intentionally does **not** use aptly's `-skip-existing-packages` flag because that flag trusts the mirror database without checking whether the referenced package file still exists. The default behavior is safer for a portable drive because a missing/corruptly removed local payload can be fetched again. | New snapshots are published and older snapshots are trimmed according to `keep_snapshots`. |
| RPM / DNF4 / DNF5 | `reposync` does not download RPM payloads already present in the destination. OFFLINEREPO also requests `--remote-time` so copied trees retain useful upstream timestamps. | DNF handles its own package downloads; rerunning reposync reuses completed payloads already present. | `--delete` removes package files no longer present upstream. |
| Alpine / rsync | Rsync's default quick check skips file data when destination size and modification time already match the source. | `--partial-dir=.rsync-partial` keeps incomplete data outside the served package namespace and reuses it on the next run. | `--delete-delay` applies upstream removals late in the transfer. |

Do not replace these behaviors with a blanket `--ignore-existing` policy. A repository index, metadata file, or rare same-path upstream correction must still be allowed to change.

## What must travel with the USB drive

Treat the configured `paths.repo_root` as one portable unit.

```text
repo_root/
├── apt/
│   ├── state/        # aptly database + deduplicated package pool; needed for future incremental APT sync
│   └── ...           # client-facing published APT trees
├── rpm/              # client-facing DNF/YUM trees
├── apk/              # client-facing Alpine trees
├── keys/             # public keys only
└── serve-offlinerepo.py   # optional portable HTTP helper
```

For a **serve-only** disconnected host, only the client-facing trees and public keys are required. Keeping `apt/state/` on the drive is strongly recommended anyway because it preserves the ability to return the drive to a connected sync host and continue incrementally instead of rebuilding APT state from scratch.

The OFFLINEREPO APT private signing key must remain separate from the portable repository. Serving an already-published tree does not require the private key. A future connected APT synchronization does.

## Filesystem choice

APT's default publish mode is `hardlink`, which is the most space-efficient arrangement because published packages can share storage with aptly's package pool.

Use a Linux filesystem with hardlink support, such as ext4 or XFS, when practical. `offline-repoctl` checks hardlink support before APT synchronization.

For a filesystem that cannot create hardlinks, configure:

```yaml
global:
  aptly_publish_link_method: copy
```

`copy` trades additional APT storage for broader filesystem compatibility. The repository remains path-portable in either mode: moving or remounting the complete tree at a different absolute path does not change client-facing repository metadata.

When copying a hardlink-mode tree to another Linux filesystem, prefer a tool that preserves hardlinks, for example `rsync -aH` or `cp -a`. Losing the hardlink relationship does not normally break the published repository, but it can consume substantially more space.

## Serving modes

OFFLINEREPO is a proactive mirror, not an on-demand caching proxy. It can still fill the same practical LAN role as apt-cacher-ng after synchronization: clients point at one local HTTP endpoint and receive package content without reaching the Internet.

### 1. Serve from the connected sync host

```bash
./serve-offlinerepo.py \
  --root /path/to/repo_root \
  --bind 0.0.0.0 \
  --port 8080
```

Set `paths.publish_url_base` to the address clients will use, for example `http://repo-host:8080`, then generate client configuration with:

```bash
./offline-repoctl export-snippets
```

### 2. Serve directly from the USB drive after crossing the gap

Before disconnecting the drive, install the self-contained standard-library server into the repository root:

```bash
./prepare-portable.sh /path/to/repo_root
```

On the disconnected machine:

```bash
/media/USB/OFFLINEREPO/serve-offlinerepo.py \
  --bind 0.0.0.0 \
  --port 8080
```

The helper defaults to the directory containing itself, so no absolute path is embedded in the portable copy.

### 3. Move the tree off USB and serve locally

Copy the complete tree to local storage, preserving hardlinks if the source uses them, then run the copied helper from the new root:

```bash
rsync -aH /media/USB/OFFLINEREPO/ /srv/OFFLINEREPO/
/srv/OFFLINEREPO/serve-offlinerepo.py --bind 0.0.0.0 --port 8080
```

The server deliberately returns 404 for `apt/state/`, `logs/`, and dot-prefixed paths and disables directory listings. Public package metadata, packages, and public signing keys remain available normally.

## Full-sync go/no-go sequence

Before spending the bandwidth and storage for a full synchronization:

```bash
./offline-repoctl validate
./offline-repoctl preflight
./offline-repoctl sync
./prepare-portable.sh /path/to/repo_root
```

A clean `validate` proves local configuration invariants. A clean `preflight` proves that the configured repositories can be resolved and every enumerated package payload can be reached. `sync` then performs the real signature-checked, incremental mirror operation.

After the first full synchronization, rerun `sync` against the same `repo_root` for updates. Do not delete APT state between runs if you want APT to remain incremental.
