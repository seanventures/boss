# Standalone BOSS Engine image — ships the OSS engine with NO API or console.
#
# It runs the engine-native BOSS Agent and/or the MCP server against a Postgres
# the container can reach (DATABASE_URL). This is the "ship only the engine"
# artifact: nothing from apps/ is copied in.
#
# Build (from the REPO ROOT — the image vendors db/schema.sql + specpack):
#   docker build -f engine/Dockerfile -t boss-engine:dev .
# Bootstrap a fresh DB:    docker run --rm -e DATABASE_URL=... boss-engine:dev bootstrap
# Inspect the tool layer:  docker run --rm boss-engine:dev tools
# Serve MCP over stdio:    docker run -i --rm -e DATABASE_URL=... boss-engine:dev serve-mcp
# Serve MCP over HTTP:     docker run --rm -p 8791:8791 -e DATABASE_URL=... \
#                             boss-engine:dev serve-mcp --http --host 0.0.0.0
#                             (HTTP is read-only by default; no auth on /mcp)
# Ask the agent:           docker run --rm -e DATABASE_URL=... boss-engine:dev \
#                             ask "how many tasks are running?"
FROM python:3.12-slim

# Mirror the prod image layout: the package lives at /app/engine/boss_engine so
# Settings.repo_root resolves to /app, where db/ + packages/ + docs/ are copied.
WORKDIR /app

# System deps for psycopg / cryptography wheels are already in the slim image's
# manylinux wheels; add build-essential only if a source build is needed.
COPY engine/requirements.txt engine/
RUN pip install --no-cache-dir -r engine/requirements.txt

# The container artifact is batteries-included: ship the [mcp-http] extra so
# `serve-mcp --http` works out of the box (the pip package keeps it optional;
# pins match apps/api/requirements.txt).
RUN pip install --no-cache-dir fastapi==0.115.12 "uvicorn[standard]==0.34.3"

# The engine package (self-contained post Phase 2 — imports only boss_engine.*)
COPY engine/boss_engine engine/boss_engine
COPY engine/pyproject.toml engine/README.md engine/LICENSE engine/

# Data the engine needs at runtime, resolved via Settings.repo_root (/app):
# schema for `bootstrap`/ensure_schema, the spec pack (roles/skills/workflows),
# and the traceability table the feature catalog seeds from.
COPY db/schema.sql db/schema.sql
COPY packages/specpack packages/specpack
COPY docs/product/TRACEABILITY.md docs/product/TRACEABILITY.md

ENV PYTHONPATH=/app/engine
ENV PYTHONUNBUFFERED=1

# `boss-engine <subcommand>`: bootstrap | tools | status | call | ask | chat | serve-mcp
ENTRYPOINT ["python", "-m", "boss_engine"]
CMD ["tools"]
