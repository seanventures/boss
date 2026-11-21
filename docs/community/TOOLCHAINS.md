# Build toolchains

BOSS builds, tests and previews projects inside its build sandbox (see [SANDBOX.md](SANDBOX.md)).
Python 3.11 and Node 20 are always installed. **Java and Go are on by default**, because most
projects people bring use one of them. Each install can turn either off (and the security scanners
on), so an install that never builds Java can drop the JDK.

| Toolchain | What you get | Default | Image cost |
|---|---|---|---|
| `python`, `node` | Always installed | always | — |
| `java` | Temurin JDK 21.0.12.1+1, Maven 3.9.16, Gradle 8.14.6 | on | about 0.5 GB on the api/worker image |
| `go` | Go 1.27.4 (`go`, `gofmt`) | on | about 0.25 GB on the api/worker image |
| `security` | Semgrep, OSV-Scanner, OWASP ZAP (includes Java) | off (opt-in) | see the security review docs |

`BOSS_TOOLCHAINS` in the install's `.env` is `none` or a comma-separated list with no spaces,
such as `java,go`. With no `BOSS_TOOLCHAINS` line (or an empty one) the install gets the
default, `java,go`. An unknown name stops the image build with a message that says what is
accepted.

With the default, a first image build downloads the JDK, Maven, Gradle and Go (about 0.75 GB
more on the api/worker image than `none`), and the egress proxy opens Maven Central, the Gradle
plugin portal and the Go module proxy for sandboxed builds (it opens them only when the image
carries the toolchain).

## Existing installs, after an upgrade

- **No `BOSS_TOOLCHAINS` line in `.env`** (the toolchains were never touched): the next
  `boss update` or `boss upgrade` builds Java and Go in. Nothing is written to `.env`, so the
  install keeps following the default.
- **`BOSS_TOOLCHAINS=none`** (someone turned them off): stays off. An explicit value always wins.
- **`BOSS_TOOLCHAINS=java`, `go`, `security`, …**: kept exactly as it is.

Every service built with `BOSS_TOOLCHAINS` is rebuilt together: api and worker (they build
projects), egress (it opens Maven Central, the Gradle portal and the Go proxy only when its own
image has the toolchain) and the runner when it runs. `boss update`, `remote.sh update` and
`boss toolchains enable|disable` all do this. `boss toolchains` and `remote.sh update` say so
when a running service carries different toolchains than the api, with the command that fixes it.

## Turn a toolchain on or off

On the BOSS host:

```sh
boss toolchains                 # what .env asks for (or the default), and what the running image has
boss toolchains disable java    # saves BOSS_TOOLCHAINS=go in .env, rebuilds api, worker and egress, restarts them
boss toolchains disable go      # the last one off saves BOSS_TOOLCHAINS=none, and that is kept
boss toolchains enable go       # back on: BOSS_TOOLCHAINS=go
boss toolchains enable security # the scanners (opt-in), on top of Java
```

You can also set `BOSS_TOOLCHAINS=java,go` (or `none`) in the install's `.env` yourself and run
`boss update`.

`security` is built on the Java stage (OWASP ZAP needs its JRE), so an install with `security`
has Java whatever the list says. `boss toolchains disable java` refuses while `security` is on and
tells you to turn the scanners off first.
Every later `boss update` and `boss upgrade` keeps the setting, because the installer builds
with that `.env`.

**Setup › Toolchains** shows which toolchains this install has. A toolchain
that is off shows the command that turns it on.

## Why a build stage, and not a volume or a second image

`Dockerfile.community` has one stage per toolchain (`toolchains-none`, `toolchains-java`,
`toolchains-go`, `toolchains-security`). From `ARG BOSS_TOOLCHAINS` it works out, for each
toolchain, either that toolchain's stage or the empty one (`tc-java`, `tc-go`, `tc-security`).
The `toolchains` stage lays their `/opt/toolchains` trees over one another and merges their
manifests into one `manifest.json`, and `python-runtime` copies `/opt/toolchains` from it.
BuildKit skips any stage that nothing uses, so an install with `BOSS_TOOLCHAINS=none` never
downloads a JDK or Go. We picked this over the alternatives:

- **A separate `api-java` image** would have to be built, tagged and kept in step with the api,
  worker, egress and mcp-tools images. Every compose file, the upgrade path and `boss prune`
  would need to know about it.
- **A toolchain volume mounted read-only** would need a populate step outside the image, and
  its contents could drift from the pinned versions. It would also add a third place where the
  sandbox reads binaries from.

With a build stage there is one image, one tag, and one setting in `.env`, and the versions are
reviewed in the Dockerfile.

Each download is pinned and checked against its published checksum before it's unpacked:
SHA-256 for the JDK (one per architecture) and Gradle, SHA-512 for Maven, SHA-256 for the Go
release tarball (one per architecture, as published on go.dev/dl). The downloads use
Python's `urllib`, and nothing is piped into a shell. The JDK's `jmods` and `src.zip` are
removed, which means `jlink` isn't available.

## What the build loop does with a Java project

