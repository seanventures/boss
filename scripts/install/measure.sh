#!/usr/bin/env bash
# What a BOSS install actually costs this machine — for the sizing table in the docs.
#
# Measured rather than estimated, because the number that was in the README ("~5GB free disk")
# was wrong by an order of magnitude and would strand an install half-way through its build.
#
# Run it twice: once with BOSS idle, once during a build. Idle tells you what the box must
# always have; during a build tells you what it must have spare.
set -uo pipefail

B=$'\e[1m'; N=$'\e[0m'
printf "\n${B}BOSS footprint on this machine${N}  (%s)\n\n" "$(date -u +%FT%TZ)"

printf "${B}memory${N}\n"
free -m 2>/dev/null | awk 'NR==2{printf "  total %sMB   used %sMB   available %sMB\n", $2, $3, $7}' \
  || vm_stat >/dev/null 2>&1 && echo "  (macOS: use Activity Monitor; /proc is not available)"
printf "  swap: %s\n" "$(free -m 2>/dev/null | awk 'NR==3{print $3"MB used of "$2"MB"}' || echo 'n/a')"

printf "\n${B}per container${N}\n"
docker stats --no-stream --format '  {{.Name}}|{{.MemUsage}}|{{.CPUPerc}}' 2>/dev/null \
  | grep -E 'boss-community' | column -t -s '|' || echo "  (no boss containers running)"

total=$(docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' 2>/dev/null \
  | grep -E 'boss-community' | awk '{print $2}' | sed 's/MiB//;s/GiB/*1024/' \
  | awk '{s+=($0 ~ /\*/ ? $0 : $0)} END {printf "%.0f", s}' 2>/dev/null)
[ -n "${total:-}" ] && printf "\n  ${B}BOSS total: ~%s MiB${N}\n" "$total"

printf "\n${B}cpu${N}\n"
printf "  cores: %s   load: %s\n" "$(nproc 2>/dev/null || sysctl -n hw.ncpu)" \
  "$(uptime | sed 's/.*load average[s]*: //')"

printf "\n${B}disk${N}\n"
df -h / | tail -1 | awk '{printf "  root: %s free of %s (%s used)\n", $4, $2, $5}'
docker system df 2>/dev/null | awk 'NR==1 || /Images|Containers|Volumes|Build/ {print "  "$0}'

printf "\n"
