# syntax=docker/dockerfile:1

FROM python:3.11-slim AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# A real home for the boss user: npm and the Claude Code CLI both write there, and a
# non-existent HOME turns every runtime `npm install` into a permissions error rather than a
# missing-directory one — which reads as a code problem instead of an image problem.
RUN addgroup --system boss \
    && adduser --system --ingroup boss --home /home/boss boss \
    && mkdir -p /home/boss && chown -R boss:boss /home/boss
ENV HOME=/home/boss

FROM python-base AS api

# The coding toolchain. NOT optional, and its absence is invisible until a task runs.
#
# The governed pipeline executes `local_subprocess`: it shallow-clones the target repo with
# git and runs the project's own commands (npm ci, npm run build, the e2e step) inside THIS
# container. `infra/containers/api.Dockerfile` installed all of it and said so in a comment;
# when the images were consolidated into this file, the toolchain was not carried over.
#
# The result was a control plane that accepted code tasks and could not perform them. Runs
# were created, sat at `queued`, and were eventually closed as abandoned — for weeks, with
# nothing anywhere saying "there is no git in this container". It worked locally throughout,
# because a developer machine has git and node, which is exactly why nobody noticed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
    `# VIDEO EDITING IS A BOSS CAPABILITY, so its tool ships with the image rather than` \
    `# being something an operator installs. infra/containers/worker.Dockerfile has said so` \
    `# since the video skills landed — but that file builds the goldeneye images, and the` \
    `# community install builds from THIS one, where ffmpeg appeared nowhere. So every video` \
    `# step refused on a community install with "ffmpeg is not installed".` \
    `#` \
    `# libavfilter-extra brings drawtext, which needs libfreetype and is not in the base` \
    `# filter set: without it video.caption fails with "No such filter: 'drawtext'" only when` \
    `# it runs, which is after the render has already spent its time.` \
    ffmpeg libavfilter-extra fonts-dejavu-core \
    # Both coding CLIs, for the same reason git is here: a harness the control plane cannot
    # start is a harness that accepts work and never performs it. claude-code authorises from
    # CLAUDE_CODE_OAUTH_TOKEN; codex from CODEX_ACCESS_TOKEN via `codex login
    # --with-access-token`. Neither ships a credential — the image has the binary, the
    # deployment supplies the token.
    && npm install -g @anthropic-ai/claude-code @openai/codex \
    && npm cache clean --force \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Playwright Chromium at a shared, world-readable path: the verification step actually
# launches a browser, and without one the e2e check degrades to a skip that the review panel
# cannot act on — an unverifiable gate, which is worse than a failing one.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN npx -y playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw-browsers \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# The npx above ran as root with HOME already set, leaving a root-owned npm cache that breaks
# every runtime `npm install` for the boss user. Separate tiny layer so the heavy browser
# layer above stays cached.
RUN rm -rf /home/boss/.npm /home/boss/.cache && chown -R boss:boss /home/boss

# The coding CLIs keep their credential here, and a deployment may share it between containers
# as a named volume so one rotating session serves all of them. Docker seeds a fresh named
# volume from the image path and INHERITS ITS OWNERSHIP — with no such path in the image the
# volume arrives owned by root, and a container running as `boss` then cannot write the
# credential it was just given. Creating it here, owned correctly, is what makes the mount work.
RUN mkdir -p /home/boss/.codex /home/boss/.claude \
    && chown -R boss:boss /home/boss/.codex /home/boss/.claude \
    && chmod 700 /home/boss/.codex /home/boss/.claude

COPY apps/api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/api-requirements.txt

COPY apps ./apps
COPY api ./api
COPY db ./db
COPY packages ./packages
COPY scripts ./scripts
COPY tools ./tools
# Post-split the API's config/db/services are shims importing boss_engine, so the
# runtime image must carry engine/ on PYTHONPATH (same as infra/containers/api.Dockerfile).
COPY engine ./engine
# Runtime-read product ledgers. Settings.repo_root is /app in this image.
# Missing V1_FEATURE_LOCK.md makes GET /v1/v1-lock return honest zeros
# ({in:0, out:0, cloud:0}) rather than invented counts. TRACEABILITY.md
# seeds the feature catalog. Narrow copy — do not dump all of docs/.
COPY docs/product/V1_FEATURE_LOCK.md docs/product/V1_FEATURE_LOCK.md
COPY docs/product/TRACEABILITY.md docs/product/TRACEABILITY.md
ENV PYTHONPATH=/app:/app/engine

