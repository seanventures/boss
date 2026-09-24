FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/engine

WORKDIR /app

# The LOCAL execution substrate runs working stages as subprocesses in THIS container, so the
# worker needs the toolchains those stages shell out to: git (EphemeralSandbox clone/diff) and
# node/npm (the web verification target: npm run build / typecheck / lint). Node 20 via NodeSource.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    `# VIDEO EDITING IS A BOSS CAPABILITY, so its tool ships with the worker rather than` \
    `# being something an operator has to install. ~80 MB, and without it every video step` \
    `# refuses with "ffmpeg is not installed in this worker image" — see video_edit.py.` \
    `#` \
    `# libavfilter-extra + a font bring drawtext, which needs libfreetype and is NOT in the` \
    `# base filter set. Plain ffmpeg here meant video.caption died with "No such filter:` \
    `# 'drawtext'" only on reaching the filter — after the render had spent its minutes.` \
    `#` \
    `# THE UNNUMBERED METAPACKAGE, deliberately. libavfilter-extra9 is bookworm and this base` \
    `# image is trixie, where the runtime package is libavfilter-extra10 — pinning either number` \
    `# breaks the build outright on the next Debian bump. The metapackage follows.` \
    ffmpeg libavfilter-extra fonts-dejavu-core \
         openssh-client \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN addgroup --system boss && adduser --system --ingroup boss boss

COPY apps/api/requirements.txt /tmp/api-requirements.txt
COPY apps/worker/requirements.txt /tmp/worker-requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/api-requirements.txt -r /tmp/worker-requirements.txt

COPY apps ./apps
COPY api ./api
COPY db ./db
COPY packages ./packages
COPY scripts ./scripts
# The OSS engine package (boss_engine) — the worker imports engine services
# (via the apps.api.app shims) so it must be on PYTHONPATH (/app:/app/engine).
COPY engine ./engine

RUN mkdir -p /var/lib/boss/artifacts \
    && chown -R boss:boss /app /var/lib/boss

USER boss

CMD ["python", "-m", "apps.worker.boss_worker.main"]
