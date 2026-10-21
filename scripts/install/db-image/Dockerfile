# BOSS database image: PostgreSQL 16 on Debian (glibc) with pgvector and Apache AGE.
# Build context: scripts/install (docker build -f scripts/install/db-image/Dockerfile scripts/install).
# docs/product/KNOWLEDGE_GRAPH_SPEC.md §3.1. Built by install.sh like every other BOSS image.
#
# Extensions are copied from their projects' own Debian images, built against the same
# PostgreSQL major on the same Debian release, so this build needs no compiler and no apt.
# Bump a version by changing one ARG; keep the three bases on one Debian release (glibc ABI).
#
# AGE: the newest release for PostgreSQL 16 is 1.6.0. AGE 1.8.0 ships only for PG 15, 18
# and 19 (see the spike doc); moving to it is an operator decision.
ARG PG_BASE=postgres:16-trixie@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d
ARG PGVECTOR_IMAGE=pgvector/pgvector:0.8.1-pg16-trixie@sha256:1e4956185a7fd9306a41ee759a7b4329c4faf9a2bb91a1d01437310c97002433
ARG AGE_IMAGE=apache/age:release_PG16_1.6.0@sha256:16aa423d20a31aed36a3313244bf7aa00731325862f20ed584510e381f2feaed

FROM ${PGVECTOR_IMAGE} AS pgvector
FROM ${AGE_IMAGE} AS age

FROM ${PG_BASE}
COPY --from=pgvector /usr/lib/postgresql/16/lib/vector.so /usr/lib/postgresql/16/lib/
COPY --from=pgvector /usr/lib/postgresql/16/lib/bitcode/vector /usr/lib/postgresql/16/lib/bitcode/vector
COPY --from=pgvector /usr/lib/postgresql/16/lib/bitcode/vector.index.bc /usr/lib/postgresql/16/lib/bitcode/
COPY --from=pgvector /usr/share/postgresql/16/extension/vector* /usr/share/postgresql/16/extension/
COPY --from=age /usr/lib/postgresql/16/lib/age.so /usr/lib/postgresql/16/lib/
COPY --from=age /usr/share/postgresql/16/extension/age* /usr/share/postgresql/16/extension/
# The libc guard: initdb writes the marker (also mounted from scripts/install/initdb by
# Compose); the entrypoint refuses a data directory another libc initialised.
COPY initdb/00-boss-libc.sh /docker-entrypoint-initdb.d/
COPY db-image/boss-db-entrypoint.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/boss-db-entrypoint.sh && chmod 0644 /docker-entrypoint-initdb.d/00-boss-libc.sh
LABEL io.boss.db-libc="glibc" io.boss.db-extensions="vector,age"
ENTRYPOINT ["boss-db-entrypoint.sh"]
CMD ["postgres"]