# WHICH SOURCE IS THIS RUNNING? The image carries no .git, so `git_history` on BOSS's own
# source fails and "does the running API contain that fix?" had no answer — an agent
# investigating a bug could read the source in the image and not tell whether it had shipped.
# The build stamps it; `inspect_self` and `git_history` report it.
ARG BOSS_SOURCE_SHA=unknown
ARG BOSS_BUILT_AT=unknown
ENV BOSS_SOURCE_SHA=$BOSS_SOURCE_SHA \
    BOSS_BUILT_AT=$BOSS_BUILT_AT

RUN mkdir -p /var/lib/boss/artifacts \
    && chown -R boss:boss /app /var/lib/boss

USER boss
EXPOSE 8000

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python-base AS worker

# Worker must carry the same coding toolchain as the api stage for any path where
# jobs shell out on the worker host (and to stay aligned with
# infra/containers/worker.Dockerfile). Goldeneye live QA (BOSS-342) found the
# multi-target consolidation dropped git from the worker image while api kept it:
# code.task deadlettered with `No such file or directory: 'git'`, and the worker
# tag lagged (`norebuild` vs api/web `view2`).
#
# Minimum contract: git (clone/diff for EphemeralSandbox / local_subprocess) and
# node/npm (project build / typecheck / lint). Playwright/Claude stay on the api
# image where verification and builder CLIs run today — do not silently reintroduce
# a half-empty worker image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
    `# VIDEO EDITING IS A BOSS CAPABILITY, so its tool ships with the image rather than` \
    `# being something an operator installs. infra/containers/worker.Dockerfile has said so` \
    `# since the video skills landed — but that file builds the goldeneye images, and the` \
    `# community install builds from THIS one, where ffmpeg appeared nowhere. So every video` \
    `# step refused on a community install with "ffmpeg is not installed".` \
    `#` \
    `# libavfilter-extra brings drawtext, which needs libfreetype and is not in the base` \
    `# filter set: without it video.caption fails with "No such filter: 'drawtext'" only when` \
    `# it runs, which is after the render has already spent its time.` \
    ffmpeg libavfilter-extra fonts-dejavu-core \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY apps/api/requirements.txt /tmp/api-requirements.txt
COPY apps/worker/requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/api-requirements.txt -r /tmp/worker-requirements.txt

COPY apps ./apps
COPY api ./api
COPY db ./db
COPY packages ./packages
COPY scripts ./scripts
COPY tools ./tools
# Worker drives the same engine code paths — bundle engine/ like the api target.
COPY engine ./engine
# Same runtime ledgers as the api stage (v1 lock + catalog seed).
COPY docs/product/V1_FEATURE_LOCK.md docs/product/V1_FEATURE_LOCK.md
COPY docs/product/TRACEABILITY.md docs/product/TRACEABILITY.md
ENV PYTHONPATH=/app:/app/engine

# WHICH SOURCE IS THIS RUNNING? The image carries no .git, so `git_history` on BOSS's own
# source fails and "does the running API contain that fix?" had no answer — an agent
# investigating a bug could read the source in the image and not tell whether it had shipped.
# The build stamps it; `inspect_self` and `git_history` report it.
ARG BOSS_SOURCE_SHA=unknown
ARG BOSS_BUILT_AT=unknown
ENV BOSS_SOURCE_SHA=$BOSS_SOURCE_SHA \
    BOSS_BUILT_AT=$BOSS_BUILT_AT

RUN mkdir -p /var/lib/boss/artifacts \
    && chown -R boss:boss /app /var/lib/boss

USER boss

CMD ["python", "-m", "apps.worker.boss_worker.main"]

FROM python-base AS account-connect

