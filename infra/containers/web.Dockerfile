FROM node:20-bookworm-slim AS builder

WORKDIR /app

RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json ./apps/web/package.json
COPY packages/api-client/package.json ./packages/api-client/package.json
RUN pnpm install --frozen-lockfile

COPY apps/web ./apps/web
COPY packages/api-client ./packages/api-client

ARG NEXT_PUBLIC_BOSS_API_URL=http://boss-api:8000
ENV NEXT_PUBLIC_BOSS_API_URL=$NEXT_PUBLIC_BOSS_API_URL

RUN pnpm --filter boss-web build

FROM node:20-bookworm-slim AS runtime

ENV NODE_ENV=production \
    PORT=3000 \
    HOME=/home/boss

WORKDIR /app

# Install pnpm globally (readable by the non-root user) instead of corepack-on-first-run, which
# tried to write to $HOME/.cache and crash-looped (EACCES, since HOME was /nonexistent). Also give
# boss a real, writable home for any runtime cache.
RUN npm install -g pnpm@9.15.0 \
    && addgroup --system boss \
    && adduser --system --ingroup boss --home /home/boss boss \
    && mkdir -p /home/boss && chown -R boss:boss /home/boss

COPY --from=builder --chown=boss:boss /app/package.json ./package.json
COPY --from=builder --chown=boss:boss /app/pnpm-lock.yaml ./pnpm-lock.yaml
COPY --from=builder --chown=boss:boss /app/pnpm-workspace.yaml ./pnpm-workspace.yaml
COPY --from=builder --chown=boss:boss /app/node_modules ./node_modules
COPY --from=builder --chown=boss:boss /app/apps/web ./apps/web
COPY --from=builder --chown=boss:boss /app/packages/api-client ./packages/api-client

USER boss

EXPOSE 3000

CMD ["pnpm", "--filter", "boss-web", "start", "--", "-H", "0.0.0.0"]
