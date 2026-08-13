# Community GA — 14-day `qa-live` calendar

**Start:** 2026-08-13 UTC  
**Host:** goldeneye `/var/lib/boss/qa-live/`  
**Cron:** `15 6 * * * /usr/local/bin/boss-qa-live-daily.sh`  
**Gate:** `scripts/qa/live_dogfood_gate.py` (10 required + Metatron)

| Day | Date (UTC) | Result | Notes |
|-----|------------|--------|-------|
| 1 | 2026-08-13 | **PASS 10/10** | First on `3e6c906`; same-day re-run on `cd13067` also 10/10 + Metatron (does not add a day) |
| 2–14 | — | pending | Daily 06:15 UTC |

**Close Community GA (G2.1)** only when this table has **14 consecutive PASS** rows with logs under `/var/lib/boss/qa-live/`.

Do not backfill. Missed day = restart count.
