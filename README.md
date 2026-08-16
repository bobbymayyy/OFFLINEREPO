# OFFLINEREPO

Portable, config-driven Linux repository mirroring for disconnected, lab, air-gapped, and recovery environments.

OFFLINEREPO pulls vendor repositories while online, stores them on removable or permanent storage, and leaves the resulting trees ready to serve over ordinary HTTP inside a disconnected network.

## Supported repository families

| Platform | Default profile | Mirror method | Notes |
|---|---|---|---|
| Debian | 13 / Trixie | aptly | base, updates, security |
| Ubuntu | 24.04 LTS / Noble | aptly | base, updates, security; 26.04 LTS / Resolute profiles included but disabled |
| Kali | rolling | aptly | main, contrib, non-free, non-free-firmware |
| Proxmox VE | PVE 9 / Trixie | aptly | pve-no-subscription and Ceph Squid no-subscription |
| Fedora | 44 | DNF5 reposync | Fedora and updates |
| Rocky Linux | 9 | DNF reposync | BaseOS, AppStream, extras |
| RHEL | 9 | host DNF reposync | BaseOS and AppStream from a registered RHEL host; no Satellite required |
| Alpine | 3.24 | rsync | main and community, x86_64 by default |
| NVIDIA CUDA | opt-in | aptly / DNF reposync | Debian 12/13, Ubuntu 24.04/26.04, RHEL/Rocky 9, Fedora 44 platform channels |

All profiles are disabled by default. Enable only what the portable repository actually needs. Full distro and CUDA mirrors can consume substantial storage.

NVIDIA publishes CUDA as distro/platform repository channels rather than a separate repository URL for each toolkit major. Mirroring a selected platform channel makes the CUDA 12 and CUDA 13 packages that NVIDIA still carries in that channel available offline. CUDA profiles are intentionally opt-in.

## Repository layout

The configured `paths.repo_root` is runtime data, not this Git repository.

```text
OFFLINEREPO/
├── apt/
│   ├── state/                 # aptly database, package pool, snapshots
│   ├── debian/...             # published APT trees
│   ├── ubuntu/...
│   ├── kali/...
│   ├── proxmox/...
│   └── cuda/...
├── rpm/
│   ├── fedora/44/...
│   ├── rocky/9/...
│   ├── rhel/9/...
│   └── cuda-*/...
├── apk/
│   └── alpine/...
└── keys/
    ├── offline-repo-signing-public.asc
    └── offline-repo-signing-public.gpg
```

The private APT signing key does **not** belong in this tree.

## First-time setup

```bash
git clone https://github.com/bobbymayyy/OFFLINEREPO.git
cd OFFLINEREPO

./offline-repoctl bootstrap
```

Edit `config.yml` and set at minimum:

```yaml
paths:
  repo_root: /path/to/OFFLINEREPO
  publish_url_base: http://repo.local/repo
```

Then enable the desired distro profiles. CUDA requires both the parent `cuda` APT profile and the desired per-platform mirror entry to be enabled. RPM CUDA profiles are separate opt-in profiles.

Build the helper images once:

```bash
./offline-repoctl build-images
```

Validate configuration syntax and local invariants:

```bash
./offline-repoctl validate
```

Before committing storage and bandwidth to the mirror, run the package reachability dry run:

```bash
./offline-repoctl preflight
```

Then sync:

```bash
./offline-repoctl sync
```

The TUI exposes the same validate, preflight, sync, and log operations:

```bash
./offline-repoctl tui
```

`bootstrap` is explicit and one-time. Normal commands no longer run operating-system updates or install packages as a side effect.

## Package reachability preflight

`validate` intentionally remains a fast, local configuration check. `preflight` is the network-aware dry run that answers a different question: **can the enabled repositories be resolved correctly, and can every enumerated package payload actually be reached before a large synchronization begins?**

```bash
./offline-repoctl preflight
```

The command does not intentionally download package payloads into `repo_root`. It does fetch the repository metadata needed to discover the package set and then reports every package with an `[OK]` or `[FAIL]` line. A complete transcript is written to:

```text
logs/preflight.log
```

The preflight behavior follows each repository family:

