# Build toolchains

BOSS builds, tests and previews projects inside its build sandbox (see [SANDBOX.md](SANDBOX.md)).
Python 3.11 and Node 20 are always installed. Other stacks are **opt-in for each install**, so an
install that never builds Java doesn't carry a JDK.

| Toolchain | What you get | Image cost |
|---|---|---|
| `python`, `node` | Always installed | — |
| `java` | Temurin JDK 21.0.12.1+1, Maven 3.9.16, Gradle 8.14.6 | about 0.5 GB on the api/worker image |

## Turn Java on

On the BOSS host:

```sh
boss toolchains enable java     # saves BOSS_TOOLCHAINS=java in .env, rebuilds api + worker, restarts them
boss toolchains                 # what .env asks for, and what the running image has
boss toolchains disable java    # back to none
```

You can also set `BOSS_TOOLCHAINS=java` in the install's `.env` yourself and run `boss update`.
Every later `boss update` and `boss upgrade` keeps the setting, because the installer builds
with that `.env`.

**Setup › Repo and models › Toolchains** shows which toolchains this install has. A toolchain
that is off shows the command that turns it on.

## Why a build stage, and not a volume or a second image

`Dockerfile.community` has one stage per toolchain (`toolchains-none`, `toolchains-java`).
`ARG BOSS_TOOLCHAINS` chooses one, and `python-runtime` copies `/opt/toolchains` from it. BuildKit
skips any stage that nothing uses, so a default install never downloads a JDK. We picked this
over the alternatives:

- **A separate `api-java` image** would have to be built, tagged and kept in step with the api,
  worker, egress and mcp-tools images. Every compose file, the upgrade path and `boss prune`
  would need to know about it.
- **A toolchain volume mounted read-only** would need a populate step outside the image, and
  its contents could drift from the pinned versions. It would also add a third place where the
  sandbox reads binaries from.

With a build stage there is one image, one tag, and one setting in `.env`, and the versions are
reviewed in the Dockerfile.

Each download is pinned and checked against its published checksum before it's unpacked:
SHA-256 for the JDK (one per architecture) and Gradle, SHA-512 for Maven. The downloads use
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

## Network and caches

- Maven and Gradle reach the internet only through boss-egress's CONNECT proxy. They ignore
  `HTTPS_PROXY`, so at run start BOSS writes the proxy into the run's own `~/.m2/settings.xml`
  and `~/.gradle/gradle.properties`. The default allowlist (`BOSS_EGRESS_CONNECT_ALLOW`) gains
  these hosts and nothing else: `repo.maven.apache.org`, `repo1.maven.org`,
  `plugins.gradle.org` and `plugins-artifacts.gradle.org`. A project that needs another
  repository (a company Nexus, JitPack) has to be added to the allowlist by the operator.
- **Caches are kept per project.** When a build ends, BOSS copies the run's `~/.m2/repository`
  and `~/.gradle/caches/modules-2` into `/var/lib/boss/toolchain-cache/<hash of project id>`.
  That directory is on the `boss_community_toolchain_cache` volume and is readable only by the
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
