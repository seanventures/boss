# Standalone BOSS Engine image — ships the OSS engine with NO API or console.
#
# It runs the engine-native BOSS Agent and/or the MCP server against a Postgres
# the container can reach (DATABASE_URL). This is the "ship only the engine"
# artifact: nothing from apps/ is copied in.
#
# Build (from engine/):   docker build -t boss-engine:dev .
# Inspect the tool layer:  docker run --rm boss-engine:dev tools
# Serve MCP over stdio:    docker run -i --rm -e DATABASE_URL=... boss-engine:dev serve-mcp
# Ask the agent:           docker run --rm -e DATABASE_URL=... boss-engine:dev \
#                             ask "how many tasks are running?"
FROM python:3.12-slim

WORKDIR /engine

# System deps for psycopg / cryptography wheels are already in the slim image's
# manylinux wheels; add build-essential only if a source build is needed.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The engine package (self-contained post Phase 2 — imports only boss_engine.*)
COPY boss_engine ./boss_engine
COPY pyproject.toml README.md LICENSE ./

ENV PYTHONPATH=/engine
ENV PYTHONUNBUFFERED=1

# `boss-engine <subcommand>`: tools | status | call | ask | chat | serve-mcp
ENTRYPOINT ["python", "-m", "boss_engine"]
CMD ["tools"]