COPY apps/account_connect/requirements.txt /tmp/account-connect-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/account-connect-requirements.txt

COPY apps/account_connect ./apps/account_connect

RUN chown -R boss:boss /app

USER boss
EXPOSE 8010

CMD ["uvicorn", "apps.account_connect.app.main:app", "--host", "0.0.0.0", "--port", "8010"]

FROM node:20-bookworm-slim AS web-builder

WORKDIR /app

RUN npm install --global pnpm@9.15.0

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/api-client/package.json ./packages/api-client/package.json
RUN pnpm install --frozen-lockfile

# The build args come AFTER the dependency install, deliberately.
#
# They used to sit at the top of the stage, so changing the API URL — a one-line string that
# affects only the Next compile — invalidated `pnpm install` and re-ran the entire dependency
# tree. On an arm64 machine cross-building linux/amd64 that install runs under QEMU, which
# turned a config tweak into a ten-minute rebuild. Declaring them here keeps the slow,
# rarely-changing layer cached and re-runs only the compile that actually reads them.
ARG NEXT_PUBLIC_BOSS_API_URL=
ARG NEXT_PUBLIC_BOSS_PROJECT_ID=boss-dogfood
ENV NEXT_PUBLIC_BOSS_API_URL=$NEXT_PUBLIC_BOSS_API_URL \
    NEXT_PUBLIC_BOSS_PROJECT_ID=$NEXT_PUBLIC_BOSS_PROJECT_ID

COPY apps/web ./apps/web
COPY packages/api-client ./packages/api-client

# Refuse to build a console that cannot reach a control plane.
#
# NEXT_PUBLIC_* values are compiled into the bundle, so omitting this arg produces an image
# that builds, pushes and deploys without a single complaint, then greets every operator with
# "Reconnecting to the control plane…". The failure is a twenty-minute round trip away from
# its cause. Omitting one build flag should not be that expensive.
#
# Deployments that intentionally supply the address at runtime instead (Kubernetes sets
# BOSS_API_URL on the server, which the client honours) say so explicitly rather than by
# leaving the value blank.
ARG ALLOW_UNSET_API_URL=
RUN if [ -z "$NEXT_PUBLIC_BOSS_API_URL" ] && [ -z "$ALLOW_UNSET_API_URL" ]; then \
      echo "ERROR: NEXT_PUBLIC_BOSS_API_URL is empty." >&2; \
      echo "  Pass --build-arg NEXT_PUBLIC_BOSS_API_URL=https://boss-api.local.sean.network" >&2; \
      echo "  or --build-arg ALLOW_UNSET_API_URL=1 if BOSS_API_URL is set at runtime." >&2; \
      exit 1; \
    fi

RUN pnpm --filter boss-web build

FROM node:20-bookworm-slim AS web

ARG NEXT_PUBLIC_BOSS_API_URL=
ARG NEXT_PUBLIC_BOSS_PROJECT_ID=boss-dogfood
ENV NODE_ENV=production \
    PORT=3000 \
    NEXT_PUBLIC_BOSS_API_URL=$NEXT_PUBLIC_BOSS_API_URL \
    NEXT_PUBLIC_BOSS_PROJECT_ID=$NEXT_PUBLIC_BOSS_PROJECT_ID

WORKDIR /app

RUN npm install --global pnpm@9.15.0 \
    && addgroup --system boss \
    && adduser --system --ingroup boss boss

COPY --from=web-builder --chown=boss:boss /app/package.json ./package.json
COPY --from=web-builder --chown=boss:boss /app/pnpm-lock.yaml ./pnpm-lock.yaml
COPY --from=web-builder --chown=boss:boss /app/pnpm-workspace.yaml ./pnpm-workspace.yaml
COPY --from=web-builder --chown=boss:boss /app/node_modules ./node_modules
COPY --from=web-builder --chown=boss:boss /app/apps/web ./apps/web
COPY --from=web-builder --chown=boss:boss /app/packages/api-client ./packages/api-client

WORKDIR /app/apps/web

USER boss
EXPOSE 3000

CMD ["pnpm", "start", "-H", "0.0.0.0"]
