# BOSS Sandbox Host agent — runs as a sidecar on the Docker host (goldeneye).
# It receives task specs from the control plane (POST /v1/sandbox/run-task) and
# launches single-tenant GUEST containers via the host Docker socket
# (ContainerBackend), runs the coding pipeline inside them, and returns
# metadata + diff only. Mount /var/run/docker.sock into this container so it can
# launch sibling guest containers. The guest image (boss-sandbox-guest) must be
# present in the registry so the host can pull/run it.
FROM python:3.13-slim

# Docker CLI (talks to the mounted host socket) + git/ca-certs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pydantic

WORKDIR /opt/boss/sandbox-host
COPY sandbox-host/sandbox_agent.py ./sandbox_agent.py

# BOSS_SANDBOX_IMAGE (the guest image), BOSS_SANDBOX_MEM/CPUS, BOSS_SANDBOX_STATE
# are provided by the deploy env. Listens on :8088 for the control plane.
ENV BOSS_SANDBOX_STATE=/var/lib/boss-sandbox/state.json
EXPOSE 8088
CMD ["uvicorn", "sandbox_agent:app", "--host", "0.0.0.0", "--port", "8088"]
