# syntax=docker/dockerfile:1

FROM python:3.11-slim AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN addgroup --system boss && adduser --system --ingroup boss boss

FROM python-base AS api

COPY apps/api/requirements.txt /tmp/api-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/api-requirements.txt

COPY apps ./apps
COPY api ./api
COPY db ./db
COPY packages ./packages
COPY scripts ./scripts
COPY tools ./tools

RUN mkdir -p /var/lib/boss/artifacts \
    && chown -R boss:boss /app /var/lib/boss

USER boss
EXPOSE 8000

CMD ["uvicorn", "apps.api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python-base AS worker

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

ARG NEXT_PUBLIC_BOSS_API_URL=
ARG NEXT_PUBLIC_BOSS_PROJECT_ID=boss-dogfood
ENV NEXT_PUBLIC_BOSS_API_URL=$NEXT_PUBLIC_BOSS_API_URL \
    NEXT_PUBLIC_BOSS_PROJECT_ID=$NEXT_PUBLIC_BOSS_PROJECT_ID

RUN npm install --global pnpm@9.15.0

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/api-client/package.json ./packages/api-client/package.json
RUN pnpm install --frozen-lockfile

COPY apps/web ./apps/web
COPY packages/api-client ./packages/api-client
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
