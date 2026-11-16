# BOSS Community 1.0.0-rc.10.17 candidate

RC10.17 is public `main` after `v1.0-rc10.16`. Nine slices landed. One slice was not built.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- The console error page links the page digest to the Activity log. The sentence says that id is the page digest, not a stored activity row. The same id is still in the container log.
- The first-week card links the Activity log and Govern spend.
- The Mission Dashboard one-hour figure is labeled new reservations, not settled receipts. The number is still the reservation sum. Settled receipts are unchanged.
- The Activity rail lists people the log already counted. The query bar suggests `actor:`.
- Recent sends has a find field. Typed text is sent as `q`. An empty field omits `q`. The page still shows 15 rows. That slice is not the server cap.
- The deploy drawer names the approver already on the row. When the row has no person, it says the approval was not recorded.
- A change-set lane with a build id opens that build's story. A lane with no build id stays plain text.
- The build story marks lines newer than the last time that build was opened. The note is "New since you opened this build." The first open marks nothing.
- A latency objective can be stored as milliseconds under `latency_objective`. Nothing is invented when the row is missing. The health card shows the sentence only when the payload includes the field. The health view does not send that field yet.

## Already on main, not written in this cut

- Pull request 145 sized each agent session's spend ceiling to a session, classed billing before the ceiling, and stopped a build at its allowance. That work was already on origin/main at `5308e16f` before these nine slices. This cut fast-forwarded it. It was not re-tested here.

## Not in this tag

- A failed deploy phase cannot be retried by itself. The existing Retry reruns the whole workflow deploy step. No server route retries one of prepare, upload, environment, switch, install, health, or smoke. That button was not added.
- A new SQL file is still applied as soon as it is present. Recording a migration approval would need a new table or a connection this plugin does not have. That slice was not built.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing on `v1.0-rc10.14`. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.17` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests before the cherry-pick: error page 1 passed, help card 9 passed, run-rate label 1 passed, activity people 12 passed, recent-sends find 1 passed, deploy approver 9 passed, lane link 13 passed, story stamp 4 passed, latency objective 1 passed and its card test 4 passed.
- The noise card, invoice form, notification list, health card, error page, help card, dashboard, activity rail, deploy drawer, change-set lane, and build story were not opened in a browser.
- This tag was not installed on a clean host.
