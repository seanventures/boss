# BOSS Community 1.0.0-rc.10.19 candidate

RC10.19 is public `main` after `v1.0-rc10.18`. Ten slices landed.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- The reports page draws the selected window's daily series and titles it Today, Last 7 days, or Last 30 days. A window with no daily series keeps the flat chart and the title "Activity, last 14 days". The scorecard uses the window's agent list when that list is present. Spend stays the settled figure. Included subscription spend is shown beside it and is not added in.
- An activity download says "The retention window cut this export" when `X-Window-Capped` is 1, and names the line count when `X-Row-Count` is present. `X-Truncated` is still the 50,000-row cap.
- The dashboard says the cursor is the event count and the time of the newest event. Ack backlog counts inbound rows that are not terminal. The canary hint names passed, failed, and skipped. The numbers are unchanged.
- A workflow card shows the last run's status next to its age. No success rate is computed.
- Recent sends still show 15 rows. When the response is longer, the page says how many of those returned rows are on the page and how many are not. A failed or dead send shows its error word.
- The activity people list says "Showing 40 people." when it has 40 rows. A shorter list says nothing about a cap.
- A finding's path links to that lane's existing pull request. The text stays `path:line`. No pull request URL stays plain text. No comment composer was added.
- A tool-call export says "The cap hid older rows from this export." when `X-Truncated` is 1. A short file does not mention a cap.
- The app drawer's Logs control still opens the Activity log. Its title says that log is not this app's requests.
- A change-set lane shows the first seven characters of its head commit when that commit is present, and shows additions and deletions when those numbers are present.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.19` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests before the cherry-pick: report page 14 passed, activity export window 6 passed, dashboard words 3 passed, workflow status 2 passed, recent sends 7 passed, people cap 2 passed, finding link 9 passed, tool-call export 3 passed, app Logs title 1 passed, lane sha 3 passed.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
