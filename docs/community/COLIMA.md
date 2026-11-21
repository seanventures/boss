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

### Optional Codex nested sandbox support

If Codex reports a bubblewrap namespace denial, an administrator can opt this
installation into the scoped profiles below. They allow nested user/mount
namespaces for the API and worker only. They do not add SYS_ADMIN, privileged
containers, disable host AppArmor, or disable Codex's sandbox. Allowing namespace
and mount operations increases the container's kernel attack surface; review
the profiles before enabling them, especially on managed laptops.

The seccomp profile (`scripts/sandbox/engine-seccomp.json`) extends Moby's default-deny
profile pinned at `2ab3e516a3b49550c35b5e4511a0a7d136a02c8d`, vendored in
`scripts/sandbox/vendor/` with its license; the generator verifies its SHA-256 and never
fetches anything.
The AppArmor profile retains the Docker default profile's proc/sys restrictions,
allowing mounts and pivot_root for the nested sandbox. Source and license:
https://github.com/moby/profiles (Apache-2.0).

From the checkout, with the BOSS Colima profile running:

```bash
python3 scripts/sandbox/engine_seccomp.py --check   # the committed profile, regenerated offline
colima --profile boss ssh -- sudo tee /etc/apparmor.d/boss-engine < scripts/sandbox/boss-engine.apparmor >/dev/null
colima --profile boss ssh -- sudo apparmor_parser -r /etc/apparmor.d/boss-engine
```

Set `BOSS_CODEX_SECCOMP_FILE` in the installation's `.env` to the absolute Mac path of
`scripts/sandbox/engine-seccomp.json` in the install (no `~`). `boss start` and other `boss` lifecycle commands then include
`scripts/install/community-codex-sandbox.yml`. Direct Compose commands must pass
both `-f scripts/install/community-compose.yml` and
`-f scripts/install/community-codex-sandbox.yml` with `--env-file .env`. Recreate
API and worker only when no builds are running. Confirm the profile remains
loaded after a VM reboot; if needed, run the parser command again. Do not widen
the profiles automatically when a future CLI version needs new syscalls.

Rollback: remove `BOSS_CODEX_SECCOMP_FILE` from `.env`, recreate API and worker
with the base Compose file, then remove the unused AppArmor profile with
`colima --profile boss ssh -- sudo apparmor_parser -R /etc/apparmor.d/boss-engine`
and remove its VM file. No host sysctl changes are needed.

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
