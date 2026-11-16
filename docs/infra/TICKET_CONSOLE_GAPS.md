# Ticket console (BOSSAPP-1047) and Okta SSO (BOSSAPP-1048): the screens, walked for real

## The walk of 2026-11-14 (replaces the stubbed 1440-only pass below)

How it was checked. The API (`python -m apps.api.serve`, port 8100) and the console (`next dev`,
port 3100) ran from this branch against their own database (`boss_console_jo`: created, schema
applied by the API's own `ensure_schema`, then `bootstrap.seed()`), claimed by a test owner who
signs in with a password and an authenticator code. Jira was the operator's real Jira Cloud site
(`paramg-1794632206014.atlassian.net`, project KAN), connected with the token from the Keychain
item `boss-jira-test`, read in-process by a script and never printed. Three tickets were made in
KAN for the walk (epic KAN-6 with stories KAN-7 and KAN-8; KAN-8 labelled `boss`).

* 1440: every control clicked by hand in Chrome — connect, Test, Settings, mapping, write-back
  chips, the pick-up rule, Advanced, New work from KAN-7, Start (refused for no repo/deploy
  target, then started with Start anyway through the API), the work drawer, Import from Jira
  (epic + children), Ready to start, Project settings, Account › Ticket accounts, Ask BOSS
  ("Implement KAN-8"), Identity › Sign-in & SSO › Add provider › Okta › Test.
* 1440, 1024, 768, 390: the same screens in headless Chromium at real viewport widths
  (the desktop Chrome window is full-screen and ignores resizing): 16 views × 4 widths, each with a
  check that nothing runs past the right edge. 0 overflows after the fixes.

What the real run proved end to end: Test signs in as the token's owner and lists KAN and SAM1;
the mapping picks from Jira's own projects; KAN-8 (label `boss`) waits in "Waiting for BOSS";
New work shows KAN-7's card; starting KAN-7 moved it to **In Progress** in Jira and commented
"BOSS started work on this ticket (run …)" — both listed under "Written to Jira" in the drawer;
Import took KAN-6 with its stories; Ask BOSS read KAN-8 with `ticket_get` and offered Import + Start.

### Gaps found on the real walk, and what was done

| # | Screen | Gap (real data) | Fix |
|---|---|---|---|
| 1 | T1 | The ⋯ menu opened under the last row and was clipped (`overflow: hidden` on the table): only "Test" peeked out | Table no longer clips; the header keeps its rounded corners |
| 2 | T1 | At 1024 and 768 the table ran off the card: New tickets, Status and ⋯ unreachable | The table follows its own width (container query): below 860px it is stacked cards, as on a phone |
| 3 | T1 | Test said only "signed in as …"; the guide promises "your name and the projects you can see" | "Jira works · signed in as Paramaguru G · sees 2 projects: KAN, SAM1." |
| 4 | T2 Settings | **No Client ID / Client secret anywhere** — Part 2 of the setup guide had nowhere to paste them (only host env vars) | A "Connect with Atlassian" block in the drawer: Client ID, Client secret (write-only, vault), the **Callback URL** with Copy, and Connect enabled only once an app is set (with the reason in words). API: PATCH `oauth_app` + `oauth_app` on every connection |
| 5 | T2 Settings | The stored token showed its last four characters | "Stored in BOSS's vault" — no part of a token on screen |
| 6 | T2 | "Replace api token" (label lower-cased); "Where to get one" was a hover-only span | "Replace API token"; the help line is visible text |
| 7 | T2 | Pick-up rule read "ask me first / start automatically" | "Ask me first / Start automatically" (the guide's words) |
| 8 | T2 | A mapping could only target "This project" | The BOSS project is a picker of the org's projects (another project needs Manage integrations there; the API already checked) |
| 9 | T2 | Tiles repeated their name: "GitHub Issues / GitHub Issues", "Plane / Plane" | The second line says what it is for when the lead repeats the name |
| 10 | T3 | The card showed no acceptance criteria or comments: `/v1/tickets/resolve` answered a preview | Resolve answers the full read (criteria, comments, attachments); the card shows "Acceptance criteria · 2" |
| 11 | T3, T5, T9 | A start the project was not ready for (no repo, no deploy target) printed the API's long sentence with no way forward | The shared checklist: New work shows its readiness panel; Waiting / Ready to start show `StartRefusal` (the one drawing every start door uses) with Start anyway |
| 12 | T3 | "Browse tickets" where the guide says "Browse Jira" | "Browse Jira" when the project has exactly one ticket system |
| 13 | T5/T6 | The row chip still said "Jira: To Do" after BOSS moved the ticket to In Progress | A transition BOSS wrote is stored on the binding (`external_state`) |
| 14 | T6 | "06:07 PM" broke over two lines in "Written to Jira" | The time column is as wide as its text |
| 15 | T9 | Cards showed the list's short name ("BOSS walk") for every ticket of the epic | Cards show the ticket's full title |
| 16 | T9 | The "imported" banner was larger than the type scale | `--fs-ui` |
| 17 | Project settings | The Ticket system card was missing from the section nav | "Ticket system" in the nav |
| 18 | all | In-app links said "Setup › Ticket systems"; the rail calls it **Setup** | Links and tips say "Setup › Ticket systems" |
| 19 | Account | "Connect with Atlassian" with no app said "needs an OAuth app (client id and secret)" | Says where: "add the app's Client ID and Client secret in Setup › Ticket systems › Jira › Settings" |
| 20 | Okta | **Test appeared only after Add provider** | A **Test** on the unsaved form: `POST /v1/identity/providers/test`, a dry run (discovery + signing keys for OIDC; https URL + a certificate that parses for SAML). Needs only the issuer; never sends or keeps a secret; stores nothing |
| 21 | Okta | A mistyped Okta org (404 on discovery) said "Test failed · check the settings" | "No sign-in settings at that address · check the issuer URL" (`discovery_failed`, `jwks_failed` named) |
| 22 | Okta | The first copy row said "Redirect URL"; Okta's field is "Sign-in redirect URIs" | "Sign-in redirect URI" for Okta |
| 23 | Okta | Provider tiles were bare words | An icon per kind |

### Seen, not fixed here

* New work's "Helm reads" brief does not use the resolved ticket: with only "KAN-8" typed it says
  "no linked issue" while the ticket card above it shows the ticket. The start itself uses the
  ticket (it is read server-side), so nothing wrong is built; the brief's words are wrong.
* Ask BOSS shows the T4 ticket brief only on a live answer whose agent called `ticket_get`. On
  this walk the first "Implement KAN-8" did call it (and offered Import + Start), but its live view
  was lost to a dev-server reload; two later turns answered from the run and from `ticket_import`
  instead, so the card did not draw. A conversation re-opened from the list never shows it: the
  history keeps no tool record. The card itself is unit-tested (`tickets-run-brief-…`).
* New work from a ticket shows two workflows at once: the ticket card's "Minor feature · picked
  from Story" (what Start uses) and the "Picked for you" tile's "Standard Product Delivery".
* Skills & MCP › Email still shows a stored SMTP password as "Set, …last4"
  (`tests/connection-section.test.tsx` fails on main for an unrelated reason).

## The first pass (stubbed API, 1440 only), kept for the record

| Screen | Status | Notes |
|---|---|---|
| T1 Setup › Ticket systems | Matches | A row on the Setup board (`?area=setup&focus=ticket_systems`), not a separate page with a breadcrumb. Columns: system, projects → BOSS project, writes back, new tickets, status (with paused bindings). Each connection's ⋯ menu has Test, Settings and Disconnect (Disconnect asks first). "Where tickets show up" has four links, and there is a vault note with "How adapters work". Skills & MCP › File tickets now points here; its Kit form is kept, folded under "Connect from here instead". |
| T2 Add a ticket system | Matches | Provider tiles come from `/catalog`, plus a "+ Your adapter" info tile that links to TICKET_CONNECTORS.md. Fields come from the catalog. The token is write-only and shows "Set, …last4" after saving. "Test sign-in" stores nothing unless the test passes, then shows the result. Also in the drawer: mappings, write-back chips, the label rule (ask me first / start automatically) and Advanced (status names, webhook/poll/off, poll minutes, webhook create/rotate shown once with Copy). Differs: "Connect with Atlassian" is enabled only after the bot sign-in is saved, because the API needs a connection id to start OAuth. A mapping can only target this project from here; another project maps from its own Setup. |
| T3 New work from a ticket | Matches | A key or link in the words resolves through `/v1/tickets/resolve` and shows the card: type, priority, status, criteria, "Also read", the workflow picked from the type, and "Updates KEY ✓". Start runs import + start-from-ticket as one press. Differs: the hint link reads "Browse tickets", not "Browse Jira", because the modal does not load connections. There is no per-ticket estimate; the modal's existing estimate chip still applies. |
| T4 Ask BOSS ticket brief | Partly checked | Built and unit-tested (`app/tickets/ticket-brief.tsx`): heading, workflow chips, write-back chips, one Start and the outside-input note. A turn records the `ticket_get` call but not its result, so the card re-reads the ticket from the call's ref. The v2 chat has no tool record, so there the card resolves a key found in the reply. No screenshot: getting one needs a live agent turn. |
| T5 Work tab: waiting and chips | Matches | "Waiting for BOSS · Jira LEND, OPS, label “boss” · N" has Start or "Plan first" per ticket and "Change the rule". Ticket chips on rows come from the work list's own `ticket` field (one query in the API, no read per row). Differs: when there are several systems the Import button reads "Import from a ticket system", and it sits on its own line under the New work bar. |
| T6 Work drawer · Linked ticket | Matches | Has the status steps (Started, Pull request, Merged, Deployed), the "ticket changed" banner (Include it / Next ticket answer the decision), and "Written to Jira" with failed lines in red plus Retry. The work item comes from the run's `input.work_item_id`. The decision inbox is read through its POST twin `/load`. Fixed during the walk: the run lookup now sends `project_id` (the API answers 422 without it), and the drawer's own list styles no longer squash the steps. |
| T7 Architecture | Not a screen | Wireframe only. |
| T8 Import from Jira | Matches | Has the breadcrumb, a connection picker, search, the 3-step strip, a filterable projects list (unmapped projects say so), the epic tree with children and "Not in an epic", and Open only / type filters. Ticking an epic takes its children. In-BOSS rows are disabled with "In BOSS ↗". The summary maps epic → Major, story → Minor, bug → Bug fix, then one "Import to Work". |
| T9 Imported, pick what to start | Matches | Drawn from the work list: the API returns imported tickets not started yet as rows with `origin: "ticket_import"`, `ticket`, `ticket_type` and `parent_work_item_id`. The epic card lists its tickets and has "Start major feature"; single tickets have a workflow dropdown and Start. No browser storage; a re-import adds no row. |
| PR / change page badge | Not walked | `TicketSyncBadge` on the change-set page, driven by the run, is unit-tested. The build cockpit (`GET /v1/code-tasks/{id}/cockpit`) now carries `ticket` too and shows the same chip (`fx-ticket`). |
| Project settings › Ticket system | Not walked | The default ticket project per connection, saved as mappings. A mirror is read-only. Links to Setup. Unit-tested. The settings tab sent the dev session to /login, so it was not captured. |
| Account › Ticket accounts | Not walked | Connect with Atlassian, Disconnect, and on an email mismatch: "It's mine" (`POST /v1/me/ticket-identities/{connection_id}/confirm`, kept on the person's own sign-in for that account and audited; `confirmed_by_person` becomes true while `email_matches` stays false) / "Not mine, disconnect". Unit-tested. Not captured: /account needs a real session cookie, and the network-trusted dev console has none. |

## Follow-ups

- A walk at 390 wide in a resizable browser (on the real install).
