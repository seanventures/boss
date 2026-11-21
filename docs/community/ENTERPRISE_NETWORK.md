# Enterprise network

A company network usually differs from a home network in four ways, and BOSS handles each one
with a setting in `.env`:

| What the network does | What breaks without a setting | The setting |
|---|---|---|
| A TLS-inspecting gateway re-signs HTTPS with the company's own CA | image builds (`UNABLE_TO_VERIFY_LEAF_SIGNATURE`, `CERTIFICATE_VERIFY_FAILED`, `x509`), then every model call, package install and git fetch at run time | `BOSS_EXTRA_CA_FILE` |
| Public package registries are blocked; an internal mirror (Artifactory, Nexus) serves them | `npm install`, `pip install`, `go mod download`, Maven, the Playwright browser download | `BOSS_NPM_REGISTRY`, `BOSS_PIP_INDEX_URL`, `BOSS_GOPROXY` (+ `BOSS_GOSUMDB`, `BOSS_GONOSUMDB`), `BOSS_MAVEN_MIRROR`, `BOSS_PLAYWRIGHT_DOWNLOAD_HOST` |
| The only way out is an HTTP proxy | everything that leaves the machine | `BOSS_UPSTREAM_PROXY`, `BOSS_UPSTREAM_NO_PROXY` |
| Internal hosts sit on private addresses and odd ports | BOSS's sandbox fence refuses them | `BOSS_EGRESS_CONNECT_ALLOW`, `BOSS_EGRESS_PRIVATE_ALLOW` |

All of them are optional, and empty means a stock install. They live in one block of the
install's `.env` (see `.env.example`), and every part of BOSS reads them from there: the image
build, every container, the sandboxed runs, and the sandbox fence.

## Set it up

Export what applies, then run the installer (a first install or an upgrade):

```bash
export BOSS_EXTRA_CA_FILE="$HOME/boss-extra-ca.pem"
export BOSS_NPM_REGISTRY=https://artifacts.example.com/api/npm/npm-virtual/
export BOSS_PIP_INDEX_URL=https://artifacts.example.com/api/pypi/pypi-virtual/simple
export BOSS_UPSTREAM_PROXY=http://proxy.example.com:8080
export BOSS_UPSTREAM_NO_PROXY=.example.com,10.0.0.0/8
./install.sh
```

The installer checks every value before it saves `.env`, and keeps them on every upgrade. A value
exported when the installer runs **replaces** the saved one; a run that exports nothing keeps
what `.env` holds. To clear a setting, empty it in `.env` and run `boss upgrade`. You can also
edit `.env` directly and run `boss upgrade`: an image-build setting needs the rebuild, and a
runtime one needs the containers recreated, which the upgrade does.

`boss doctor` checks the block again and asks the sandbox fence, from inside `boss-egress`,
whether each configured registry gets through.

### What a value may hold

