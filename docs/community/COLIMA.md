# Installing on macOS with Colima

Colima runs Docker Engine in a Linux VM, so BOSS runs on a Mac without Docker Desktop (often the
case where Docker Desktop's licence is not approved). The installer and every `boss` command use
Docker Compose, so use Colima's Docker runtime. BOSS's release validation runs on Ubuntu x86_64
and on Docker Desktop; a Colima install that starts the console is not proof that every agent
workflow is validated on Apple Silicon.

Behind a company proxy, a TLS-inspecting gateway or a private package registry, read
[Enterprise network](ENTERPRISE_NETWORK.md) as well: it covers the CA, the registries, the proxy
and the sandbox fence, and the Colima VM's own CA and proxy settings.

## Install the runtime

```bash
brew install colima docker docker-compose docker-buildx
```

`brew info docker-compose` and `brew info docker-buildx` print how to make the two CLI plugins
discoverable (a `cliPluginsExtraDirs` entry in `~/.docker/config.json`). Old plugins in
`~/.docker/cli-plugins` can shadow Homebrew's; check the versions below, and back up old links
before you change them. Keep your existing Docker configuration and credentials.

### The Docker credential helper

After Docker Desktop is removed (the usual way to Colima), `~/.docker/config.json` still names
Docker Desktop's credential helper, and every pull and build fails:

```
error getting credentials - err: exec: "docker-credential-desktop": executable file not found in $PATH
```

The installer checks this before it builds and stops with the fix. Do not delete saved logins or
replace the whole file:

1. Back it up: `cp ~/.docker/config.json ~/.docker/config.json.bak`.
2. `brew install docker-credential-helper` (the macOS keychain helper).
3. In `~/.docker/config.json`, change `"credsStore": "desktop"` to `"credsStore": "osxkeychain"`.

If the message names `docker-credential-osxkeychain`, step 2 alone is the fix.

## Create the profile

Give BOSS its own profile, sized like BOSS's reference host:

```bash
colima start boss --runtime docker --vm-type vz --cpu 4 --memory 12 --disk 100
export DOCKER_CONTEXT=colima-boss
docker info
docker compose version
docker buildx version
```

* **Disk 100 GB.** The installer warns below about 80 GB free and recommends about 100 GB: the
  images include Java and Go by default, and builds, caches and agent repositories grow. The VM
  disk grows as it is used, so the Mac needs that much free space too. To save about 0.75 GB, an
  install can leave the toolchains out (`BOSS_TOOLCHAINS=none`, see [Toolchains](TOOLCHAINS.md)).
* **Memory 12 GB or more** (16 GB if the Mac has 32 GB). BOSS needs 8 GB at least and is
  comfortable at 16 GB; the VM itself uses part of what you give it, and the full stack
  (Postgres, build services, the knowledge graph, monitoring, the console, the worker) in 8 GB is
  untested.
* **CPU 4.** Reviewers run in parallel.

### Keep the install directory under your home directory

Colima mounts only `$HOME` into the VM by default. BOSS's compose file bind-mounts paths from the
install directory (`.boss/selfsrc`, `.boss/import`, the database init scripts, the monitoring
configuration). Installed outside `$HOME`, those mounts are silently empty inside the containers.
Install under `$HOME`, or add the directory to the profile's `mounts:` (`colima edit boss`).

## Install BOSS

From a clean checkout of the release you chose (the `--ref` is that checkout's commit):

```bash
export DOCKER_CONTEXT=colima-boss
./install.sh --dir "$PWD" --ref "$(git rev-parse HEAD)" \
  --no-install-deps --no-discover --no-local-model --skip-model-key --yes
```

Model keys exported in the shell are still imported. Run with them unset if you would rather
connect a model later in Setup; the control plane starts without one. Open
http://localhost:3000 and follow the [claim instructions](QUICKSTART.md#claim-the-install). For
projects that stay on this machine, choose **This machine** instead of connecting GitHub.

The installer saves the Docker context it ran with (`BOSS_DOCKER_CONTEXT` in `.env`), and `boss`
and later installer runs use it, so they reach the Colima daemon even from a shell that selects
another one (Docker Desktop installed beside Colima, for example). An exported `DOCKER_CONTEXT`
or `DOCKER_HOST` still wins.

```bash
boss status
boss doctor
colima stop boss
colima start boss
```

Stopping keeps the VM and its volumes. Deleting the profile deletes BOSS's data: back up first
(`./scripts/install/backup.sh --dir "$PWD"`, see the
[installer guide](../../scripts/install/README.md)) before you delete or replace a profile.

## Engine inner-sandbox support

Only when a coding engine must use its own sandbox (a Codex login whose organisation forbids full
access) and it fails with `bwrap: No permissions to create a new namespace`. The Colima VM is
Ubuntu with AppArmor, so it needs both layers. From the install directory, with the profile
running:

```bash
colima --profile boss ssh -- sudo "$PWD/scripts/sandbox/load_apparmor.sh"
echo 'BOSS_ENGINE_INNER_SANDBOX=seccomp+apparmor' >> .env
boss start      # recreates api and worker with the profiles; run it when no build is running
boss doctor     # the "engine sandbox" line proves Codex's own sandbox starts
```

The install must be under `$HOME` (above), so the VM sees the loader at the same path. The
profile is installed in the VM's `/etc/apparmor.d`, so it is loaded again when the VM restarts. It
is gone if the profile is deleted and recreated: load it again then. What it allows, the attack
surface it opens, and how to turn it off: [Engine sandbox](ENGINE_SANDBOX.md#engine-inner-sandbox-support).

## When Docker is not running

With the Colima profile (or Docker Desktop) stopped, the installer and every `boss` command stop
with:

```
Docker is not running for the selected context (colima-boss). Start Docker Desktop, or your Colima profile (colima start <profile>), ...
```

Start the profile (`colima start boss`) and run the command again. `sudo docker` does not help: a
Mac has no root Docker socket to fall back to.

## Models running on the Mac

The compose file maps `host.docker.internal` to `host-gateway`. Depending on the Colima version,
that is the Mac or the VM's own Docker bridge. For a model server on the Mac (Ollama, a local
OpenAI-compatible server), check from a BOSS container which name reaches it:

```bash
docker compose -f scripts/install/community-compose.yml --env-file .env exec api \
  python -c "import urllib.request; print(urllib.request.urlopen('http://host.lima.internal:11434/api/tags', timeout=5).status)"
```

`host.lima.internal` reaches the Mac from inside a Colima VM; try `host.docker.internal` the same
way. The server must listen on an address the VM can reach, not only on 127.0.0.1. Connecting and
probing a model is a separate check from bringing up the control plane.

## Weekly verification checklist

Run this manual check on a real macOS host once per week as part of release readiness (see
[TEST_TIERS.md](../operations/TEST_TIERS.md#release-gates)):

1. Start from a clean checkout or release tag in a fresh directory on macOS with Colima installed.
2. Ensure Colima is running with sufficient Docker resources (`colima start boss --runtime docker --vm-type vz --cpu 4 --memory 12 --disk 100` or equivalent profile).
3. Run `./install.sh` (or the community install script) against a fresh install prefix and wait for all services to reach healthy status.
4. Run `boss status` and `boss doctor` and confirm every check passes with no degraded core services.
5. Run one sample build/preview smoke against the fresh install and verify completion and teardown/cleanup leave zero stray containers or volumes.
