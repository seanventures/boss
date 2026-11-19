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

## Models running on macOS

The Compose `host.docker.internal:host-gateway` mapping points to the Linux VM's
Docker bridge. Do not assume it reaches a model server on macOS. For a Mac-hosted
model, use Colima's `host.lima.internal` address in the model connection and check
reachability from the BOSS container. The server must listen on an address
reachable from the VM. Connecting and probing a model is a separate validation
from bringing up the control plane.
