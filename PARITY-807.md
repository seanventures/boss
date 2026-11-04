# BOSSAPP-807 parity: every Govern control, before and after

Govern now opens on **Spend** (Screen A). **Limits** (Screen B) is the second tab. Every place
that was a Govern tab is still one click away under **More**. Every old `?area=budget&tab=…` link
opens what it opened before, and every `?focus=<row>` link (Setup's Budget row, the Log) still
lands on that row of the control board.

| Old control (where it was) | Where it lives now |
|---|---|
| Govern landing = Limits board (`?area=budget`) | The landing is Spend. The board is **More › Advanced limits** (`?tab=board`) and **Limits › Advanced: pace, token guides, review rounds** |
| Meter (money of the binding cap, holds, ticks, burn, cap-in, other period, Spent / Outside caps / Included) | Hero on Spend (money of the cap, badge, bar + forecast, today/subscription/reset line). The full meter is unchanged above the board, Where the money went, Envelopes, Sharing and Self-fix |
| Legacy financial / provider hold notices | Unchanged under the meter, and also in the Spend hero (`LegacyHolds`, same testids) |
| Red "New work is waiting for budget · Raise monthly cap · Stopped ↗" banner | Unchanged, on every Govern screen. **Raise monthly cap** now opens Limits with the Per-month field focused (it used to open the board row) |
| Daily cap (board row, Pace-only) | Limits › **Per day** (now directly editable via `POST /v1/govern/limits`). The board row, its evidence and Pace still work |
| Monthly cap (board row editor + cap preview) | Limits › **Per month** (the same `set_budget` write, `via: govern`). The board editor and its 30-day preview are unchanged |
| Burst (board row, Pace-only) | Limits › **Burst per minute** (now directly editable). The board row is unchanged |
| Pace (slider, preview, Apply) | The **first card on Limits**, "How fast BOSS works". It uses the same `pace` and `pace_preview` ops. Moving the slider shows what Pace sets (month, day, burst, jobs, workers) and fills the caps below, which can still be fine-tuned. The single Save writes Pace first, then any cap changed from what Pace set. The "would have paused" line follows the new caps. Pace is also still on the board, unchanged |
| Thresholds: Heads-up / Cheaper builds / No new servers / Risky work stops | Board. Unchanged. Heads-up also drives the Spend badge's "Near the cap" |
| Token guides (project default, per step, edit) | Board. Unchanged |
| Review lanes, Turn budget, Review rounds, Captain leads, Captain self-improves, Jobs at once, Workers | Board. Unchanged |
| Cap preview ("Would have stopped N days") | Board editors (unchanged). Also on Limits as "With these limits, last month would have paused work on N days" (the same `cap_preview` replay, month + day) |
| Elsewhere links (Approval rules, Access, Model routing) | Board. Unchanged |
| Spend tab (window, total, daily chart, model spend link, by agent, by model, cache, scorecard, by step, savings, top drivers) | **Spend › Where the money went →** and **More › Where the money went** (`?tab=spend`, unchanged) |
| Envelopes tab (allocations, FinOps, reconcile, initiatives) | **More › Envelopes** (`?tab=envelopes`, unchanged). Per-project monthly limits show there as project envelopes |
| Sharing tab | **More › Sharing** (`?tab=sharing`, unchanged) |
| Self-fix tab | **More › Self-fix** (`?tab=selffix`, unchanged) |
| Header links Changes ↗ / Stopped ↗ | **More** menu (same testids, same Log links). Stopped ↗ also stays on the red banner |
| (new) What happens at the cap | Limits › **When a limit is reached**: pause (default, what every install did before), cheaper, stop. This is project setting `budget_at_limit` |
| (new) Per-project monthly limit | Limits › **Per project (optional)**: creates, updates or closes a project-scoped budget envelope |

## API: fields and routes are only added

- `GET /v1/govern` adds `at_limit {value, choices, default, source, version, changed_by}` and `meter.periods[].forecast_usd_micros`.
- `GET /v1/govern/spend` adds `window=month`, `by_project`, `by_model_all`, `metered_calls`, `included_calls`. `by_model` and every other field are unchanged, and still cover this project.
- New routes: `GET /v1/govern/project-limits`, `POST /v1/govern/limits`, `POST /v1/govern/project-limit`.
- Engine: new setting `budget_at_limit` (default `pause` = the old behaviour). A project limit is a `budget_allocations` row (`scope_type='project'`, scope_id = the project, one calendar month). "No limit" sets its status to `closed`. There is no migration and nothing is dropped or renamed.

## Old tests changed (behaviour change listed above)

These eight web tests opened the board as the Govern landing. They now open it at `?area=budget&tab=board`. That is a one-line URL change in each file, and no assertion changed:
`activity-govern-copy-budget`, `every-govern-form-saves-what-was-typed`, `govern-board-shows-effective-values`, `govern-loads-on-a-rail-switch`, `govern-spend-says-who-and-which-model`, `govern-writes-what-it-wrote-before`, `log-and-govern-recheck-items`, `log-govern-layout-fixture`.
The fake API helper `tests/_helpers/govern-fixtures.ts` gained a `project_limits` read. That is an addition only.

## One figure (after the 2026-11-04 walk)

- **Spent** is one definition everywhere, the ledger (`included_spend.ledger_spend`). It is what the Spend hero, Today, Week, By model (this project, adding up to the hero), By project, Limits "Used" and per-project "used", and the board meter's "Spent" line all show.
- **Held** means active reservations. A cap counts spent + held, so the board's meter amount and Setup's budget row show spent + held. The Spend hero shows "$X held" beside the spent figure, and the meter amount's tooltip says so.
- The Spend hero covers this project, because caps are per project. By project lists every project you can see. Both scopes are labelled on screen.
- API additions: `meter.periods[].spent_usd_micros` and `held_usd_micros`, `GET /v1/govern/spend?window=day`, and `by_model_spent`.
