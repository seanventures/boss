# Local macOS installation with Colima

Colima runs Docker Engine in a Linux VM. Docker Desktop is not required. Use the
Docker runtime, since the installer and lifecycle commands use Docker Compose.
The upstream release validation is Ubuntu x86_64; a working control plane on
Colima does not establish that every agent workflow is qualified on Apple Silicon.

## Runtime

Install the Homebrew formulae:

```bash
brew install colima docker docker-compose docker-buildx
```

Follow `brew info docker-compose` and `brew info docker-buildx` to make their CLI
plugins discoverable. Preserve existing Docker configuration and credentials.
Old plugins in `~/.docker/cli-plugins` can shadow Homebrew's plugins; check the
versions below and back up old links before changing them.

If a build reports that `docker-credential-osxkeychain` is missing, an existing
Docker configuration selects the macOS keychain helper but the executable is
absent. Install it with `brew install docker-credential-helper`; do not remove
stored credentials or replace the entire Docker configuration.

If Docker Hub's image CDN fails certificate verification, diagnose the network
and certificate chain without disabling TLS. A reachable, trusted registry mirror
can be configured under `docker.registry-mirrors` in the Colima profile and
activated with `colima restart boss`. Mirrors only help for images they cache;
an uncached image can still fall back to Docker Hub and encounter the original
failure. Back up the profile before changing it.

Create a dedicated profile. These are starting allocations, not minimums; the
virtual disk grows as used and still needs sufficient free space on the Mac.

```bash
colima start boss --runtime docker --vm-type vz --cpu 4 --memory 8 --disk 60
export DOCKER_CONTEXT=colima-boss
docker info
docker compose version
docker buildx version
```

Keep `DOCKER_CONTEXT=colima-boss` set for installation and all lifecycle commands.
Changing the active Docker context later otherwise makes BOSS commands address a
different daemon. Do not use `sudo docker` to repair a stopped Colima VM; start
the profile instead.

## BOSS

From a clean checkout of the chosen release:

```bash
./install.sh --dir "$PWD" --ref "$(git rev-parse HEAD)" \
  --no-install-deps --no-discover --no-local-model --skip-model-key --yes
```

Exported model keys are still imported by the installer. Run with those variables
unset if you want to connect a model later through Setup. The control plane can
start without a model. Open http://localhost:3000 and follow the
[claim instructions](QUICKSTART.md#claim-the-install). For projects that remain
local, choose **This machine** instead of connecting GitHub.

```bash
DOCKER_CONTEXT=colima-boss boss status
DOCKER_CONTEXT=colima-boss boss doctor
colima stop boss
colima start boss
```

Stopping preserves the VM and its volumes. Deleting the profile deletes its data.
Use the BOSS backup workflow before deleting or replacing a profile.

## Node package downloads behind a corporate gateway

When npm reports `UNABLE_TO_VERIFY_LEAF_SIGNATURE`, supply your organization's
approved public CA chain in PEM format through `BOSS_BUILD_CA_FILE`:

```bash
export BOSS_BUILD_CA_FILE=/absolute/path/to/approved-corporate-ca.pem
DOCKER_CONTEXT=colima-boss ./install.sh --dir "$PWD" --ref v1.0-rc10.31 \
  --no-install-deps --no-discover --no-local-model --skip-model-key --yes
```

Use an absolute path outside the checkout. Compose passes the file as a BuildKit
secret only to the application image builds. The affected npm, pnpm and Playwright
build steps use `NODE_EXTRA_CA_CERTS`, which extends Node's normal trust store.
TLS and hostname verification remain enabled. The certificate is not copied into
the resulting image or committed to the repository. With no file configured,
Node uses its existing trust configuration as before.

This option fixes missing build-time Node trust configuration; it cannot create
the correct corporate CA. It does not configure Docker daemon image pulls,
Python package downloads, or runtime model connections. Keep the export set for
later rebuilds, or store the absolute path in the private `.env`.

BuildKit does not invalidate cached layers when secret contents change. After a
CA rotation, rebuild the affected service with `docker compose ... build
--no-cache SERVICE` using this install's Compose file and `.env`.

## Models running on macOS

The Compose `host.docker.internal:host-gateway` mapping points to the Linux VM's
Docker bridge. Do not assume it reaches a model server on macOS. For a Mac-hosted
model, use Colima's `host.lima.internal` address in the model connection and check
reachability from the BOSS container. The server must listen on an address
reachable from the VM. Connecting and probing a model is a separate validation
from bringing up the control plane.