Every URL is `https://` (a proxy may be `http://`), with a host and no user name, password, query
or fragment. A credential in a URL ends up in places that are hard to clean: an image build
argument is kept in `docker history`, and environment values show in process listings and logs.
The installer refuses such a value before it writes `.env`, and the image build refuses it again
(a `.env` edited by hand never reaches an image history). Messages name the setting, never the
value. Registry credentials are not supported through these settings (see
[Not covered yet](#not-covered-yet)).

## The company's CA (TLS inspection)

With a TLS-inspecting gateway, every HTTPS connection presents a certificate issued by the
company's own root CA. BOSS's images only trust the public CAs, so they need that root.

**Finding it.** Before the image build, the installer connects to the package registries it will
use and to Docker Hub. When the certificate is not one a public CA issued, it stops, names the
issuer, and on a Mac prints the command that exports it from the System keychain:

```bash
security find-certificate -a -c "Example Corp Root CA" -p /Library/Keychains/System.keychain > "$HOME/boss-extra-ca.pem"
```

Give BOSS the **root** CA (the top of the chain the gateway presents), as PEM, certificates only.
A file holding a private key is refused, and so is one over 500 KB (the most an image build can
take as a secret): give the root, not a whole exported keychain. If you cannot find it, ask IT for "the root CA of the
TLS inspection gateway". The check stops only when it actually read a certificate a public CA
did not issue; a host it cannot reach, or a connection reset during the handshake, is reported
as a note. `BOSS_TLS_PREFLIGHT=warn` lets the installer continue past the check
(the build then fails at its first download unless something else trusts the gateway).

**What trusts it.** `BOSS_EXTRA_CA_FILE` is ADDED to the public CAs, never a replacement, and
certificate and host name checks stay on everywhere:

* **image builds:** the file goes to BuildKit as a secret (`boss_build_ca`), mounted only while a
  download step runs. It is never in an image layer, an image's environment or `docker history`.
  Every step that downloads uses it: apt, curl, pip, the JDK and Go downloads (Python urllib), npm
  and pnpm, and the Playwright browser download.
* **every BOSS container built from the api image** (api, worker, runner, egress, model gateway,
  MCP tools, knowledge graph): the installer copies the file to `.boss/extra-ca.pem` in the
  install directory (a CA certificate is public), Compose mounts it read-only at
  `/etc/boss/extra-ca.pem` (the file always exists, empty when no CA is set; Docker never creates
  it, so a missing file stops `up` with a clear error instead of mounting a directory — run
  `boss upgrade` to write it), and each role starts through `with-extra-ca`, which writes one
  combined bundle and exports it for every process the role runs: `SSL_CERT_FILE` (OpenSSL,
  Python, httpx, Go, Rust), `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `PIP_CERT`, `GIT_SSL_CAINFO`,
  and `NODE_EXTRA_CA_CERTS` (Node and the coding CLIs). Sandboxed runs inherit them. With Java in
  the image, a Java trust store with the CA added is written too, and BOSS points each sandboxed
  run's Maven and Gradle at it.

A command you run yourself with `docker compose exec` does not go through that start-up step.
Prefix it to get the same trust: `docker compose exec api /usr/local/libexec/boss/with-extra-ca <command>`.

**After a CA rotation**, replace the file and run `boss upgrade`. BuildKit does not notice a
changed secret, so if a build step still fails, rebuild without its cache:
`docker compose -f scripts/install/community-compose.yml --env-file .env build --no-cache api`.

**There is no switch that turns TLS verification off**, for builds or at run time. The coding
CLIs run with model credentials and are installed from a lockfile with integrity hashes
(`scripts/install/cli-lock`), so neither a mirror nor a gateway can substitute them; turning
verification off would undo that.

## Package registries

| Setting | Used by | Notes |
|---|---|---|
| `BOSS_NPM_REGISTRY` | npm, pnpm, yarn v1 | `npm_config_registry`, with `npm_config_strict_ssl=true` forced. Yarn Berry reads its own `npmRegistryServer` and is not covered. |
| `BOSS_PIP_INDEX_URL` | pip | `PIP_INDEX_URL`; an inherited `PIP_EXTRA_INDEX_URL` or `PIP_TRUSTED_HOST` is dropped. |
| `BOSS_GOPROXY` | go | Replaces `proxy.golang.org` for sandboxed runs. There is still no `direct` fallback: modules come only from the proxy. |
| `BOSS_GOSUMDB` | go | `sum.golang.org` (default), optionally followed by the URL of a proxy for it (`sum.golang.org https://artifacts.example.com/api/go/gosumdb`). It cannot be `off`. |
| `BOSS_GONOSUMDB` | go | Module path patterns of internal modules the public checksum database does not know (`git.example.com/*`). They still come through `BOSS_GOPROXY`. |
| `BOSS_MAVEN_MIRROR` | Maven | A `<mirror>` of `*` in each sandboxed run's `settings.xml`. Gradle builds keep the repositories their build files name. |
| `BOSS_PLAYWRIGHT_DOWNLOAD_HOST` | Playwright | The browser download at image build and in runs. |

The npm, pip and Playwright settings apply to the image build and to every sandboxed run (code
agents, verification, previews, the console agent shell). Go and Maven apply to sandboxed runs.
Remote runners need the same values in their own environment.

An invalid value stops a run rather than falling back to the public registry, because falling
back would break the policy the mirror exists for. The installer and `boss doctor` catch it first.

Mirrors do not rewrite everything: package metadata and tarballs can come from different hosts,
a project's lockfile may name `registry.npmjs.org` in its URLs, scoped registries (`@scope:registry`)
and install scripts can reach elsewhere. Validate one full dependency install in a sandboxed run
before relying on it.

## The outbound proxy

`BOSS_UPSTREAM_PROXY` is the proxy BOSS's containers use for HTTPS to leave the network. Set it
explicitly. The installer never takes the proxy your shell exports on its own (a developer or
VPN proxy there would otherwise reroute every container, or replace a saved company proxy on
upgrade). To save the shell's `HTTPS_PROXY` and `NO_PROXY`, run the installer with
`--use-shell-proxy`. A proxy on `127.0.0.1` or `localhost` is refused: inside a container that
address is the container itself. Use `host.docker.internal` or the machine's network address.

`BOSS_UPSTREAM_HTTP_PROXY` is the proxy for plain `http://`, and is used only if you set it (for
example, when Debian's apt mirrors are reachable only through the proxy during the image build).
Without it, plain http (previews, app sidecars, a model server on the LAN) never goes to the
proxy.

* Image builds get `HTTPS_PROXY`, `HTTP_PROXY` (when set) and `NO_PROXY` for every download step.
* Every container gets them, with BOSS's own services (`api`, `egress`, `db` and the rest) and
  `localhost` always in `NO_PROXY`. Add internal hosts (a model server, a Git host) to
  `BOSS_UPSTREAM_NO_PROXY`: host names, `.domain` suffixes or CIDR ranges, comma-separated.
* Sandboxed runs never talk to the proxy themselves: they reach the internet only through
  BOSS's fence (`boss-egress`), which chains each allowed connection through the proxy. A name
  the company DNS cannot resolve is left for the proxy to resolve.

**A proxy that needs a login** (HTTP 407, NTLM, Kerberos) is not supported directly: the login
would sit in `.env` and in every container's environment. Run a local authenticating proxy
(cntlm, px) on the host and point `BOSS_UPSTREAM_PROXY` at it.

**A TLS-inspecting gateway sees what passes through it.** That is what inspection means: the
model API keys and subscription tokens, and the GitHub credentials BOSS sends, are visible to the
gateway in clear. BOSS cannot prevent this; agree with your security team that the gateway is
trusted with them, or exempt the model and GitHub hosts from inspection.

## The sandbox fence

Sandboxed runs (builds, previews, the coding CLIs) reach the internet only through `boss-egress`,
which allows a fixed list of hosts on port 443 and refuses any name that resolves to a private
address, so a run can never reach BOSS's own database or services.

* **Configured registries are allowed automatically**, host and port, from the settings above.
* **`BOSS_EGRESS_CONNECT_ALLOW`** ADDS hosts: `host`, `*.suffix`, or `host:port` for a port other
  than 443. (It used to replace the built-in list, so a host missed when copying it silently broke
  model calls.) `BOSS_EGRESS_CONNECT_DEFAULTS=off` drops the built-in list, for an install that
  wants only its own.
* **`BOSS_EGRESS_PRIVATE_ALLOW`** lets exact host names (an internal registry on `10.x`,
  optionally `:port`) through even though they resolve to a private address. It is an explicit
  opt-in: no wildcards and no bare service names, and the address is still refused when it is
  loopback, link-local (the cloud metadata service), multicast, IPv6 unique-local (`fc00::/7`,
  where cloud metadata also lives), the Docker host (`host.docker.internal`, host-gateway, the
  default gateway), or on one of BOSS's own container networks.
* GitHub never goes through this tunnel: it goes through BOSS's credential-injecting proxy.

**A refusal says so.** The fence answers with a 403 whose reason names the host and the setting
to change, and npm, pip and Go print it:

```
npm error 403 403 Blocked by BOSS network fence: artifacts.example.com is not on the egress allowlist. An operator can add artifacts.example.com to BOSS_EGRESS_CONNECT_ALLOW in .env ...
```

(Without that reason, npm reports a fence refusal as `E403 ... forbidden by your security policy`,
which reads like a registry token problem.) curl and git print only `CONNECT tunnel failed,
response 403`. Every refusal is logged with the host and port:
`docker compose -f scripts/install/community-compose.yml --env-file .env logs egress | grep "connect refused"`.

Never turn the fence off (`BOSS_SANDBOX_NETWORK=off`) to reach a registry: allow the host.

## Docker's own downloads (base images)

The images start from base images pulled by the Docker daemon, which has its own CA and proxy
settings, separate from everything above. All of BOSS's `FROM` lines are pinned by digest, so a
mirror can serve them but cannot substitute different content.

**Colima.** macOS keychain trust does not reach the VM. Add the CA and the proxy to the profile
(`colima edit boss`), then `colima restart boss`:

```yaml
provision:
  - mode: system
    script: |
      cp /Users/you/boss-extra-ca.pem /usr/local/share/ca-certificates/boss-extra-ca.crt
      update-ca-certificates
      systemctl restart docker
docker:
  proxies:
    http-proxy: http://proxy.example.com:8080
    https-proxy: http://proxy.example.com:8080
    no-proxy: localhost,127.0.0.1,.example.com
  registry-mirrors:
    - https://docker-mirror.example.com
```

The `proxies` key needs Docker Engine 23 or later inside the VM. Back up the profile before you
change it. A mirror helps only for images it caches; an uncached image may still go to Docker Hub.

**Docker Desktop.** Settings › Resources › Proxies for the proxy. On macOS it trusts CAs in the
System keychain for pulls; add the CA there if pulls fail.

**Linux.** Put the CA in `/usr/local/share/ca-certificates/` and run `update-ca-certificates`,
then `systemctl restart docker`. Give the daemon the proxy in `/etc/docker/daemon.json`
(`"proxies": {...}`, Docker 23+) or a systemd drop-in, and a mirror under `"registry-mirrors"`.
Many users behind one company address share Docker Hub's anonymous pull limit; a pull-through
mirror avoids it.

## Upgrading from an earlier configuration

* `BOSS_BUILD_CA_FILE` (an earlier name) is taken over as `BOSS_EXTRA_CA_FILE` on the next
  install or upgrade.
* `BOSS_EGRESS_CONNECT_ALLOW` now adds to the built-in hosts instead of replacing them. An install
  that already set it keeps exactly its fence: the upgrade writes `BOSS_EGRESS_CONNECT_DEFAULTS=off`.
  Set it to `on` when you want the built-in hosts as well.
* Older entries in `BOSS_EGRESS_CONNECT_ALLOW` that are IP addresses or `*` are kept as they were
  and produce a warning (in the installer and `boss doctor`), never a failed upgrade. A public
  address still works; `*` never matched anything and still does not.
* The shell's `HTTPS_PROXY` is no longer adopted automatically (see `--use-shell-proxy`).

## Not covered yet

* **Registry credentials.** These settings work with mirrors that allow anonymous reads. A
  token in a URL is refused, and a run never receives one (`npm_config_//host/:_authToken` is
  withheld from agents as credential-shaped). Authenticated registries will need the same design
  as GitHub: the fence adds the credential, so a sandbox never holds it.
* **Self-hosted Git** (GitHub Enterprise Server, GitLab) through the fence: only GitHub and
  Bitbucket are routed today.
* A download mirror for the JDK, Maven, Gradle and Go toolchain archives at image build (they
  are checksum-pinned, so a mirror would be safe; today they need the proxy and the CA).
* Yarn Berry's registry, and a Gradle repository mirror.

Keep company URLs, certificates and `.env` files out of public issues and patches.
