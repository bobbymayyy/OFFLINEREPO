# OFFLINEREPO

Portable, config-driven Linux repository mirroring for disconnected, lab, air-gapped, recovery, and bandwidth-constrained environments.

OFFLINEREPO mirrors vendor repositories while connected, keeps each selected repository independently stateful, and leaves synchronized repository units ready to move, copy, or serve over ordinary HTTP.

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

All profiles are disabled by default.

## Core operating model

**`config.yml` is the source of truth.** There is no second batch-selection interface.

The normal synchronization command is always:

```bash
./offline-repoctl sync
```

OFFLINEREPO synchronizes the profiles, mirrors, repos, suites, components, and architectures that are enabled in `config.yml` at that moment. Child `enabled:` values are honored, so an enabled distro does not require every suite or repo beneath it to be selected.

The independently movable repository boundary is the lowest configured repository unit:

- **APT:** one enabled `mirrors:` entry. Its selected `components:` are published together as one normal signed APT distribution.
- **RPM:** one enabled `repos:` entry / repo ID.
- **Alpine:** one enabled mirror.

OFFLINEREPO therefore does not require one giant package-manager state database or one giant transfer set.

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
├── serve-offlinerepo.py            # portable helper installed by OFFLINEREPO
└── offload-offlinerepo.py          # portable helper installed by OFFLINEREPO
```

The private APT publishing key never belongs in this tree.

### Why per-unit APT state matters

APT is the repository family that needs the extra architecture. Each configured mirror/suite gets its own aptly database and package pool under `.state/`, plus its own signed publication at the unit root.

For example, `debian-trixie` can contain `main`, `contrib`, and `non-free-firmware`, while `debian-trixie-security` is a separate signed unit. Kali and Proxmox are separate again.

A complete APT unit can therefore be:

- synchronized incrementally by itself;
- served directly from removable media;
- copied or moved to disconnected storage;
- returned later with its own state for another incremental update.

The tradeoff is intentional. Package bytes shared between two different APT suites are no longer deduplicated through one global aptly pool. In return, no suite depends on a giant shared synchronization database.

RPM and Alpine naturally fit the same unit model because their repository directories are already independent.

## First-time setup

Clone the project and install host prerequisites:

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

Enable exactly what you want synchronized. For example:

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

If APT content is enabled, create and configure the OFFLINEREPO publishing key as described in [APT repository signing](#apt-repository-signing).

Build the helper images once:

```bash
./offline-repoctl build-images
```

## Proper operating sequence

For whatever is currently enabled in `config.yml`, use:

```bash
./offline-repoctl validate
./offline-repoctl preflight
./offline-repoctl sync
./offline-repoctl state
```

This sequence is intentionally the same whether one repository or twenty repositories are enabled.

- `validate` checks local configuration and selection invariants.
- `preflight` performs the network-aware dry run and package reachability checks.
- `sync` refreshes exactly the enabled repository units.
- `state` lists repository units actually present in `paths.repo_root`.

Configuration changes, not command-line selectors, determine the next synchronization set.

### Bandwidth-constrained staged synchronization

Nothing special changes at the command line.

For one trip, enable Debian and Kali in `config.yml`, plus only the suites/components you actually want:

```bash
./offline-repoctl preflight
./offline-repoctl sync
```

Move the drive across the boundary and either serve it directly or offload those units.

For a later trip, change `config.yml`: disable repositories that do not need refreshing, enable Proxmox and Rocky, then use the same commands:

```bash
./offline-repoctl preflight
./offline-repoctl sync
```

The disconnected side may already contain Debian and Kali. Adding Proxmox and Rocky does not require rebuilding or merging their package-manager state with the earlier repositories.

## Package reachability preflight

`preflight` enumerates the payloads selected by the enabled configuration and checks whether they can be reached without intentionally downloading complete package files into `repo_root`.

```bash
./offline-repoctl preflight
```

- **APT:** exercises repository metadata, suite/component resolution, and upstream signature trust before probing package URLs.
- **RPM:** uses reposync URL enumeration and probes the returned RPM URLs.
- **Alpine:** uses rsync dry-run listing against each selected source/architecture.

The transcript is written to `logs/preflight.log`.

## Incremental and interruption behavior

OFFLINEREPO refreshes repository metadata so upstream changes are discovered, but unchanged package payloads are reused.

| Family | Incremental behavior | Interrupted transfer behavior | Removal behavior |
|---|---|---|---|
| APT / aptly | Each enabled mirror/suite reuses the package pool under that unit's `.state/`. | Aptly mirror updates can be restarted safely. OFFLINEREPO deliberately does not use `-skip-existing-packages`, so a database-referenced payload that is physically missing can be repaired. | New snapshots switch the signed publication in place; older snapshots are trimmed by `keep_snapshots`. |
| RPM / DNF4 / DNF5 | Reposync reuses RPMs already present locally and preserves upstream timestamps with `--remote-time`. | Re-running reposync reuses completed RPMs. | `--delete` removes content no longer upstream, scoped only to that repo ID's unit. |
| Alpine / rsync | The normal size/mtime quick check skips unchanged file data. | Interrupted payloads live under `.rsync-partial` for reuse on the next run. | `--delete-delay` applies upstream removals only inside that Alpine mirror. |

Do not replace these behaviors with blanket `--ignore-existing` logic. Repository metadata and rare same-path upstream corrections must remain updateable.

## Portable repository workflow

### Checkout as the repository root

Cloning OFFLINEREPO directly onto the staging or removable filesystem and setting `paths.repo_root` to that same checkout is a supported first-class layout:

```text
/media/USB/OFFLINEREPO/
├── .git/
├── config.yml
├── offline-repoctl
├── lib/
│   ├── serve-offlinerepo.py        # source helper
│   └── offload-offlinerepo.py      # source helper
├── apt/                            # runtime repository data
├── rpm/                            # runtime repository data
├── apk/                            # runtime repository data
├── keys/                           # generated public keys
├── serve-offlinerepo.py            # generated portable copy
├── offload-offlinerepo.py          # generated portable copy
└── .offlinerepo-index.json
```

The generated root-level helper/state files and repository data are ignored by Git. The HTTP server exposes only `apt/`, `rpm/`, `apk/`, and `keys/`, so checkout files such as `config.yml`, `offline-repoctl`, README files, `lib/`, and `.git/` are not served.

A separate data-only `paths.repo_root` remains equally supported.

### Portable filesystem choice

The default APT publish method is:

```yaml
global:
  aptly_publish_link_method: hardlink