- **Detection.** The repository root has a `pom.xml` (Maven), or a `build.gradle`,
  `build.gradle.kts` or `settings.gradle(.kts)` (Gradle), and no `package.json`. A `pom.xml`
  wins when both build files are there. A task's own `test_commands` and a `boss.verify.yaml`
  still come first.
- **Gate.** Maven runs `mvn -B -ntp verify` (compile, test, package). Gradle runs
  `gradle --no-daemon --console=plain build`. The gate has 20 minutes, because a first build
  downloads its dependencies.
- **Gradle wrapper.** BOSS uses the image's pinned Gradle, not `./gradlew`. The wrapper
  downloads a distribution from `services.gradle.org`, which is not on the egress allowlist.
- **Java turned off.** The build fails with: *"This project needs Java (it has a pom.xml), and
  Java is not turned on for this BOSS. Turn it on with `boss toolchains enable java` …"*.

## What the build loop does with a Go project

- **Detection.** The app's directory has a `go.mod` and no `package.json` or Python manifest. A
  task's own `test_commands` and a `boss.verify.yaml` still come first.
- **Gate.** `go vet ./...`, then `go test ./...` (two steps, each run without a shell). The gate
  has 20 minutes, like Java's, because a first build downloads its modules.
- **Running it.** A Go backend is built with `go build` into the run's temporary directory (the
  module root when it is `package main`, else the first `./cmd/<name>` with a `main.go`) and
  started; it must listen on `$PORT`. `boss.preview.command` still overrides this.
- **The pinned Go only.** The image rewrites Go's own `$GOROOT/go.env` to
  `GOPROXY=https://proxy.golang.org`, `GOSUMDB=sum.golang.org`, `GOTOOLCHAIN=local`, so `go` is
  safe even without a per-run environment, and every sandboxed run gets the same plus
  `GOPRIVATE`, `GONOPROXY`, `GONOSUMDB`, `GONOSUMCHECK`, `GOINSECURE` set empty. A `toolchain` line
  in `go.mod` never downloads another Go. `-mod=readonly` is not set: a Go change needs `go get`
  and `go mod tidy` to update go.mod and go.sum, and every module they add is still checked
  against the checksum database.
- **Go turned off.** The build stops before anything is spent: *"This build needs Go, which is
  not turned on for this BOSS: turn it on with `boss toolchains enable go` on the BOSS host (or
  set BOSS_TOOLCHAINS=go in .env … and run `boss update`) …"*. A tree with a `go.mod` and no Go
  gets the same words from the gate (`toolchain_missing`), never the npm or Python default.

## A stack BOSS does not have is never swapped for another

When the brief asks for a language that is off on this install (Go or Java without its
toolchain), the build stops and says how to turn it on. When it asks for a language BOSS cannot
build at all (Rust, Ruby, PHP, .NET, Elixir, Scala), the build stops with `stack_unsupported`. In
neither case does BOSS build the app in another language: a panel or an architecture that picks
"the closest supported stack" does not override the language the operator asked for. A tree that
holds such a language's manifest (`Cargo.toml`, `Gemfile`, …) and no Node or Python app gets a
`toolchain_missing` gate in words, never the npm or Python default.

A `go.mod` (or a Maven/Gradle build) in the app's directory without its toolchain is
`toolchain_missing` **even with a `package.json` or a Python manifest beside it**, so a repair that
adds one cannot turn the gate into npm's. The plan reports it as a person's to fix
(`GatePlan.needs_human`), not a repairable failure. When a directory holds both a Java build and
a `go.mod`, the one most of its source is written in decides (Java on a tie).

## Network and caches

- Maven and Gradle reach the internet only through boss-egress's CONNECT proxy. They ignore
  `HTTPS_PROXY`, so at run start BOSS writes the proxy into the run's own `~/.m2/settings.xml`
  and `~/.gradle/gradle.properties`. The built-in allowlist gains
  these hosts and nothing else: `repo.maven.apache.org`, `repo1.maven.org`,
  `plugins.gradle.org` and `plugins-artifacts.gradle.org` (only on an install with Java). A project that needs another
  repository (a company Nexus, JitPack) has to be allowed by the operator (`BOSS_EGRESS_CONNECT_ALLOW`,
  or `BOSS_MAVEN_MIRROR` for a mirror of everything: [Enterprise network](ENTERPRISE_NETWORK.md)).
- Go honours `HTTPS_PROXY`, so it needs no config file. Each run gets
  `GOPROXY=https://proxy.golang.org` (no `direct`: a module is never fetched from its source
  host) and `GOSUMDB=sum.golang.org`, and the allowlist gains exactly those two hosts. The proxy
  serves module zips itself, so `storage.googleapis.com` is not needed. Nothing may skip the
  proxy. A company module proxy replaces both (`BOSS_GOPROXY`, `BOSS_GOSUMDB`), and its internal
  modules can be named in `BOSS_GONOSUMDB` ([Enterprise network](ENTERPRISE_NETWORK.md)).
