# OFFLINEREPO

> Portable Linux repository mirroring for disconnected, classified, lab, and air-gapped environments.

OFFLINEREPO provides a repeatable way to mirror software repositories from multiple Linux ecosystems onto removable storage, transport them into disconnected networks, and publish them locally for package management.

## Features

- Portable repository storage
- Air-gap friendly
- Docker-based tooling
- Signed APT repository publishing
- Snapshot retention
- Multi-distribution support
- Incremental synchronization
- Optional permanent repository synchronization
- Config-driven operation

## Supported Platforms

### APT

- Debian
- Ubuntu
- Kali Linux
- Proxmox VE
- NVIDIA CUDA repositories

### RPM

- Fedora
- Rocky Linux
- RHEL
- AlmaLinux
- Oracle Linux
- EPEL
- RPM Fusion
- NVIDIA CUDA

### APK

- Alpine Linux

## Repository Layout

```text
OFFLINEREPO/
├── apt/
├── rpm/
├── apk/
├── keys/
│   └── repo-signing-private.asc
└── state/
```

## Configuration

All mirroring behavior is controlled through `config.yml`.

```yaml
paths:
  repo_root: /run/media/user/OFFLINEREPO

global:
  architectures:
    - amd64
```

## Prerequisites

Build the container images:

```bash
docker build -f Dockerfile.debian -t offline-repo/debian-apt:stable .
docker build -f Dockerfile.fedora -t offline-repo/fedora-rpm:latest .
docker build -f Dockerfile.rocky -t offline-repo/rocky-rpm:latest .
```

## APT Repository Signing

Place your signing key at:

```text
OFFLINEREPO/keys/repo-signing-private.asc
```

Configure:

```yaml
global:
  aptly_gpg_key: YOUR_KEY_FINGERPRINT
```

## Mirroring

### APT

```bash
python3 sync_apt.py
```

### RPM

```bash
python3 sync_rpm.py
```

### Alpine

```bash
./sync_apk.sh
```

## Publishing Offline

Example NGINX configuration:

```nginx
server {
    listen 80;
    server_name repo.local;

    location /repo/ {
        alias /srv/repo/;
        autoindex on;
    }
}
```

## Example Workflow

### Online Environment

```bash
python3 sync_apt.py
python3 sync_rpm.py
./sync_apk.sh
```

### Offline Environment

```bash
rsync -av OFFLINEREPO/ /srv/repo/
```

Point clients at:

```text
http://repo.local/repo/
```

## Intended Use Cases

- Air-gapped environments
- Classified networks
- Military systems
- Incident response kits
- Portable cyber ranges
- Homelabs
- Disaster recovery repositories
- Software preservation