```

Within each APT unit, hardlinks allow aptly's internal package pool and the published `pool/` tree to share package bytes. Use a filesystem such as ext4 or XFS when practical.

If the staging filesystem cannot create hardlinks:

```yaml
global:
  aptly_publish_link_method: copy
```

`copy` uses more capacity but remains correct.

The offload helper preserves source hardlink relationships when the destination filesystem supports them and falls back to ordinary copies when it does not.

### Serving from the connected host or removable media

Serve the configured staging root from the connected host:

```bash
./offline-repoctl serve --bind 0.0.0.0 --port 8080
```

A successful sync installs portable serving and offload helpers into `repo_root`. On the disconnected machine, serve the removable drive in place:

```bash
python3 /media/USB/OFFLINEREPO/serve-offlinerepo.py \
  --bind 0.0.0.0 \
  --port 8080
```

The server rejects dot-prefixed paths. `.state/`, `.offlinerepo-unit.json`, `.offlinerepo-index.json`, legacy `apt/state/`, and logs are not exposed. Package metadata, package payloads, and public signing keys remain available.

This is a proactive static mirror rather than an on-demand proxy cache.

### Copy or move repository units off USB

Copy every unit currently present on the removable root into disconnected storage:

```bash
python3 /media/USB/OFFLINEREPO/offload-offlinerepo.py \
  /srv/OFFLINEREPO
```

Or free the USB after each unit is copied and verified:

```bash
python3 /media/USB/OFFLINEREPO/offload-offlinerepo.py \
  --move /srv/OFFLINEREPO