- **Toolchain hosts open only with the toolchain.** boss-egress reads the image's manifest: the
  Maven and Gradle hosts are allowed only when Java is installed, the Go hosts only when Go is.
  Every allowed host is also a way out: a module proxy fetches any public path it is asked for,
  so a run could encode data into its requests. An install that doesn't build Go doesn't open the
  Go proxy at all. `BOSS_EGRESS_CONNECT_ALLOW` adds hosts to this list (`BOSS_EGRESS_CONNECT_DEFAULTS=off` drops the built-in hosts).
- **Caches are kept per project.** When a build ends, BOSS copies the run's `~/.m2/repository`
  and `~/.gradle/caches/modules-2` (Java), or `~/go/pkg/mod` (`GOMODCACHE`) and
  `~/.cache/go-build` (`GOCACHE`) (Go), into `/var/lib/boss/toolchain-cache/<hash of project id>`.
  That directory is on the `boss_community_toolchain_cache_v2` volume and is readable only by the
  `boss` user. The project's next build starts with a copy of it. A build never gets another
  project's cache, and no two runs share a live directory, because each run works on its own
  copy under its own uid. `BOSS_TOOLCHAIN_CACHE_MAX_MB` (default 2048) caps what is kept for
  each cache.

## Previews

A Java project with no other app at its root previews like this:

```sh
mvn -B -ntp -q -DskipTests package          # or: gradle --no-daemon -q -x test assemble
exec java -Dserver.port="$PORT" -jar target/<app>.jar [server config.yml]
```

- `server config.yml` is added when a `config.yml` (or `config.yaml`) is present, which is the
  Dropwizard layout. The app must listen on `$PORT`. In Dropwizard, wrap the configuration
  source in `SubstitutingSourceProvider` with `EnvironmentVariableSubstitutor(false)` and write
  `port: ${PORT:-8080}`. Spring Boot reads `-Dserver.port` by itself.
- **Health check.** BOSS checks the preview at `/`. An API with nothing at `/` names its own
  path in a one-line `boss.preview.health` file at the root, for example `/hello`.
- **Your own command.** `boss.preview.command` still overrides all of this.
- **Start time.** A Java preview gets 10 minutes to build and bind, instead of 3.

## Container previews and deploys

- **Container preview.** With no Dockerfile in the app, BOSS writes a two-stage one: the jar is
  built in `maven:3.9-eclipse-temurin-21` (or `gradle:8.14-jdk21`) and runs on
  `eclipse-temurin:21-jre`, on `$PORT` (8080). A Java app's own `boss.preview.command` runs in
  the build tool's image, since it needs the JDK.
- **One-container deploy** (no `boss.deploy.yaml`). BOSS builds the jar where it runs, with this
  install's toolchain, ships only `app.jar` (and a Dropwizard `config.yml`), and the host runs it
  on `eclipse-temurin:21-jre` with `-Dserver.port=$PORT`, `/data` kept across deploys. The
  health gate waits 240 s for a Java deploy instead of 120 s. Without Java turned on, the deploy
  says how to turn it on instead of failing on a missing `index.html`.
- **Pre-check.** A Maven or Gradle app is checked for the Java toolchain (not Node), and its
  health path is read from `@GetMapping`/`@RequestMapping`/`@Path` routes, or
  `/actuator/health` when Spring Boot Actuator is a dependency.
- **Agents** on an install with Java are told its JDK, Maven and Gradle versions, and to use the
  image's `gradle` rather than `./gradlew`.
- **Memory.** BOSS sets no JVM heap flags: a JVM sizes its heap at 25% of the memory it sees by
  default. If Spring Boot tests run out of memory in the build, set `JAVA_TOOL_OPTIONS` for the
  install after measuring, rather than guessing.

## A project's own image (built by hand)

When a project needs something no toolchain stage carries (a different JDK or Go, browsers, a
private CLI), build its image once instead of reinstalling it every run. This needs the docker
build runtime (`BOSS_SANDBOX_COMMAND_RUNTIME=docker`); the console says so when it is off.

1. **Project › Settings › Toolchain › Build image** starts a throwaway setup container from
   `BOSS_TOOLCHAIN_DEFAULT_BASE` (default: `BOSS_SANDBOX_CONTAINER_IMAGE`), as root, with network
   (`BOSS_TOOLCHAIN_SETUP_NETWORK`, default `bridge`).
2. **Open terminal** runs commands in it. Every command is audited and kept; the ones that
   succeed become the image's Dockerfile. Install system-wide — builds run as a non-root user.
   Anything typed here, logins included, is saved in the image.
3. **Capture** runs `docker commit`. With a registry the image is pushed and builds pin it by
   digest; without one it stays in the local daemon (`docker images boss-toolchain/*`).
4. The chip turns **Project image ready**. From then on every build of that project runs its
   commands in that image. An execution environment's `config.image` still wins; with neither,
   builds use the default image exactly as before.

The local registry is optional: `docker compose --profile toolchains up -d registry`, then set
`BOSS_TOOLCHAIN_REGISTRY=127.0.0.1:5000` in `.env` and recreate the api and worker. It listens on
the host loopback only.
