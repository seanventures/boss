FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/engine

WORKDIR /app

RUN addgroup --system boss && adduser --system --ingroup boss --home /home/boss boss \
    && mkdir -p /home/boss && chown -R boss:boss /home/boss
ENV HOME=/home/boss

# git: the governed code pipeline shallow-clones the target repo into an ephemeral sandbox.
# Node + Claude Code CLI: the claude_code agent shells out to `claude -p` (headless, authed via
# the CLAUDE_CODE_OAUTH_TOKEN env var) for the builder roles. Early, cacheable layer.
#
# openssh-client is NOT optional: BOSS deploys to customer hosts by shelling out to `ssh`,
# and mints host credentials with `ssh-keygen`. python:3.11-slim ships neither. Without it
# every fleet operation fails at runtime — key generation returned an opaque HTTP 500 and
# any SSH deploy would have died the same way. It worked in development purely because a
# developer laptop has ssh; the container is the only place that truth was tested.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
    `# VIDEO EDITING IS A BOSS CAPABILITY, and the API runs skills as well as the worker does` \
    `# (app/main.py imports the skill executors), so a video skill can land in EITHER process.` \
    `# The worker image has carried ffmpeg since the video skills landed and this one did not,` \
    `# which made the tool present or absent depending on which process picked the step up.` \
    `# libavfilter-extra + a font bring drawtext — see worker.Dockerfile for why that is split` \
    `# out of the base filter set.` \
    ffmpeg libavfilter-extra fonts-dejavu-core \
         openssh-client \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Playwright Chromium + its system libs, at a SHARED path readable by the non-root `boss` user:
# the web verification target's e2e step (`npm run test:e2e`) must actually launch a browser —
# without this it "runs" as a skip/failure the review panel can't execute, i.e. an unverifiable
# check. Sandboxed projects resolve browsers via PLAYWRIGHT_BROWSERS_PATH (persisted as ENV).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN npx -y playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw-browsers \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
# The npx above ran as root with HOME=/home/boss already set, leaving a ROOT-owned npm cache that
# breaks every runtime `npm install` for the boss user ("chown -R 100:101 /home/boss/.npm").
# Drop the stale cache (a separate tiny layer so the heavy browser layer above stays cached) —
# npm recreates it owned by boss on first use.
RUN rm -rf /home/boss/.npm /home/boss/.cache && chown -R boss:boss /home/boss

COPY apps/api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/api-requirements.txt

COPY apps ./apps
COPY api ./api
COPY db ./db
COPY packages ./packages
COPY scripts ./scripts
# The OSS engine package (boss_engine). Post-split the API's config/db/services
# are compatibility shims that import boss_engine, so it must be on PYTHONPATH
# (set to /app:/app/engine above). Its deps are a subset of the api's — no extra
# pip install needed.
COPY engine ./engine
# infra/ too: Ask BOSS can read BOSS's own source to answer "how does this work", and
# the deployment/compose/Dockerfile definitions are part of that answer. Omitting them
# left every infrastructure question unanswerable from inside the running system.
COPY infra ./infra

RUN mkdir -p /var/lib/boss/artifacts \
    && chown -R boss:boss /app /var/lib/boss

USER boss

EXPOSE 8000

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