```

The operation is unit-aware:

- if Rocky BaseOS already exists at the destination, only that BaseOS unit is reconciled;
- stale files may be removed inside that same unit;
- Debian, Kali, Proxmox, Alpine, or other destination-only units are untouched;
- with `--move`, a source unit is removed only after the destination copy verifies successfully.

The destination remains a collection of independent repository units. No giant package-manager repository is synthesized on the disconnected side.

### Keeping incrementality after an emptied USB

Incremental network use requires the prior package payloads to physically exist somewhere. A state manifest by itself cannot prevent a future re-download if the package files are gone.

If `repo_root` is removable media and you use `--move`, configure `paths.permanent_root` when you want to empty/reuse the USB while retaining a connected-side copy for future incremental updates:

```yaml
paths:
  repo_root: /media/USB/OFFLINEREPO
  permanent_root: /srv/offlinerepo-cache
```

With `permanent_root` configured, the normal `./offline-repoctl sync` workflow automatically:

1. determines the repository units enabled in `config.yml`;
2. restores only enabled units that are missing from `repo_root` but available in `permanent_root`;
3. runs the normal APT/DNF/rsync synchronization so only upstream changes require network transfer;
4. refreshes the persistent copy after a successful sync.

Disabled or unrelated units in `permanent_root` are not automatically restored to the USB.

Without `permanent_root`, preserve incrementality by returning the complete unit to staging before its next update. Otherwise physically missing payloads must be downloaded again.

### Returning APT state for a later trip

APT incremental state lives inside each unit's hidden `.state/`. If an APT unit was moved off the USB and no `permanent_root` copy exists, return the complete unit to the same staging path before its next connected sync.

That is what makes the aptly state portable rather than tied to one USB device or machine.

## APT repository signing

APT has two separate trust jobs.

**Upstream archive keys** authenticate what OFFLINEREPO downloads. Debian content is checked using Debian trust, Ubuntu using Ubuntu trust, Kali using Kali trust, Proxmox using Proxmox trust, and NVIDIA using NVIDIA trust.

**The OFFLINEREPO publishing key** authenticates the new APT metadata produced by aptly. Once OFFLINEREPO republishes selected snapshots/components, it creates new `Release`, `Release.gpg`, and `InRelease` metadata. Offline clients therefore need a key you control to authenticate those publications.

One dedicated OFFLINEREPO signing identity can sign many independent APT units. A separate private key per suite is not required.

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

For unattended operation, a dedicated signing key without an interactive passphrase is the simplest model when the sync host is appropriately protected. If a passphrase file is configured, protect it with mode `0600`.

During APT sync, the private key is bind-mounted read-only into the helper container and imported into a temporary `GNUPGHOME`. Only the public key is exported into portable repository storage. The temporary GnuPG home is deleted when the sync completes.

Back up the private key separately. Losing it prevents future publications from being signed with the identity existing clients trust.

## APT suite and component handling

For a non-flat APT source, OFFLINEREPO creates one internal aptly mirror/snapshot per selected component, then publishes the selected snapshots together inside that suite's unit. This preserves normal client lines such as:

```text
deb [signed-by=/usr/share/keyrings/offline-repo.gpg] http://repo.local:8080/apt/debian/debian-trixie trixie main contrib non-free-firmware
```

Flat repositories such as NVIDIA CUDA are republished under the configured local distribution/component name so clients can consume them consistently.

### Migration from older shared APT state

Older OFFLINEREPO work stored aptly state under a shared `apt/state/` tree. The independent-unit design does not modify that old database. The first sync with the current design creates fresh state under each `apt/<profile>/<mirror>/.state/` unit.

For a clean migration, plan for the first per-unit APT run to populate the new unit state rather than assuming the old global aptly database can simply be split after the fact.

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

`./offline-repoctl install-helpers` is the supported way to refresh portable helper copies in an existing `repo_root`; no separate helper-preparation script is required.

## Design guarantees

- `config.yml` is the sole repository-selection interface.
- Repository failures are reported rather than silently converted to success.
- APT suites are independently stateful and independently signed.
- APT components selected for one suite are published together as a normal multi-component distribution.
- RPM and Alpine repository directories remain independent units.
- Package payload reuse is preserved for APT, RPM, and APK synchronization.
- Interrupted RPM and Alpine transfers are safe to restart with completed/partial payload reuse.
- Portable offload deletion is scoped to the same repository unit only.
- `paths.permanent_root` restores only enabled missing units and retains connected-side incremental state when desired.
- OFFLINEREPO's private APT signing key remains on the connected sync host.
- Portable serving exposes package content and public keys while hiding operational state and checkout files.