- **APT / aptly:** OFFLINEREPO asks aptly to create temporary mirror definitions with the configured URL, suite, components, architectures, and upstream keyrings. This catches bad URLs, nonexistent distributions/components, and Release-signature failures using aptly's own parsing/trust path. OFFLINEREPO then fetches the Release/InRelease and package indexes, verifies package-index size/SHA256 values when provided by Release metadata, enumerates every referenced `.deb`, source payload, and configured `.udeb`, and probes each package URL. Temporary aptly state is discarded after the check.
- **RPM / DNF:** OFFLINEREPO runs `dnf reposync --urls`/`dnf5 reposync --urls` against the configured repository. This resolves the same repo metadata and package set without downloading RPM payloads. Every returned RPM URL is then probed individually. Explicit CUDA GPG-key URLs are probed as well.
- **Alpine / rsync:** OFFLINEREPO performs an rsync dry run against each configured source/architecture. Every `.apk` returned by the source listing is printed as reachable. A bad rsync URI/module/path fails the family preflight.

HTTP/HTTPS package probes use `HEAD` first. If an upstream rejects `HEAD`, OFFLINEREPO falls back to a one-byte range GET, so the reachability check does not intentionally transfer the complete package. The preflight exits nonzero if any repository setup, metadata fetch, or package probe fails, but continues through the other enabled repositories so the final log contains the full failure set.

For large distributions this can still generate many requests because the point is to test **every package URL that would be mirrored**. Tune the concurrency and timeouts in `config.yml` if necessary:

```yaml
global:
  preflight_concurrency: 8
  preflight_timeout_sec: 15
  preflight_metadata_timeout_sec: 120
```

Preflight is a reachability/planning test, not a replacement for synchronization-time cryptographic verification. Actual APT/RPM sync continues to perform the configured upstream signature/package checks.

## Why APT needs an OFFLINEREPO signing key

There are two different trust jobs and therefore two different sets of keys.

**Upstream archive keys** authenticate what OFFLINEREPO downloads. Debian content is checked with Debian's archive keyring, Ubuntu with Ubuntu's, Kali with Kali's, Proxmox with Proxmox's, and CUDA with NVIDIA's. These trust sets are deliberately scoped so a key trusted for one vendor is not automatically trusted for another.

**The OFFLINEREPO publishing key** authenticates what offline APT clients receive from this mirror. aptly snapshots and republishes packages, generating new `Release`, `Release.gpg`, and `InRelease` metadata. The original vendor signature cannot authenticate metadata that aptly has regenerated, so the published repository must be signed again with a key that you control.

This does not mean OFFLINEREPO is replacing upstream verification. The flow is:

```text
vendor repository
    │
    │ verify vendor Release/InRelease with vendor key
    ▼
aptly mirror + snapshot
    │
    │ generate new repository metadata
    ▼
OFFLINEREPO published APT tree
    │
    │ sign with OFFLINEREPO private key
    ▼
offline client verifies with OFFLINEREPO public key
```

### Create the signing key

A dedicated repository-signing key is preferable to a personal identity key.

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

gpg --armor --export "$FPR" \
  > ~/.config/offline-repo/repo-signing-public.asc
```

Put the full fingerprint into `config.yml`:

```yaml
global:
  aptly_gpg_key: "FULL_FINGERPRINT_HERE"
  aptly_private_key_file: ~/.config/offline-repo/repo-signing-private.asc
```

For unattended sync, a dedicated signing key without an interactive passphrase is the simplest model when the sync host itself is encrypted and access-controlled. If a passphrase is used, configure a root/user-readable-only file:

```yaml
global:
  aptly_gpg_passphrase_file: ~/.config/offline-repo/repo-signing-passphrase
```

Protect that file with mode `0600`. A passphrase stored beside the key should not be treated as a separate security boundary.

During APT sync, `offline-repoctl` bind-mounts the private key read-only into the helper container. `sync_apt.py` imports it into a temporary `GNUPGHOME`, signs the publication, exports only the public key into `repo_root/keys/`, and deletes the temporary GnuPG home. The private key is never intentionally copied to the portable repository.

Back up the private key separately. Losing it means future repository metadata cannot be signed with the key your existing offline clients trust. If the key is compromised, rotate it and redistribute the new public key to clients.

## APT component handling

aptly merges components when a multi-component upstream is mirrored as a single aptly mirror. OFFLINEREPO instead creates one internal aptly mirror and snapshot per component, then publishes those snapshots together. That preserves ordinary client lines such as:

```text
deb ... trixie main contrib non-free non-free-firmware
```

Flat repositories such as NVIDIA CUDA use aptly's `./` distribution syntax internally and are republished under a normal local distribution/component name so clients can consume them consistently.

If you used an older OFFLINEREPO build to populate APT state already, its single-mirror component layout may not be switch-compatible with the new component-preserving publications. Use a fresh `apt/` state for the cleanest migration, or explicitly drop the old aptly publication before the first new sync. The tool does not destructively drop an existing publication automatically.

## Portable filesystem choice

The default aptly publish method is `hardlink`, which avoids storing a second copy of every published `.deb` and is the most space-efficient option:

```yaml
global:
  aptly_publish_link_method: hardlink
