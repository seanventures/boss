# Supported stacks

<!-- Generated from engine/boss_engine/services/stacks.py (ADAPTERS, LANGUAGES). Do not edit by hand: run `python3 -m boss_engine.services.stacks --write-docs docs/community/SUPPORTED_STACKS.md` from engine/. A test fails when this file and the registry disagree. -->

What BOSS builds, tests and starts, and how sure we are of it. Levels:

- **certified** — the full workflow passed a recorded, repeatable benchmark run.
- **experimental** — BOSS builds, tests and starts it; the full lifecycle is not yet certified.
- **best effort** — BOSS attempts it with the same build code; no reliability claim.
- **not supported** — refused before anything is spent (at Start, at architecture approval and at the build).

Promotion: An experimental adapter becomes certified only when a recorded benchmark run for its id (GET /v1/delivery-workflows/runs/{run_id}/benchmark) passed end to end: result success, tests_passed equal to tests_total and above zero, and deployment_smoke_test pass. A maintainer then changes its support_level here, citing that run.

| Stack | Level | Languages | Frameworks | Databases |
|---|---|---|---|---|
| `react-vite+express+postgres` — React + Vite, Express, PostgreSQL | experimental | Node | react, vite, express | postgres, sqlite (local development) |
| `react-vite+fastapi+postgres` — React + Vite, FastAPI, PostgreSQL | experimental | Node, Python | react, vite, fastapi | postgres, sqlite (local development) |
| `nextjs+fastapi+postgres` — Next.js, FastAPI, PostgreSQL | experimental | Node, Python | next, fastapi | postgres, sqlite (local development) |
| `java-spring` — Java Spring Boot (with an optional React + Vite front end) | experimental | Java, Node | spring-boot, react, vite | postgres, sqlite (local development) |
| `python-service` — Python service (FastAPI, Flask, Django, WSGI) | best effort | Python | fastapi, flask, django, wsgi | postgres, sqlite (local development) |
| `node-service` — Node service (Express, Fastify, Koa, Hono, NestJS, node:http) | best effort | Node | express, fastify, koa, hono, nestjs, node-http | postgres, sqlite (local development) |
| `node-web` — Node web front end (Vue, Svelte, Angular) | best effort | Node | vue, svelte, angular |  |
| `java-dropwizard` — Java Dropwizard | best effort | Java | dropwizard | postgres, sqlite (local development) |
| `go` — Go | not supported | Go | — | — |
| `rust` — Rust | not supported | Rust | — | — |
| `ruby` — Ruby | not supported | Ruby | — | — |
| `php` — PHP | not supported | PHP | — | — |
| `dotnet` — .NET (C#) | not supported | .NET (C#) | — | — |
| `elixir` — Elixir | not supported | Elixir | — | — |
| `scala` — Scala | not supported | Scala | — | — |

## Languages

| Language | Runtime | Package manager | Detected by | Toolchain |
|---|---|---|---|---|
| Python | python >=3.11 | pip | `requirements.txt`, `pyproject.toml`, `setup.py`, `Pipfile` | always installed |
| Node | node >=20 | npm | `package.json` | always installed |
| Java | java 21 (Temurin) | maven or gradle | `pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle`, `settings.gradle.kts` | opt-in: `boss toolchains enable java` |

## Commands

Each service runs its own, in its own directory. `—`: not a separate step.

| Language | install | build | lint | typecheck | test | e2e |
|---|---|---|---|---|---|---|
| Python | `python3 -m pip install --user --disable-pip-version-check --no-input -q -r requirements.txt` | — | — | — | `python3 -m pytest -q` | — |
| Node | `npm install --include=dev --no-audit --no-fund --loglevel=error` | `npm run build` | `npm run lint` | `npm run typecheck` | `npm run test` | `npm run test:e2e` |
| Java | — | `mvn -B -ntp verify | gradle --no-daemon --console=plain build` | — | — | `mvn -B -ntp verify | gradle --no-daemon --console=plain build` | — |

## Starting a backend and serving a front end

BOSS starts the backend on `$PORT` (the command in the service's `boss.preview.command` wins) and asks `/` — or the path in `boss.preview.health` — for an answer before any e2e test runs. A front end whose Playwright config serves nothing is served built.

| Framework | Role | Command |
|---|---|---|
| FastAPI (Python) | backend | `exec python3 -m uvicorn <module>:<app> --host 0.0.0.0 --port "$PORT"` |
| Flask (Python) | backend | `exec python3 -m flask --app <module>:<app> run --host 0.0.0.0 --port "$PORT"` |
| Django (Python) | backend | `exec python3 manage.py runserver "0.0.0.0:$PORT" --noreload` |
| WSGI (Python) | backend | `exec python3 main.py` |
| Express (Node) | backend | `exec npm start` |
| Fastify (Node) | backend | `exec npm start` |
| Koa (Node) | backend | `exec npm start` |
| Hono (Node) | backend | `exec npm start` |
| node:http (Node) | backend | `exec npm start` |
| NestJS (Node) | backend | `exec npm start` |
| Next.js (Node) | frontend | `exec npm run start -- -H 127.0.0.1 -p "$PORT"` |
| React (Node) | frontend | `exec npm run preview -- --host 127.0.0.1 --port "$PORT" --strictPort` |
| Vite (Node) | frontend | `exec npm run preview -- --host 127.0.0.1 --port "$PORT" --strictPort` |
| Vue (Node) | frontend | `exec npm run preview -- --host 127.0.0.1 --port "$PORT" --strictPort` |
| Svelte (Node) | frontend | `exec npm run preview -- --host 127.0.0.1 --port "$PORT" --strictPort` |
| Spring Boot (Java) | backend | `java -Dserver.port="$PORT" -jar <the built jar>` |
| Dropwizard (Java) | backend | `java -jar <the built jar> server config.yml` |

A multi-service app is a backend and a web front end, each in its own directory with its own manifest. Data: `DATABASE_URL` (PostgreSQL) with a local SQLite file when it is unset, for local development.
