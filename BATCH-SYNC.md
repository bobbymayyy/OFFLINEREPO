# Incremental batch states

OFFLINEREPO can be moved across a disconnected boundary in multiple independent batches. This is separate from ordinary incremental update behavior:

- **incremental update** means a later sync of the same profile reuses unchanged package payloads;
- **incremental state** means the disconnected repository can accumulate different profiles over multiple USB trips without deleting profiles that are absent from the current trip.

## Example: two trips

Assume `debian`, `kali`, `proxmox`, and `rocky` are enabled in `config.yml`.

On the connected sync machine, prepare the first batch:

```bash
./offline-repo-batch preflight debian kali
./offline-repo-batch sync debian kali
```

The wrapper creates a temporary configuration that enables only the requested profiles. It never edits `config.yml`. It also disables `paths.permanent_root` for the batch run because the controller's permanent mirror mode intentionally uses deletion semantics and is not appropriate for accumulating partial states.

Take the portable drive to the disconnected machine and merge it into the cumulative serving tree:

```bash
python3 /media/usb/OFFLINEREPO/merge-offlinerepo.py /srv/OFFLINEREPO
```

You may serve `/srv/OFFLINEREPO` immediately. You may also serve directly from the USB instead of merging it.

The USB can now be reused or emptied. Back on the connected side, prepare the second batch:

```bash
./offline-repo-batch preflight proxmox rocky
./offline-repo-batch sync proxmox rocky
```

Merge the second batch into the same disconnected tree:

```bash
python3 /media/usb/OFFLINEREPO/merge-offlinerepo.py /srv/OFFLINEREPO
```

The resulting disconnected repository contains Debian, Kali, Proxmox, and Rocky. The second merge does **not** delete Debian or Kali merely because those profiles are absent from the second USB batch.

## Updating one profile later

A later trip can contain only one already-imported profile:

```bash
./offline-repo-batch preflight debian
./offline-repo-batch sync debian
```

The merge copies changed/new Debian files and updates Debian repository metadata while leaving Kali, Proxmox, Rocky, and any other destination-only profiles in place. Old package payloads that are no longer referenced by current metadata are intentionally not deleted during a partial merge. That favors safety over automatic reclamation; destructive pruning should only be done from a known-complete state.

## State manifest

Each successful batch sync writes:

```text
.offlinerepo-state.json
```

The file records the profiles known to be present in that portable tree and the most recent batch selection. It is hidden from the built-in HTTP server because dot-prefixed paths are not served.

`merge-offlinerepo.py` unions the source manifest with the destination manifest and records recent imports. This makes the cumulative disconnected state inspectable instead of relying on operator memory.

## Profile selectors

List profiles and whether they are enabled:

```bash
./offline-repo-batch list
```

Bare names work when unique:

```bash
./offline-repo-batch sync debian kali rocky
```

Family-qualified names are also supported and avoid ambiguity if a future configuration reuses a name:

```bash
./offline-repo-batch sync apt:debian rpm:rocky
```

A requested profile must already be enabled in `config.yml`. Mirror-level `enabled` flags within that profile continue to be honored.

## Merge safety

The merge helper is deliberately non-destructive:

- destination-only files are never removed;
- unchanged files are skipped using size and modification time;
- changed files are copied to a temporary sibling and atomically renamed into place;
- package payloads are copied before repository-switching metadata where the layout permits it;
- `apt/state/`, logs, rsync partial files, and hidden operational files are not copied;
- APT state is not required on the disconnected serving host.

Use the same OFFLINEREPO APT publishing key for all APT batches so every imported APT publication remains trusted by the same offline clients.

## Dry-run a merge

```bash
python3 /media/usb/OFFLINEREPO/merge-offlinerepo.py --dry-run /srv/OFFLINEREPO
```

This reports files that would be copied while making no changes.
