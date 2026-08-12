# Public site UX audit — boss.sean.network

**Date:** 2026-08-12  
**Surface:** https://boss.sean.network (marketing)  
**Product console:** https://boss.local.sean.network (Mission Control — separate)  
**Promised workflow:** GTM path B — engine first (CLI + MCP), simple governed loop, company OS as vision not day-one claim (`docs/marketing/GTM_DECK_REVISION_BRIEF.md`)

---

## Before (live `cc0aa48`)

| Check | Finding |
|---|---|
| Lead narrative | “Build the company / vibe companying” — full OS first |
| Developer entry | **No** MCP, CLI, Docker install CTA |
| Simple loop | 5 steps existed but framed as org-building (Define/Staff/Execute) |
| Community tier | Claimed “Basic Mission Control” + “Local and open models” (GTM G1/G2 gaps) |
| CTAs | Early access + Cloud beta only |
| vs GTM | **Misaligned** — deck says “not five dashboards; lead with engine” |

---

## After (live `9268c69`)

| Check | Finding |
|---|---|
| Hero | “Governed AI work. Not another chat window.” |
| Path | “Engine first. Operating system next.” |
| Loop | Outcome → Plan → Work → Evidence → Decide |
| CTAs | See the simple loop · Self-host the engine · Cloud beta |
| Nav | Simple Loop, Software Factory, Mission Control, Engine, Cloud, Enterprise |
| Community table | CLI+MCP, BYOK, budgets/receipts, self-host console — honest packaging |
| FAQ | Start path + autonomy honesty |

**Status:** Marketing UX now matches promised **simplified workflow** and **engine-first** packaging.  
**Not claimed fixed:** full Mission Control simplification on `boss.local` (still dense console — product Phase A/B).

---

## Residual product console notes (`boss.local`)

- Org gate + dense nav still present — not the simplified single-loop founder home.
- Align MC empty states / primary CTA to the same Outcome→Decision loop after redeploy.

---

## Deploy

- Source: `/Volumes/DataSSD1/boss-website` commit `9268c69`
- Image: `boss-website:9268c69` on goldeneye
