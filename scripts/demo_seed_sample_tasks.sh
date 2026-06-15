#!/usr/bin/env bash
# Demo seeder: creates sanitized B2B SaaS sample CEO-command tasks via the
# control-plane API. Safe to re-run (each command carries a fresh dedupe key).
# Usage: scripts/demo_seed_sample_tasks.sh
set -euo pipefail

BASE="${BOSS_CONTROL_PLANE_URL:-http://localhost:8000}"
PROJECT="${BOSS_DEMO_PROJECT_ID:-boss-dogfood}"
ACTOR="${BOSS_DEMO_ACTOR:-ceo}"

# Fictional company: "Northwind" — a B2B SaaS startup. All data is sanitized/test.
# Each line: command|max_cost_usd|deadline
TASKS=(
  "Add a public customer-facing status page wired to our uptime monitor and document the on-call runbook.|3|This week"
  "Draft the 4-email onboarding sequence for new trial signups and define the success metric.|2|Next week"
  "Investigate our cloud spend and propose a plan to cut the monthly bill by 15% without hurting reliability.|3|This sprint"
  "Prepare the SOC 2 access-control evidence checklist for the upcoming security review.|2|End of month"
  "Plan SSO and SAML support for enterprise customers, including a phased rollout.|4|This quarter"
)

echo "Seeding ${#TASKS[@]} sample tasks into project '${PROJECT}' as actor '${ACTOR}' via ${BASE}"
echo

i=0
for row in "${TASKS[@]}"; do
  i=$((i+1))
  cmd="${row%%|*}"; rest="${row#*|}"
  maxcost="${rest%%|*}"; deadline="${rest##*|}"

  resp="$(curl -s -X POST "${BASE}/v1/commands/propose" \
    -H 'Content-Type: application/json' \
    -d "$(printf '{"project_id":"%s","actor":"%s","command":"%s","constraints":{"max_cost_usd":%s,"deadline":"%s"}}' \
        "$PROJECT" "$ACTOR" "$cmd" "$maxcost" "$deadline")")"

  task_id="$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("task_id",""))' 2>/dev/null || true)"
  job_id="$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("job_id",""))' 2>/dev/null || true)"

  if [ -n "$task_id" ]; then
    printf "  %d. %-70.70s\n      task=%s job=%s\n" "$i" "$cmd" "$task_id" "$job_id"
  else
    printf "  %d. FAILED: %s\n      response: %s\n" "$i" "$cmd" "$resp"
  fi
done
echo
echo "Done. The worker will auto-plan these into pending decisions within ~90s."
