#!/bin/bash
# Build the console AND restart it. Never one without the other.
#
# `next start` serves the build that existed when it started, so `npm run build` alone changes
# nothing an operator can see. That gap produced the same bug report three times — "the button
# does nothing" — while the fix sat compiled on disk and unserved. Every one of those was me
# rebuilding and forgetting to restart.
#
# Usage: scripts/restart-console.sh [port]
set -euo pipefail
PORT="${1:-3001}"
cd "$(dirname "$0")/../apps/web"

echo "==> building"
npm run build 2>&1 | grep -E "Compiled|Type error|Failed to compile|✓" || true

# A failed build must NOT restart the server: serving the previous build is better than serving
# a half-written one, and silence here would look like success.
if [ ! -f .next/BUILD_ID ]; then
  echo "!! build produced no BUILD_ID — refusing to restart"; exit 1
fi
BUILD_ID=$(cat .next/BUILD_ID)

echo "==> restarting on :$PORT"
pkill -f "next start -p $PORT" 2>/dev/null || true
pkill -f "next-server" 2>/dev/null || true
sleep 2
# Detach so the server survives this script exiting. `setsid` was the obvious choice and does
# not exist on macOS — using it stopped the server starting AT ALL, which is a worse failure
# than the race it was meant to fix. A subshell + nohup + disown is portable.
( nohup npm exec -- next start -p "$PORT" > /tmp/boss-console-$PORT.log 2>&1 < /dev/null &
  disown 2>/dev/null || true )

# Wait for it to actually answer. Reporting "restarted" before the port is open is the same
# class of lie as reporting a deploy succeeded because the exit code was 0.
# `set -e` turns a failing `[ ... ] && { ... }` into a script exit, so the earlier version
# reported failure on the first not-yet-ready poll. Use an explicit if, which has no such
# interaction — a readiness check that lies about readiness is worse than none, because it
# trains you to ignore it.
for _ in $(seq 1 45); do
  code=$(curl -s -o /dev/null -m 3 -w "%{http_code}" "http://localhost:$PORT/" 2>/dev/null || echo 000)
  if [ "$code" = "200" ]; then
    echo "==> serving build $BUILD_ID on :$PORT"
    exit 0
  fi
  sleep 1
done
# Distinguish "not ready" from "I cannot see it from here". In some sandboxes a nested shell
# has no loopback access, so curl returns 000 while the server is serving perfectly. Reporting
# that as a failure is the same lie in the other direction — claiming broken when it works
# teaches you to ignore the check, exactly like claiming success when it does not.
if grep -q "Ready in" "/tmp/boss-console-$PORT.log" 2>/dev/null && pgrep -f next-server >/dev/null; then
  echo "==> serving build $BUILD_ID on :$PORT (process up; port unverifiable from this shell)"
  exit 0
fi
echo "!! console did not start — see /tmp/boss-console-$PORT.log"
tail -5 "/tmp/boss-console-$PORT.log" 2>/dev/null
exit 1