```

The repository drive must therefore use a filesystem that supports hardlinks. `offline-repoctl` tests this before APT sync. If the portable filesystem cannot create hardlinks, use:

```yaml
global:
  aptly_publish_link_method: copy
```

`copy` is more portable but can substantially increase APT storage usage because aptly's internal pool and published tree contain separate file copies.

## RHEL without Satellite

RHEL is intentionally handled differently from Rocky and Fedora. Full RHEL BaseOS/AppStream content requires Red Hat subscription entitlement, so OFFLINEREPO does not fake this with UBI or a generic container.

Use a normal registered RHEL 9 machine as the online sync host, enable the `rhel` profile, and run:

```bash
subscription-manager identity
./offline-repoctl preflight
./offline-repoctl sync
```

`offline-repoctl` verifies registration for both preflight and sync. Sync enables the configured BaseOS/AppStream repository IDs with `subscription-manager` when necessary and runs `dnf reposync` on the host. Preflight does not enable repositories or mutate subscription-manager state. The resulting repository is still written to the same portable `repo_root` and can be served the same way as Rocky/Fedora mirrors. Satellite is not required.

Use RHEL content in accordance with the subscription attached to the sync host.

## RPM repository behavior

RPM mirrors use `reposync --download-metadata`. The upstream RPM metadata is copied with the packages, so the resulting directory is immediately usable as a DNF/YUM repository and does not need `createrepo_c` for a full mirror.

For Fedora/Rocky/RHEL, package GPG verification is enabled while syncing. CUDA uses an explicit NVIDIA `baseurl`. OFFLINEREPO downloads the matching NVIDIA RPM public key into `repo_root/keys/rpm/`, configures that key for the temporary CUDA repository, and keeps package signature verification enabled during mirroring. The generated offline client stanza points `gpgkey=` at the locally served copy and keeps `gpgcheck=1` enabled.

Generate client `.repo` examples with:

```bash
./offline-repoctl export-snippets
```

## Alpine behavior

Alpine mirrors use rsync and preserve Alpine's repository index/signature files. The default profile mirrors only `x86_64` instead of every architecture; add architectures under the Alpine profile if needed. `--delay-updates` and `--delete-delay` reduce the amount of time a repository being served during synchronization can expose a mixed old/new tree.

## Serving the repository

Any static HTTP server can serve the configured `repo_root`. Example NGINX configuration:

```nginx
server {
    listen 80;
    server_name repo.local;

    # aptly state is needed for incremental sync, but it is not repository content.
    location ^~ /repo/apt/state/ {
        return 404;
    }

    location /repo/ {
        alias /srv/OFFLINEREPO/;
        autoindex off;
    }
}
```

Copy or mount the portable repository at `/srv/OFFLINEREPO`. Do not expose `apt/state/`; it contains aptly's operational database and pool state, not client-facing repository metadata. Then use:

```bash
./offline-repoctl export-snippets
```

The generated examples match the actual OFFLINEREPO paths, APT distributions/components, Fedora release, RHEL layout, and Alpine branch configured in `config.yml`.

APT clients must install `keys/offline-repo-signing-public.gpg` and reference it with `signed-by=`. RPM clients keep package `gpgcheck=1`. Alpine clients use the distribution signing keys already provided by Alpine.

## Operational commands

```text
./offline-repoctl bootstrap          install host prerequisites once
./offline-repoctl build-images       build helper containers using config tags
./offline-repoctl validate           validate configuration without network/package probing
./offline-repoctl preflight          enumerate and probe every package in enabled repositories
./offline-repoctl sync               sync every enabled repository
./offline-repoctl tui                interactive menu
./offline-repoctl export-snippets    print client configuration examples
```

## Design notes

- Repository families are independent. A failure in one mirror is reported without silently claiming success.
- Preflight continues across enabled repositories so a single run can expose the complete set of unreachable sources/packages.
- APT snapshots retain the configured number of generations.
- RPM and APK synchronization use incremental tools and remove content that disappeared upstream.
- APT upstream signing keys are narrowly scoped by vendor.
- OFFLINEREPO's APT private signing key remains on the online sync host.
- RHEL uses Red Hat's normal entitlement path without requiring Satellite.
- CUDA platform mirrors are opt-in because they are large.
