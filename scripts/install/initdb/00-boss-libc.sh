#!/bin/sh
# Record which C library initialised this data directory. musl and glibc sort text
# differently, so text indexes built under one are wrong under the other; the BOSS DB
# image refuses a directory without the glibc marker (boss-db-entrypoint.sh).
if ldd --version 2>&1 | grep -qi musl; then libc=musl; else libc=glibc; fi
printf '%s\n' "$libc" > "$PGDATA/boss-libc"
