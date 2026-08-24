# OFFLINEREPO

Portable, config-driven Linux repository mirroring for disconnected, lab, air-gapped, recovery, and bandwidth-constrained environments.

OFFLINEREPO mirrors vendor repositories while connected, keeps every selected repository independently stateful, and leaves each synchronized unit ready to move, copy, or serve over ordinary HTTP.

## Supported repository families

| Platform | Default profile | Mirror method | Notes |
|---|---|---|---|
| Debian | 13 / Trixie | aptly | base, updates, security |
| Ubuntu | 24.04 LTS / Noble | aptly | 26.04 LTS / Resolute profiles included but disabled |
| Kali | rolling | aptly | main, contrib, non-free, non-free-firmware |
| Proxmox VE | PVE 9 / Trixie | aptly | PVE and Ceph no-subscription profiles |
| Fedora | 44 | DNF5 reposync | Fedora and updates |
| Rocky Linux | 9 | DNF reposync | BaseOS, AppStream, extras |
| RHEL | 9 | host DNF reposync | registered RHEL host; Satellite is not required |
| Alpine | 3.24 | rsync | main and community, x86_64 by default |
| NVIDIA CUDA | opt-in | aptly / DNF reposync | Debian, Ubuntu, RHEL/Rocky, Fedora platform channels |

All profiles are disabled by default. **`config.yml` is the source of truth.** The normal command is always:

```bash
./offline-repoctl sync
```

There is no second batch-selection interface. OFFLINEREPO synchronizes the profiles, mirrors, repos, suites, components, and architectures that are enabled in `config.yml` at that moment.

For the repository-unit model and transfer workflow, see [PORTABLE-UNITS.md](PORTABLE-UNITS.md). For the synchronization contract, see [FULL-SYNC-READINESS.md](FULL-SYNC-READINESS.md).

## Repository units

OFFLINEREPO does not require one giant repository state.

The independently movable unit is the lowest configured repository boundary:

- **APT:** one enabled `mirrors:` entry. Its selected `components:` are published together as one normal signed APT distribution.
- **RPM:** one enabled `repos:` entry / repo ID.
- **Alpine:** one enabled mirror.

A representative runtime tree looks like:

```text
OFFLINEREPO/
├── apt/
│   ├── debian/
│   │   ├── debian-trixie/
│   │   │   ├── .state/aptly/       # state for this suite only
│   │   │   ├── dists/trixie/
│   │   │   ├── pool/
│   │   │   └── .offlinerepo-unit.json
│   │   └── debian-trixie-security/
│   ├── kali/kali-rolling/
│   └── proxmox/proxmox-pve-trixie/
├── rpm/
│   └── rocky/9/
│       ├── baseos/
│       └── appstream/
├── apk/
│   └── alpine/
│       ├── alpine-v3.24-main/
│       └── alpine-v3.24-community/
├── keys/                           # public keys only
├── .offlinerepo-index.json         # local inventory, not served
├── serve-offlinerepo.py
└── offload-offlinerepo.py
```

The private APT publishing key never belongs in this tree.

### Why per-unit APT state matters

APT is the repository family that needs the extra architecture. Each configured suite/mirror gets its own aptly database and package pool under `.state/`, and its own signed publication at the unit root.

For example, `debian-trixie` can contain `main`, `contrib`, and `non-free-firmware`, while `debian-trixie-security` is a separate signed unit. Kali and Proxmox are separate again.

That means a suite can be:

- synchronized incrementally by itself;
- served directly from removable media;
- copied or moved to disconnected storage;
- brought back later with its own state for another incremental update.

The tradeoff is intentional: package bytes shared between two different APT suites are no longer deduplicated through one global aptly pool. In return, no suite depends on a giant shared synchronization database.

## First-time setup

```bash
git clone https://github.com/bobbymayyy/OFFLINEREPO.git
cd OFFLINEREPO
./offline-repoctl bootstrap
```

Set the staging root and serving URL in `config.yml`:

```yaml
paths:
  repo_root: /path/to/OFFLINEREPO
  permanent_root: ""
  publish_url_base: http://repo.local:8080
```

Then enable exactly what you want synchronized. Child `enabled:` values are honored too, so an enabled distro does not require every suite or repo under it to be selected.

Example idea:

```yaml
apt:
  - name: debian
    enabled: true
    mirrors:
      - mirror_name: debian-trixie
        enabled: true
        # ...
        components: [main, contrib, non-free-firmware]
      - mirror_name: debian-trixie-updates
        enabled: false
        # ...

rpm:
  - name: rocky
    enabled: false
    # ...
```

Build the helper images once:

```bash
./offline-repoctl build-images
```

Then the ordinary operating sequence is:

```bash
./offline-repoctl validate
./offline-repoctl preflight
./offline-repoctl sync
./offline-repoctl state
```

`state` lists the independent repository units that are actually present in `paths.repo_root`.

## Bandwidth-constrained staged synchronization

Nothing special changes at the command line.

For one trip, enable Debian and Kali in `config.yml`, plus only the suites/components you actually want:

