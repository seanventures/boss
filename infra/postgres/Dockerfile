# BOSS knowledge-graph database image. Debian, so text order matches glibc.
# pgvector comes from the Debian package. Apache AGE is not installed by this
# file: the PG16 1.8.x tag was not built or timed here. Do not point compose
# at this image until that install and the populated upgrade have been run.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*
