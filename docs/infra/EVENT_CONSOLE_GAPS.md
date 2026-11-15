# Eventing console (P9a-7, BOSSAPP-1099): E1, E2, E4, E5, E6, E7 against the wireframes

## How it was checked

The API (`python -m apps.api.serve`, port 8100) and the console (`next dev`, port 3100) ran from
this branch against a scratch database (`boss_scratch_p9a7_<stamp>`: schema by `ensure_schema`,
`bootstrap.seed()`, then real intake data made through the engine's own front door and processor:
a Slack connection on a chat endpoint, a Grafana generic webhook, GitHub, three deployed agents,
four rules, one person linked by code and one by SSO email, events that ended routed / ignored /
filtered / refused / held / dead). The console ran network-trusted (`BOSS_CONSOLE_AUTH=network`,
unclaimed, so the bridge holds the Owner set). The scratch database was dropped afterwards.

Every screen was walked by clicking in Chrome at the laptop's own width (1512 CSS px):
Connect a source → Generic webhook → Connect (URL and secret shown once) → Done; a card's ⋯ →
Test; Connect Slack → Test, then a signed `@BOSS` sent to the front door turned **Saved** green
live; Rules → budget $5, quiet hours Wait → Save rule (checked in the API: `quiet_hours`,
`limits` and a `trigger` scoped budget written); Stream → chips, a dead event opened as a flow;
Dead letters → tick → Reprocess, tick → Force… → reason → Force run; People → Revoke → confirm.

**Not done: the 1440 / 1024 / 768 / 390 pass and the screenshots.** The headless pass
(`walk.py`: each screen at four widths, a check that nothing runs past the right edge, the
wireframes rendered beside) was stopped when the laptop's free disk fell to 7 GiB, under the
10 GiB floor for this work. The layout answers to container queries (`.ev` is the container),
so the narrow rules are the ones the vitest file exercises by structure only. Run it when there
is room: `python3 -I walk.py docs/infra/walk/events-console <wireframes dir>`.

| Screen | Status | Notes |
|---|---|---|
| E1 Connections (`app/events/connections.tsx`) | Matches | Tiles (events today, woke an agent, refused, dead letters; each opens the stream or dead letters) · one card per source: mark, where, Live / Paused / Sign-in failed / Rate limited chip, Digest mode chip, a sparkline of the last hour in 5-minute buckets, per-minute rate, last event, median lag, errors in the hour, N rules → Rules · ⋯ Test / Rotate secret (BOSS-signed sources) / Pause·Resume / Remove (confirmed inline) · dashed Coming cards for Teams, ServiceNow, Salesforce, Gmail/Outlook · the vault line. Connect a source: Slack → E2; GitHub and Generic webhook connect in a sheet built from the catalog's fields, the URL and a minted secret shown once. Differs: the wireframe's Jira / Plane card is not drawn (no intake adapter yet; tickets have their own row). |
| E2 Connect Slack (`app/events/connect-slack.tsx`) | Matches | Three steps; "Open manifest link" (Slack's create-from-manifest URL with scopes and events; the Request URL inside once the chat endpoint exists, Socket Mode drops it); signing secret and bot token write-only, sent once to the chat endpoint (`POST /v1/chat/endpoints`), then the intake connection riding on it (`chat_endpoint_id`); Socket Mode needs the `xapp-` token; Who answers = `default_dm_agent`; Test draws Signed → Saved → Replied, polling the stream, and says where it stopped (refused, no grant) with a link to the event. SSO attestation is drawn only with `identity_provider:manage` and PUTs `sso-attestation` after save. Differs: the wireframe's "BOSS posts in a channel you pick" — the API's test checks secrets and inbox; BOSS does not post a test message (follow-up). The channel id is asked for because the chat endpoint requires it. Attestation walked in vitest only (the bridge does not hold `identity_provider:manage`; the scratch org had no IdP). |
| E4 Rules (`app/events/rules.tsx`, `lib/intake-rules.ts`) | Matches | The connection's rules across agents (each agent's triggers, `kind=intake`), first match wins; ↑/↓ re-prioritise (agent:deploy); a switch per rule (agent:lifecycle). The editor is the sentence: When [a mention / a DM / any message / a follow-up] in [channels] → [agent], acting for [mode], and [field] [op] [value]…, follow-ups toggle, Edit as JSON (a filter the sentence cannot say stays as it is). Would match n of the last 50 is asked of `preview-match` as you type, drawn as dots plus three events (the API now returns each event's words and sender). Guards: acting for, limits (per thread/record per hour/day, storm per minute), daily budget (a `trigger` scoped budget, budget:manage) with an estimate from the agent's last 7 days, quiet hours (always answer / say "later" / wait, with times and zone), triage off by default (on: small model, $0.002/event, a daily cap). Differs: reorder is by arrows, not drag; the identity modes are the API's three (`owner_readonly` is not in the API). |
| E5 Stream (`app/events/stream.tsx`, `event-flow.tsx`) | Matches | Live (5 s re-read while visible) · Pause stream · status chips with the period's counts, each a filter · search, source, period · rows with time, source, face, words, place, status chip. The open event is a flow: Source → Checked → Saved → Rule → For whom → Run → Reply (Reply from the run's state), stopping red where it stopped (For whom? not linked → Link sent; Refused; Failed n tries); timing, screen verdict; Details: guards, identity, envelope, raw. Differs: no "Rule: any" filter (the events API has none); reply delivery is inferred from the run finishing, not from the outbox. |
| E6 Dead letters (`app/events/dead-letters.tsx`) | Matches | Select all / one · Reprocess N · Force… with a required reason, logged by the API · each row: source, words, why in words, rules, tries, first failed, open in the stream · "<connection>: nothing stuck" beside the stuck ones. |
| E7 People (`app/events/people.tsx`) | Matches | New API `GET /v1/intake/connections/{id}/people` (integration:read) and `POST …/people/{link}/revoke` (integration:manage): linked people with the BOSS person, method chip (Code / SSO email / Entra ID), when; not linked with when a link prompt went; guests (never auto-linked); revoked in the last 30 days. Attestation banner with Turn off for an identity-provider admin. Differs: "Send link prompts · 6" and "Resend" are not drawn — there is no API to push a prompt (follow-up). |

## Gaps found on the walk and fixed

- Connect Slack bounced to Connections on a fresh load (the first render had no `kind`) → read from the address.
- A held event (no grant yet) counted as an error on the card → only dead and real failures count.
- GitHub showed "lag 1377 min" (a PR's own update time) → lag ignores anything over ten minutes.
- The steps showed the list's own "1." "2." → no markers.
- Rule rows broke onto three lines → one row: arrows, rule, switch; the editor is the wider column.
- Save rule's result was off screen and the budget field emptied after saving → the result sits by the button; the budget is kept.
- A webhook event's place said `com.boss.webhook.received` → the connection's name.
- "Held: …" in red → faint, with the reason.
- People's middle column wandered and the Not linked chip stretched → fixed columns, chips at their size.
- Dead letters repeated "nothing stuck" per connection when nothing was stuck → said once.
- Test said "nothing to check" for Slack (its secrets live on the chat endpoint) → says what was checked, if anything.

## Follow-ups

- The four-width pass and screenshots (blocked on disk).
- A real "BOSS posts a test message" for E2, and link-prompt sends for E7, need API.
- A rule filter on the events API for E5.