```bash
./offline-repoctl preflight
./offline-repoctl sync
```

Move the drive across the boundary and either serve it directly or offload those units.

For a later trip, change `config.yml`: disable the repositories you do not need to refresh on this trip, enable Proxmox and Rocky, then run the exact same commands:

```bash
./offline-repoctl preflight
./offline-repoctl sync
```

The disconnected side may contain Debian and Kali already. Adding Proxmox and Rocky does not require rebuilding or merging their package-manager state with the earlier repositories.

## Incremental download behavior

OFFLINEREPO must refresh repository metadata to discover upstream changes. The expensive package payloads remain incremental:

- **APT / aptly:** each unit's package pool is reused on later `aptly mirror update` runs. OFFLINEREPO deliberately does not use aptly's `-skip-existing-packages` shortcut, so a payload that the database references but the drive has actually lost can be repaired.
- **RPM / DNF4 / DNF5:** reposync reuses RPMs already present locally, preserves upstream timestamps with `--remote-time`, and removes content that disappeared upstream with `--delete`.
- **Alpine / rsync:** the normal size/mtime quick check avoids retransferring unchanged files. Interrupted payloads live under `.rsync-partial` for reuse on the next run.

A blanket “never replace an existing path” option is intentionally avoided because repository indexes and rare same-path upstream corrections still need to change.

## Package reachability preflight

`validate` checks local configuration and invariants. `preflight` is the network-aware dry run:

```bash
./offline-repoctl preflight
```

It enumerates the payloads selected by the enabled configuration and checks whether they can be reached without intentionally downloading the complete package files into `repo_root`.

- APT exercises repository metadata, suite/component resolution, and upstream signature trust before probing package URLs.
- RPM uses reposync URL enumeration and probes each returned RPM URL.
- Alpine uses rsync dry-run listing against each selected source/architecture.

The transcript is written to `logs/preflight.log`.

## Why APT needs an OFFLINEREPO signing key

There are two trust jobs.

**Upstream archive keys** authenticate what OFFLINEREPO downloads. Debian content is checked using Debian trust, Ubuntu using Ubuntu trust, Kali using Kali trust, Proxmox using Proxmox trust, and NVIDIA using NVIDIA trust.

**The OFFLINEREPO publishing key** authenticates the new APT metadata produced by aptly. Once OFFLINEREPO republishes selected snapshots/components, it creates new `Release`, `Release.gpg`, and `InRelease` metadata. Offline clients therefore need a key you control to authenticate those publications.

One dedicated OFFLINEREPO signing identity can sign many independent APT units. Each unit has its own signed metadata; you do not need a different private key for every suite.

The trust flow is:

```text
vendor repository
    │ verify vendor metadata with vendor key
    ▼
aptly mirror + snapshot for one configured unit
    │ generate unit-specific repository metadata
    ▼
independent OFFLINEREPO APT unit
    │ sign with OFFLINEREPO private key
    ▼
offline client verifies with OFFLINEREPO public key
```

### Create the publishing key

```bash
install -d -m 0700 ~/.config/offline-repo

gpg --quick-generate-key \
  'OFFLINEREPO Repository <repo@offline.invalid>' \
  rsa3072 sign 3y

FPR="$(gpg --with-colons --fingerprint \
  'OFFLINEREPO Repository <repo@offline.invalid>' \
  | awk -F: '$1 == "fpr" {print $10; exit}')"

printf 'Fingerprint: %s\n' "$FPR"

gpg --armor --export-secret-keys "$FPR" \
  > ~/.config/offline-repo/repo-signing-private.asc
chmod 0600 ~/.config/offline-repo/repo-signing-private.asc
```

Configure the full fingerprint and private-key path:

```yaml
global:
  aptly_gpg_key: "FULL_FINGERPRINT_HERE"
  aptly_private_key_file: ~/.config/offline-repo/repo-signing-private.asc
  aptly_gpg_passphrase_file: ""
```

For unattended operation, a dedicated signing key without an interactive passphrase is the simplest model when the sync host itself is appropriately protected. If a passphrase file is configured, protect it with mode `0600`.

During APT sync, the private key is bind-mounted read-only into the helper container and imported into a temporary `GNUPGHOME`. Only the public key is exported into portable repository storage. The temporary GnuPG home is deleted when the sync completes.

Back up the private key separately. Losing it prevents future publications from being signed with the identity existing clients trust.

## APT suite and component handling

For a non-flat APT source, OFFLINEREPO creates one internal aptly mirror/snapshot per selected component, then publishes the selected snapshots together inside that suite's unit. This preserves normal client lines such as:

```text
deb [signed-by=/usr/share/keyrings/offline-repo.gpg] http://repo.local:8080/apt/debian/debian-trixie trixie main contrib non-free-firmware
```

Flat repositories such as NVIDIA CUDA are republished under the configured local distribution/component name so clients can consume them consistently.

### Migration from older shared APT state

