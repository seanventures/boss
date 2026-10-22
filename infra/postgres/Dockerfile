# Opt-in knowledge-graph database image. Debian, so text order matches glibc.
# pgvector comes from the Debian package.
#
# Apache AGE 1.6.0 is the newest stable release published for PostgreSQL 16
# (Apache download page, checked 2026-10-21). AGE 1.7.0 is published for
# PostgreSQL 18. AGE 1.8.0 is not published for PostgreSQL 16.
#
# Do not point compose at this image. The default images stay
# postgres:16-alpine and postgres:16. The server must start with
# shared_preload_libraries=age, which this file appends to the initdb sample.
FROM postgres:16-bookworm AS age-build

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        bison \
        ca-certificates \
        curl \
        flex \
        libreadline-dev \
        postgresql-server-dev-16 \
        zlib1g-dev \
    && curl -fsSL -o /tmp/age.tar.gz \
        https://dlcdn.apache.org/age/PG16/1.6.0/apache-age-1.6.0-src.tar.gz \
    && tar -xzf /tmp/age.tar.gz -C /tmp \
    && make -C /tmp/apache-age-1.6.0 -j"$(nproc)" \
        PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config install \
    && mkdir -p /opt/age/lib /opt/age/extension \
    && cp /usr/lib/postgresql/16/lib/age.so /opt/age/lib/age.so \
    && cp /usr/share/postgresql/16/extension/age.control /opt/age/extension/age.control \
    && cp /usr/share/postgresql/16/extension/age--*.sql /opt/age/extension/

FROM postgres:16-bookworm

COPY --from=age-build /opt/age/lib/age.so /usr/lib/postgresql/16/lib/age.so
COPY --from=age-build /opt/age/extension/ /usr/share/postgresql/16/extension/

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/* \
    && printf "\nshared_preload_libraries = 'age'\n" >> /usr/share/postgresql/postgresql.conf.sample
