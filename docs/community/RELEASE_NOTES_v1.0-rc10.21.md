# BOSS Community 1.0.0-rc.10.21 candidate

RC10.21 is public `main` after `v1.0-rc10.20`. Ten slices landed.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- A schedule row says why it did not start. A stored last error becomes "Last run failed:" plus that error. A missing start with no error says "Not started last time." A schedule that started adds no sentence. The longer missing list stays in the title.
- The dead-letter list still shows five failures. A longer list says how many are on the page and how many are not.
- A deploy card says "From the app's requests." or "From health probes." for p95. Any other source adds no sentence. The millisecond number stays.
- The environment drawer uses those same two sentences.
- A stage card uses those same two sentences beside the p95 number.
- The project health chip shows the p95 number already in its title, when that string is present. The chip does not add a source sentence.
- The identity overview still shows 12 agents. A longer list says how many are on the page and how many are not.
- The pulse still shows five agents. A longer list says how many are on the page and how many are not.
- A recent run says "Stopped:" plus the blocked reason when one is present. A blank reason adds no sentence.
- The safety trace still shows 40 events. A longer list says how many are on the page and how many are not.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.21` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests before the cherry-pick: schedule row 6 passed, dead-letter list 4 passed, deploy card p95 source 4 passed, environment drawer p95 source 4 passed, stage card p95 source 4 passed, health chip p95 3 passed, identity overview 5 passed, pulse 5 passed, recent run stopped 3 passed, safety trace 3 passed.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