Older OFFLINEREPO work stored aptly state under a shared `apt/state/` tree. The independent-unit design does not modify that old database. The first sync with this design creates fresh state under each `apt/<profile>/<mirror>/.state/` unit.

For a clean production migration, plan for the first per-unit APT run to populate the new unit state rather than assuming the old global aptly database can simply be split after the fact.

## Portable filesystem choice

The default APT publish method is:

```yaml
global:
  aptly_publish_link_method: hardlink
```

Within each APT unit, hardlinks let aptly's internal package pool and the published `pool/` tree share package bytes. Use a filesystem such as ext4 or XFS when practical.

If the staging filesystem cannot create hardlinks:

```yaml
global:
  aptly_publish_link_method: copy
```

This uses more capacity but remains correct.

The OFFLINEREPO offload helper attempts to preserve hardlink relationships on the destination. If the destination filesystem cannot create them, it falls back to copies.

## Serving directly from the connected host or USB

Serve the configured staging root from the connected host:

```bash
./offline-repoctl serve --bind 0.0.0.0 --port 8080
```

A successful sync also installs portable helpers in `repo_root`. On the disconnected machine you can serve the removable drive in place:

```bash
python3 /media/USB/OFFLINEREPO/serve-offlinerepo.py \
  --bind 0.0.0.0 \
  --port 8080
```

The server rejects dot-prefixed paths, so `.state/`, `.offlinerepo-unit.json`, and `.offlinerepo-index.json` are not exposed. Package metadata, packages, and public signing keys remain available.

This is a proactive static mirror rather than an on-demand proxy cache, but clients can use the connected or disconnected host as their single LAN repository endpoint after synchronization.

## Copy or move repository units off USB

Copy every unit currently present on the removable root into a disconnected storage root:

```bash
python3 /media/USB/OFFLINEREPO/offload-offlinerepo.py \
  /srv/OFFLINEREPO
```

Or free the USB after each unit is successfully copied and verified:

```bash
python3 /media/USB/OFFLINEREPO/offload-offlinerepo.py \
  --move /srv/OFFLINEREPO
```

The operation is **unit-aware**:

- if Rocky BaseOS is already present, only that BaseOS unit is reconciled;
- stale files may be removed inside the BaseOS unit;
- Debian, Kali, Proxmox, Alpine, or any other destination-only units are untouched;
- with `--move`, a source unit is removed only after the destination copy verifies.

The destination is still simply a collection of independent repository units. No giant package-manager repository is synthesized on the disconnected side.

### Returning a unit for future incremental updates

APT incremental state lives inside the unit. If you moved an APT unit off the USB and want to refresh it later, return the complete unit, including its hidden `.state/`, to the same staging path before the next connected sync.

That is what makes the state portable rather than tied to one particular USB device or machine.

## Optional `permanent_root`

If `paths.permanent_root` is configured, `./offline-repoctl sync` copies repository units there with the same unit-aware offload logic. It does **not** mirror-delete the entire destination simply because some unrelated repository is disabled for the current sync.

## RHEL without Satellite

RHEL content is mirrored from a normal registered and entitled RHEL host. Satellite is not required.

```bash
subscription-manager identity
./offline-repoctl preflight
./offline-repoctl sync
```

The configured RHEL repo IDs are enabled through `subscription-manager` when needed, then DNF reposync writes the same independent RPM-unit layout used by the other RPM families.

Use RHEL content in accordance with the subscription attached to the sync host.

## NVIDIA CUDA

CUDA profiles remain explicitly opt-in because the platform channels can be large. NVIDIA publishes distro/platform repositories that can contain multiple toolkit major versions. Enable only the platform channels needed for the disconnected environment.

## Client configuration snippets

Generate configuration only for repositories currently enabled in `config.yml`:

```bash
./offline-repoctl export-snippets
```

APT clients install the OFFLINEREPO public key and use `signed-by=`. RPM clients keep package `gpgcheck=1`. Alpine clients continue to use Alpine's signing trust.

## Operational commands

```text
./offline-repoctl bootstrap          install host prerequisites once
./offline-repoctl build-images       build helper containers
./offline-repoctl validate           validate config.yml selections
./offline-repoctl preflight          probe enabled repositories and package payloads
./offline-repoctl sync               sync exactly what is enabled in config.yml
./offline-repoctl state              list synchronized repository units
./offline-repoctl serve [options]    serve paths.repo_root over HTTP
./offline-repoctl install-helpers    refresh portable serve/offload helpers
./offline-repoctl tui                interactive interface
./offline-repoctl export-snippets    print client configuration for enabled repositories
```

## Design notes

- `config.yml` is the sole repository-selection interface.
- Repository failures are reported rather than silently converted to success.
- APT suites are independently stateful and independently signed.
- APT components selected for one suite are published together as a normal multi-component distribution.
- RPM and Alpine repository directories remain independent units.
- Package payload reuse is preserved for APT, RPM, and APK synchronization.
- Portable offload deletion is scoped to the same repository unit only.
- OFFLINEREPO's private APT signing key remains on the connected sync host.
- Public state manifests are intentionally hidden from HTTP clients.
