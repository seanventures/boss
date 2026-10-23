# BOSS's database image: PostgreSQL 16 on Alpine with pgvector and Apache AGE compiled in
# (BOSSAPP-622; operator decision 2026-10-22).
#
# The base is the image installs ran before (postgres:16 on Alpine, musl), so an existing data
# directory opens unchanged: the upgrade is an image swap, and the schema step creates the
# extensions (CREATE EXTENSION IF NOT EXISTS vector, age). No dump and restore.
#
# Pinned: PostgreSQL 16.12 on Alpine 3.23, pgvector 0.8.1, Apache AGE 1.6.0 (the newest AGE
# release for PostgreSQL 16). Both extensions are built here from their release sources and only
# their installed files are copied into the final image, so it stays the size of the base plus
# a few megabytes. pgvector is built without -march=native (OPTFLAGS=""), so the image runs on
# any x86-64 or arm64 host, not only the one that built it.
ARG PG_IMAGE=postgres:16.12-alpine3.23

FROM ${PG_IMAGE} AS build
ARG PGVECTOR_VERSION=0.8.1
ARG AGE_VERSION=1.6.0
RUN apk add --no-cache --virtual .build build-base bison flex perl curl ca-certificates \
    && mkdir -p /out \
    && curl -fsSL -o /tmp/pgvector.tar.gz \
        "https://github.com/pgvector/pgvector/archive/refs/tags/v${PGVECTOR_VERSION}.tar.gz" \
    && tar -xzf /tmp/pgvector.tar.gz -C /tmp \
    && make -C "/tmp/pgvector-${PGVECTOR_VERSION}" OPTFLAGS="" with_llvm=no \
    && make -C "/tmp/pgvector-${PGVECTOR_VERSION}" OPTFLAGS="" with_llvm=no install DESTDIR=/out \
    && curl -fsSL -o /tmp/age.tar.gz \
        "https://archive.apache.org/dist/age/PG16/${AGE_VERSION}/apache-age-${AGE_VERSION}-src.tar.gz" \
    && tar -xzf /tmp/age.tar.gz -C /tmp \
    && make -C "/tmp/apache-age-${AGE_VERSION}" with_llvm=no \
    && make -C "/tmp/apache-age-${AGE_VERSION}" with_llvm=no install DESTDIR=/out

FROM ${PG_IMAGE}
ARG PGVECTOR_VERSION=0.8.1
ARG AGE_VERSION=1.6.0
LABEL org.opencontainers.image.title="boss-community-db" \
      org.boss.pgvector="${PGVECTOR_VERSION}" org.boss.age="${AGE_VERSION}"
COPY --from=build /out/usr/local/ /usr/local/
# AGE is loaded at server start. Compose also passes -c shared_preload_libraries=age, which
# covers a data directory whose postgresql.conf predates this image.
RUN printf "\nshared_preload_libraries = 'age'\n" >> /usr/local/share/postgresql/postgresql.conf.sample
