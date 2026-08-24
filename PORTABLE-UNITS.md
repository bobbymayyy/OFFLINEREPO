# Portable repository units

OFFLINEREPO treats `config.yml` as the only sync-selection interface. There is no separate batch mode.

`./offline-repoctl sync` processes exactly the enabled repository configuration:

- an enabled APT profile contributes only its enabled mirrors; each mirror is one independent signed repository unit, and its `components:` list is the component selection for that unit;
- an enabled RPM profile contributes only its enabled `repos:` entries; each repo ID is one independent repository unit;
- an enabled Alpine profile contributes only its enabled mirrors; each mirror is one independent repository unit.

This allows a removable drive to be filled in stages without combining unrelated distributions into one package-manager repository.

## APT unit layout

Each configured APT mirror/suite is self-contained:

```text
apt/debian/debian-trixie/
├── .state/aptly/                   # aptly database + package pool for this unit only
├── dists/trixie/...                # signed client-facing metadata
├── pool/...                        # client-facing packages
├── offline-repo-signing-public.gpg
├── offline-repo-signing-public.asc
└── .offlinerepo-unit.json
```

If `debian-trixie` selects `main`, `contrib`, and `non-free-firmware`, aptly keeps one internal mirror/snapshot per component and publishes those selected components together as the `trixie` distribution. The resulting `Release`, `Release.gpg`, and `InRelease` belong to this unit only.

The same OFFLINEREPO signing identity may sign every APT unit. A separate private key per suite is not required. The private key remains on the connected sync host and is never copied into the repository unit.

Because `.state/` lives inside the unit, the complete unit can be copied or moved independently. The built-in HTTP server does not expose dot-prefixed state paths.

## RPM and Alpine units

RPM reposync produces one usable directory per repo ID:

```text
rpm/rocky/9/baseos/
rpm/rocky/9/appstream/
```

Each directory gets its own `.offlinerepo-unit.json`. Package signatures remain the upstream vendor signatures and the downloaded repository metadata remains usable directly by DNF/YUM.

Alpine mirrors are likewise independent:

```text
apk/alpine/alpine-v3.24-main/
apk/alpine/alpine-v3.24-community/
```

Alpine's upstream index/signature files are preserved.

## Normal operator flow

Choose the repositories in `config.yml`, then run the normal commands:

```bash
./offline-repoctl validate
./offline-repoctl preflight
./offline-repoctl sync
./offline-repoctl state
```

There is no extra selector on `sync`.

For example, on the first trip enable Debian and Kali plus the exact APT mirrors/components desired. Sync them, cross the gap, then either serve the USB directly or move its repository units to disconnected storage.

On a later trip, change `config.yml` so Proxmox and Rocky are enabled and the earlier profiles are disabled if they are not needed on that trip. Running the same `./offline-repoctl sync` creates or updates those selected units.

## Checkout as the repository root

Cloning OFFLINEREPO directly onto the staging or removable filesystem and setting `paths.repo_root` to that same checkout directory is a supported first-class layout:

```text
/media/USB/OFFLINEREPO/
├── .git/
├── config.yml
├── offline-repoctl
├── serve-offlinerepo.py
├── lib/
├── apt/                     # runtime repository data
├── rpm/                     # runtime repository data
├── apk/                     # runtime repository data
├── keys/                    # generated public keys
└── .offlinerepo-index.json
```

In this layout, `./offline-repoctl sync` recognizes when a portable helper is already the exact same file as its destination and treats that helper as already installed. Re-running sync or `./offline-repoctl install-helpers` is therefore idempotent instead of failing with a same-file `cp` error.

The checkout's root-level runtime directories and generated helper/state files are ignored by Git, so mirrored package content does not flood `git status` or become an accidental normal `git add` target.

The built-in HTTP server exposes only `apt/`, `rpm/`, `apk/`, and `keys/`. Source/configuration files such as `config.yml`, `offline-repoctl`, README files, and the Git checkout itself are not served even when the checkout and repository root are the same directory.

A separate data-only `paths.repo_root` remains equally supported. The checkout-root layout is simply another intentional operating mode.

## Serve directly from removable media

A successful sync leaves the portable HTTP helper in the repository root:

```bash
python3 /media/USB/OFFLINEREPO/serve-offlinerepo.py --bind 0.0.0.0 --port 8080
```

The repository units remain independent even when one HTTP server exposes several of them. An individual APT unit can also be served as its own HTTP root because it contains its own signed metadata and public signing key.

## Copy or move units to the disconnected machine

Copy the repository units currently present on the drive:

```bash
python3 /media/USB/OFFLINEREPO/offload-offlinerepo.py /srv/OFFLINEREPO
```

Or remove each source unit from the USB only after the destination copy has completed and verified:

```bash
python3 /media/USB/OFFLINEREPO/offload-offlinerepo.py --move /srv/OFFLINEREPO
```

The destination remains a directory of independent units. This is not a package-manager-level merge.

If the destination already contains the same unit, only that unit is reconciled to the new source state. Destination-only files may be removed **inside that unit** after replacement content is copied. Other units are never removed merely because they are absent from the current USB.

The offload helper also preserves source hardlinks when the destination filesystem supports them, which keeps aptly's state/published package relationship space-efficient.

## Keeping network incrementality after an emptied USB

If `repo_root` is the USB and you use `--move`, the connected machine no longer has those package payloads unless another local copy exists. A state manifest alone cannot avoid a future network transfer because the packages themselves must physically exist somewhere before they can be carried across again.

Use `paths.permanent_root` on the connected machine when you want to empty/reuse the USB **and** preserve incremental network behavior:

```yaml
paths:
  repo_root: /media/USB/OFFLINEREPO
  permanent_root: /srv/offlinerepo-cache
```

With that configured, the normal `./offline-repoctl sync` workflow does the bookkeeping automatically:

1. determine the repository units enabled in `config.yml`;
2. for each enabled unit missing from the returned USB, restore only that unit from `permanent_root`;
3. run the normal APT/DNF/rsync synchronization so only upstream changes need network transfer;
4. refresh the persistent copy after a successful sync.

Disabled or unrelated repositories are not restored to the USB just because they exist in `permanent_root`.

This gives you a persistent connected-side cache without changing the operator command. It does require enough connected-side storage to retain the units whose payloads you want to reuse later.

If `permanent_root` is empty, the other option is to copy the complete unit back onto the USB before returning to the connected side. Otherwise a deleted payload is genuinely no longer local and must be downloaded again when needed.

## Stateful return trip

APT's `.state/` travels with its unit. That makes the unit portable rather than tied to one machine. When `permanent_root` is configured, the connected-side persistent copy retains that state and OFFLINEREPO can restore it automatically to removable staging. Without a persistent copy, return the complete unit if you want its next APT update to resume from the existing aptly database and pool.
