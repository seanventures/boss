FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN addgroup --system boss && adduser --system --ingroup boss --home /home/boss boss \
    && mkdir -p /home/boss && chown -R boss:boss /home/boss
ENV HOME=/home/boss

# git: the governed code pipeline shallow-clones the target repo into an ephemeral sandbox.
# Node + Claude Code CLI: the claude_code agent shells out to `claude -p` (headless, authed via
# the CLAUDE_CODE_OAUTH_TOKEN env var) for the builder roles. Early, cacheable layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs npm \
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

COPY apps/api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/api-requirements.txt

COPY apps ./apps
COPY api ./api
COPY db ./db
COPY packages ./packages
COPY scripts ./scripts

RUN mkdir -p /var/lib/boss/artifacts \
    && chown -R boss:boss /app /var/lib/boss

USER boss

EXPOSE 8000

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
