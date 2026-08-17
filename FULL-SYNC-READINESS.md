# Full-sync readiness and portability

OFFLINEREPO has two related guarantees:

1. repository metadata is refreshed so upstream changes are discovered, while unchanged package payloads are reused instead of downloaded again;
2. repository state is kept at an independently movable repository-unit boundary, so a constrained removable drive does not have to carry every distribution at once.

`config.yml` is the source of truth for both. `./offline-repoctl sync` needs no secondary batch selector.

## Incremental payload contract

| Family | Incremental behavior | Interrupted transfer behavior | Removal behavior |
|---|---|---|---|
| APT / aptly | Each enabled APT mirror/suite has its own aptly database and package pool under that unit's `.state/`. Re-running `aptly mirror update` reuses package files already present in that unit. | Aptly mirror updates can be restarted safely. OFFLINEREPO does not use `-skip-existing-packages`, allowing a missing local payload to be repaired. | New snapshots switch the signed publication in place; older snapshots are trimmed by `keep_snapshots`. |
| RPM / DNF4 / DNF5 | `reposync` avoids re-downloading RPMs already present and uses `--remote-time`. | Re-running reposync reuses completed RPMs. | `--delete` removes packages no longer present upstream, but only inside that repo ID's unit. |
| Alpine / rsync | Rsync's size/mtime quick check skips unchanged file data. | `.rsync-partial` stores incomplete payloads outside the served package namespace. | `--delete-delay` applies upstream removals inside that Alpine mirror only. |

Do not replace these behaviors with blanket `--ignore-existing` logic. Metadata and same-path upstream corrections must remain updateable.

## Independent state boundaries

APT is the important case. OFFLINEREPO no longer relies on one shared `apt/state/aptly` database for all configured suites. Instead, each enabled APT mirror is published from its own state directory:

```text
apt/<profile>/<mirror>/
├── .state/aptly/
├── dists/
├── pool/
└── .offlinerepo-unit.json
```

Selected components still form one normal multi-component APT distribution for that configured mirror/suite. The distribution is signed independently with the OFFLINEREPO publishing key.

This sacrifices cross-suite aptly-pool deduplication in exchange for much cleaner portability: one suite can be moved off removable media without taking every other suite's synchronization database with it.

RPM and Alpine naturally map to the same model because their local repository directories are already independent.

See `PORTABLE-UNITS.md` for the operator workflow.

## Portable filesystem choice

APT's default publish method remains `hardlink`, which avoids duplicating package bytes between a unit's aptly pool and its published `pool/` tree.

Use a filesystem with hardlink support, such as ext4 or XFS, when practical. If the staging media cannot create hardlinks, configure:

```yaml
global:
  aptly_publish_link_method: copy
```

`copy` uses additional capacity but does not change repository correctness.

The offload helper preserves source hardlinks when the destination filesystem supports them and falls back to ordinary copies when it does not.

## Serving modes

The same repository units can be served in three ways:

- from the connected sync host;
- directly from removable media after crossing the gap;
- after copying or moving individual units to disconnected storage.

The built-in HTTP helper refuses dot-prefixed paths, so per-unit `.state/` and state manifests are not exposed to clients. Client-facing package metadata and payloads remain available normally.

## Transfer safety

`offload-offlinerepo.py` works at the repository-unit level, not as a global mirror of the entire destination.

For an existing destination unit it copies payload/state files first, repository-switching metadata last, and only then removes destination files that no longer exist in the new source unit. A different repository unit is never deleted because it was absent from the current removable drive.

With `--move`, the source unit is removed only after the destination copy verifies successfully.

## Go/no-go sequence

For whatever is currently enabled in `config.yml`:

```bash
./offline-repoctl validate
./offline-repoctl preflight
./offline-repoctl sync
```

That sequence remains identical whether one suite or twenty repositories are enabled. Configuration changes, not command-line selectors, determine the next synchronization state.
