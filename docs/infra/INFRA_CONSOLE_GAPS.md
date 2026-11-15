# Infra Agent console (BOSSAPP-1145 · ST-11 1156): built screens against the wireframes S1–S5

How this was checked. The screens were walked by clicking in Chrome against one `next dev` server
(port 3200, this branch) pointed at the laptop's API on `127.0.0.1:8000`, with the storage routes
answered from the typed sample data (`?infra_demo=1|asking|resolved|quiet`, `lib/infra-storage-demo.ts`;
the sample is the wire shape of `docs/infra/INFRA_AGENT_API.md`, parsed by the same code the live API
goes through, and every screen says "Sample data" while it is on). The backend branch
(`feat/infra-agent-backend`, PR #111) was not run: the laptop's API has no storage router yet, so the
screens were not walked against live incidents. Screenshots are in `docs/infra/walk/infra-console/`,
named `s<n>-<screen>-<width>.jpg`.

**Widths.** The automation browser's window ignored every resize (the same limit the ticket walk hit),
so the real viewport was 1138 CSS px (a 1024 px window at the browser's 90 % zoom). 1440 and 1024 were
walked at that viewport with the page zoomed (`body { zoom }`) so the content column measured 1440 and
1024 CSS px; 768 and 390 were checked the same way with the rail docked to icons. Every new surface
answers to **container queries** (the width it is given, as `cc.css` does), not media queries, so the
zoomed passes exercise the same rules a narrow window would: at 768 the card's grid is one column, the
strip's chips fold into its sub line, and at 390 the strip's two buttons go full width, the deletion
card is one column, the host rows lose their bar and spark. Screenshots at 390 are partial (the zoomed
page scrolls in device pixels); a pass in a resizable browser on the real install is the follow-up.

| Screen | Status | Notes |
|---|---|---|
| S1 Top strip (`app/infra/infra-strip-banner.tsx`) | Matches | Mounted by `ConsoleShell` above its notice slot, so it is on every area (walked on Command Center and Govern). `role="alert" aria-live="assertive"`, red, a 2 s pulse on the dot and the ground (0.5 Hz) that `prefers-reduced-motion` stops; dot · Infra Agent face · "Storage critical on *host* · Infra Agent is cleaning" · "9 % free · 14 runs paused at a checkpoint · nothing is lost" · chips freed / ETA / step n of m · **Watch in Command Center** · **Acknowledge**. Acknowledge is the only control; it posts `…/acknowledge`, the strip shrinks to a `role="status"` line ("Storage critical · Infra Agent cleaning · acknowledged by …") and stays until `resolved`. The press is remembered per tab until the API's `acknowledged_by` arrives. Differs: under ~1240 px of strip (a 1440 laptop with the rail open) the three chips fold into the sub line so the buttons stay on the first row; the wireframe draws them as chips at 1440 with a narrower rail. Without `system:operate` the button is not drawn (contract §1). |
| S2 Command Center card (`app/infra/infra-critical-card.tsx`) | Matches | Above the Now cards while an incident is open. Face · "Infra Agent · critical work in progress" · Critical chip · cause sentence · big "GB freed · ETA"; the flow is the API's `flow` nodes drawn by `InfraFlow` (Detected → Paused → Cleaning → Retention → Freed → Resumed, state and time per node, one sentence for a screen reader); the bar to target; "Cleaning, safest first" with done/active/waiting/ask/skipped marks and GB per step; Paused work and Receipts mini cards (dry-run first · labelled only · $ spent); Details behind a `<details>`. The tier 3 step's **Review** opens the S4 card in place. The status line says "Infra Agent is cleaning" (between budget_stopped and host_critical) and `motionOf` stops the page's own motion. Differs: the wireframe's lower row (Needs you / Work / Spend / Hosts) is the Command Center's existing cards; the card itself carries no host row. The "helper unreachable" chip is new (contract `helper.reachable`). |
| S3 Storage, Govern › Infrastructure (`app/infra/storage-card.tsx`) | Matches | A Govern tab under More (`?area=budget&tab=infrastructure`, `?host=` picks the host; Infra › Fleet's host drawer links here). Host switcher · **Clean now** (autonomy:manage) · the notice line (amber under 20 %, red under 15 %, the 10 % floor names "builds and upgrades wait"; the pending tier 3 decision is linked) · four tiles Free now / Growth / Full in / Last cleanup (→ the report) · Used by type (SVG stacked bar, striped free part, Docker VM unreclaimed chip) · Trend and forecast (30 d used, dashed "if nothing changed", dashed green "with retention rules", the "full" line; floor scaled so the month reads as a slope) · threshold chips + Resume at · retention rules with words per kind and an On/Off toggle (PUT rules) · All hosts rows with bar, % free, days chip, spark. Differs: thresholds are not edited here — "Change thresholds" links to Govern › Advanced limits (`focus=storage_thresholds`), a row that does not exist yet; "was 1.8 days this morning" is "> 90 days with the rules" because the summary carries `with_rules_days_until_full`, not a before value. |
| S4 Approval card (`app/infra/storage-delete-decision.tsx`) | Matches | Drawn for decision kind `storage_delete` from its `recommendation` (Approvals: the inbox row; Needs you and the Command Center card: one read of `GET /v1/decisions/{id}`). "Infra Agent asks" · Waiting n min · Not reversible · the sentence (count, kind, GB, what stays from the typed args) · Why (the incident's cause) · Already done without asking · the two lists (`to_delete` with CSV, `kept`) · **Make this a permanent rule** (form-kit CheckRow, pre-ticked from `rule_proposal.preticked`) · **Delete N · free X GB** (red, decision:approve, step-up by apiWrite) · the agent's alternatives ("Keep 20 instead", `args`) · **Not now** · "Logged with your name · passkey step-up"; side: While you decide (paused runs, bar to target), What it will run (tool + typed args + dry-run count), Never automatic chips. Approve sends `plan_hash`/`content_hash`, `make_rule`, `args`. Differs: the wireframe's item dates and "9.6 GB kept" are not in the recommendation (names only); "brings free space to 51 %" is "frees 161 GB" for the same reason. The Approvals list itself could not be walked with a real `storage_delete` row (no API); the inline card was walked from the Command Center step's Review, and Not now was pressed (sample write, toast). |
| S5 Incident report (`app/infra/storage-incident-report.tsx`) | Matches | `?area=budget&tab=infrastructure&incident=<id>` (from the strip's card, the Command Center card's Details, Storage › Last cleanup). Breadcrumb · "Storage incident · host" · date and span · Resolved chip · **Export** (Markdown from the API's `report.md`, the console's own words as the fallback; JSON) · the flow with times (`report.timeline`, the incident's `flow` while open) · the summary sentence · Freed / Work paused / Lost or repeated = 0 / Cost tiles · Cause with chips · Learned (Rule added, Proposed with Review → the decision) · What was removed (type, detail, by rule / automatic / approved by a person, GB; "9 receipts" → the Log scoped to the incident) · Paused work rows (waited, resumed, All N runs). Differs: "approved by Alice" is "approved by a person" (the report's `by` carries no name); Observability › Incidents does not list storage incidents yet (the report is reached from the three places above). |
| "Paused by Infra Agent" chip (`components/stuck-work.tsx`) | Unit-tested, not walked | `WhyLine` draws a wait reason whose code starts with `infra_hold` as one chip with the Infra Agent's face, on every surface a run shows why it waits (Agents at work, Project › Work, the build page, Needs you). No run on the laptop carried `last_error.code = "infra_hold"`, so it was not seen live. |
| Live updates | Wired, not walked | SSE kinds `storage.*` and `infra.hold.*` invalidate every area ("*"), the Command Center, Govern and the work screens, and restart the client polls through the console's change signal; the polls (10 s incident, 60 s summary) carry ETags. |

## Gaps driven to zero during the walk

- The strip wrapped its buttons onto a second row at every laptop width → the chips fold into the sub line under ~1240 px of strip; one row.
- The card's flow lost the node times → times shown under each node (card and report).
- "Full in 2 days" / "1.8 days" rounding and "was … before the cleanup" nonsense while the incident was open → `daysWord` keeps one decimal under 10 days; the sub line reads from the forecast's with-rules figure.
- The host `<select>` rendered taller than Clean now → sized with the button.
- The trend's lines sat in the top fifth of the chart (y from 0) → the floor is a little under the lowest reading, "full" stays the top line.
- Acknowledge was forgotten on an area change (the shell re-mounts the strip) → kept per tab until the API answers.
- Inline `style={{width}}` bars and `<p>` broke `tests/infra-and-observability-are-tokens-only` → every bar is an SVG rect in a 0–100 box; no paragraphs.
- The receipts link was an Activity-log URL built by hand → `logHref`.
- `Date.now()` and `toLocaleDateString` in render → `useNow` and `lib/datetime`.
- Write bodies carried a literal `actor` → the session's person only; the writes are registered in `lib/identity/write-permissions.ts`.
- "Keep 20 instead" was hard-coded → the agent's `alternatives` from the recommendation.

## Follow-ups

- Walk every screen against `feat/infra-agent-backend` on port 8100 with a scratch DB and a real incident (the strip's `acknowledged_by`, the Approvals row for `storage_delete`, the `infra_hold` chip on a paused build, SSE kicks).
- A pass at 390 in a resizable browser on the real install (the shell's own rail behaviour at phone width is outside these components).
- Govern › Advanced limits needs the `storage_thresholds` row that "Change thresholds" points at (thresholds are `PUT /v1/infra/storage/rules` with `thresholds`).
- Observability › Incidents could list storage incidents (`GET /v1/infra/storage/incidents`) and open S5.
