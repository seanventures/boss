# Ticket console (BOSSAPP-1047 Phase C): built screens against the wireframes

How this was checked. Each screen was opened in one `next dev --webpack` server (port 3457) against
the local API at `127.0.0.1:8000`. A throwaway stub proxy in front of it answered the ticket routes
with NorthPeak/LEND fixtures, because the local install has no Jira. Screens were captured at 1440
wide and compared with the approved wireframes T1–T9. The 390 wide pass could not be captured: the
automation browser's window ignored the resize, and the console refuses to load in an iframe. Phone
layouts are covered by CSS breakpoints at 640px (tables become stacked cards, grids become one
column), but nobody has looked at them yet.

**Install caveat.** The running install is older than this branch, so it does not grant
`work_tracker:write` to its roles. Every Import and Start button therefore showed greyed with
"Needs “Work with tickets”". That is the permission mirror working as intended. On a current API,
an owner gets these buttons enabled.

| Screen | Status | Notes |
|---|---|---|
| T1 Setup › Ticket systems | Matches | A row on the Setup board (`?area=setup&focus=ticket_systems`), not a separate page with a breadcrumb. Columns: system, projects → BOSS project, writes back, new tickets, status (with paused bindings). Each connection's ⋯ menu has Test, Settings and Disconnect (Disconnect asks first). "Where tickets show up" has four links, and there is a vault note with "How adapters work". Skills & MCP › File tickets now points here; its Kit form is kept, folded under "Connect from here instead". |
| T2 Add a ticket system | Matches | Provider tiles come from `/catalog`, plus a "+ Your adapter" info tile that links to TICKET_CONNECTORS.md. Fields come from the catalog. The token is write-only and shows "Set, …last4" after saving. "Test sign-in" stores nothing unless the test passes, then shows the result. Also in the drawer: mappings, write-back chips, the label rule (ask me first / start automatically) and Advanced (status names, webhook/poll/off, poll minutes, webhook create/rotate shown once with Copy). Differs: "Connect with Atlassian" is enabled only after the bot sign-in is saved, because the API needs a connection id to start OAuth. A mapping can only target this project from here; another project maps from its own Setup. |
| T3 New work from a ticket | Matches | A key or link in the words resolves through `/v1/tickets/resolve` and shows the card: type, priority, status, criteria, "Also read", the workflow picked from the type, and "Updates KEY ✓". Start runs import + start-from-ticket as one press. Differs: the hint link reads "Browse tickets", not "Browse Jira", because the modal does not load connections. There is no per-ticket estimate; the modal's existing estimate chip still applies. |
| T4 Ask BOSS ticket brief | Partly checked | Built and unit-tested (`app/tickets/ticket-brief.tsx`): heading, workflow chips, write-back chips, one Start and the outside-input note. A turn records the `ticket_get` call but not its result, so the card re-reads the ticket from the call's ref. The v2 chat has no tool record, so there the card resolves a key found in the reply. No screenshot: getting one needs a live agent turn. |
| T5 Work tab: waiting and chips | Matches | "Waiting for BOSS · Jira LEND, OPS, label “boss” · N" has Start or "Plan first" per ticket and "Change the rule". Ticket chips on rows show sync words, read for at most 12 visible rows. Differs: when there are several systems the Import button reads "Import from a ticket system", and it sits on its own line under the New work bar. |
| T6 Work drawer · Linked ticket | Matches | Has the status steps (Started, Pull request, Merged, Deployed), the "ticket changed" banner (Include it / Next ticket answer the decision), and "Written to Jira" with failed lines in red plus Retry. The work item comes from the run's `input.work_item_id`. The decision inbox is read through its POST twin `/load`. Fixed during the walk: the run lookup now sends `project_id` (the API answers 422 without it), and the drawer's own list styles no longer squash the steps. |
| T7 Architecture | Not a screen | Wireframe only. |
| T8 Import from Jira | Matches | Has the breadcrumb, a connection picker, search, the 3-step strip, a filterable projects list (unmapped projects say so), the epic tree with children and "Not in an epic", and Open only / type filters. Ticking an epic takes its children. In-BOSS rows are disabled with "In BOSS ↗". The summary maps epic → Major, story → Minor, bug → Bug fix, then one "Import to Work". |
| T9 Imported, pick what to start | Matches | "✓ Imported 1 epic and 2 tickets from Jira LEND": the epic card lists its tickets and has "Start major feature"; single tickets have a workflow dropdown and Start. Differs: the cards are kept in sessionStorage, so they appear only in the browser session that did the import. The work list API does not yet mark items that are Ready because they came from a ticket. |
| PR / change page badge | Not walked | `TicketSyncBadge` on the change-set page, driven by the run, is unit-tested. A code-task run (rather than a workflow run) shows no badge. |
| Project settings › Ticket system | Not walked | The default ticket project per connection, saved as mappings. A mirror is read-only. Links to Setup. Unit-tested. The settings tab sent the dev session to /login, so it was not captured. |
| Account › Ticket accounts | Not walked | Connect with Atlassian, Disconnect, and on an email mismatch: "It's mine" / "Not mine, disconnect". Unit-tested. Not captured: /account needs a real session cookie, and the network-trusted dev console has none. Gap: no API records "this account is mine", so "It's mine" is remembered only in this browser. |

## Follow-ups

- A route to confirm a mismatched ticket account (`POST /v1/me/ticket-identities/{id}/confirm`).
- Mark ticket-imported Ready work items in the work list, so T9 survives a reload.
- The badge for code-task runs on the change page.
- A walk at 390 wide in a resizable browser.
