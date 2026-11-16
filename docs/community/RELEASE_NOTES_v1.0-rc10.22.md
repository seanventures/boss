# BOSS Community 1.0.0-rc.10.22 candidate

RC10.22 is public `main` after `v1.0-rc10.21`. Ten slices landed.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- A plan card shows the full title when that title is different from the short label. The label stays. A title that matches the label adds no second line.
- The staging tile says the health reason when one is present. The health word stays. The p95 source sentence stays.
- A security review step says its error when one is present. The step name and the step word stay.
- A stopped mission says its error. "Failed, step N" stays. The flight row prints that error because it prints the same words.
- An org agent says "Configured:" plus the configured model when one is set. The list of models it called stays.
- A savings row says the action, using the same 14-word shortening as the title. "What to do" stays as the label.
- A preview whose Restart is off says why. The button stays.
- A workforce card says the autonomy level next to the configured trust score.
- A workflow definition shows its description when one is present. The name, key, status, and version count stay. A list longer than 8 still says how many are not on the page.
- A deploy card says "Shared by N projects." when the host is shared by more than one project. The "runs on" link stays.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.22` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests before the cherry-pick: plan title 5 passed, staging health reason 4 passed, review step error 2 passed, stopped mission 3 passed, configured model 3 passed, savings action 3 passed, preview restart 2 passed, autonomy level 5 passed, registry description 2 passed, shared host 3 passed.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
