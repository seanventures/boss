FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

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

RUN mkdir -p /var/lib/boss/artifacts \
    && chown -R boss:boss /app /var/lib/boss

USER boss

CMD ["python", "-m", "apps.worker.boss_worker.main"]
