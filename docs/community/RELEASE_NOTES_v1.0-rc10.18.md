# BOSS Community 1.0.0-rc.10.18 candidate

RC10.18 is public `main` after `v1.0-rc10.17`. Ten slices landed.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- Each environment on the health view carries `latency_objective_ms` and `latency_words` from the stored latency objective. A missing row is null and the sentence is "No latency objective is stored." The availability budget is unchanged.
- Each report window now includes that window's daily chart and agent list. An absent `since` still keeps the 14-day chart and every role. The reports page still draws the flat series, so the chart title stays "Activity, last 14 days" and the scorecard still lists every role. The typed client window does not yet expose those new fields.
- The work inbox keeps `person` when the inbox API already returned one, and shows it. An absent person adds nothing. Targets are not assigned.
- The deployments tab shows Promote only when that environment is staging and its deploy attempt is already a registry run whose status is succeeded, deployed, or completed. The button posts that run id through the existing promote action. A stack deployment id is not used.
- The factory Tests row shows one failed test name when the verify tail already contains exactly one. The tail stays folded. No rerun command is added. More than one name, or none, shows nothing.
- A This-week deploy point keeps its three-word label and adds `sha7`, the first seven characters of the stored commit, when that commit is present. The week strip shows it beside the label.
- Activity export sets `X-Window-Capped` to 1 only when retention cut the asked window, else 0. `X-Truncated` is still the 50,000-row cap.
- Tool-call export sets `X-Truncated` to 1 only when a further matching row exists past the cap. A file that fills the cap exactly is not truncated. The console download forwards that header.
- The agent scorecard still draws 12 rows. When the payload has more, it says how many are on the page and how many are not.
- The preview log drawer says "The text is the preview process log, not its requests or jobs." That sentence is next to the process text whenever a preview id is open. No log store was added.

## Not in this tag

- A failed deploy phase cannot be retried by itself. The existing Retry reruns the whole workflow deploy step. That button was not added.
- A new SQL file is still applied as soon as it is present. Recording a migration approval would need a new table or a connection this plugin does not have. That slice was not built.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing on `v1.0-rc10.14`. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.18` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests before the cherry-pick: latency view 1 passed, report window 3 passed, inbox person 3 passed, promote button 3 passed, failed test name 8 passed, week commit 1 passed and its strip test 2 passed, export window header 1 passed, tool-call export 2 passed, agent scorecard 2 passed, preview log sentence 1 passed.
- The health card, reports page, inbox, deployments tab, factory tests row, week strip, and preview drawer were not opened in a browser.
- This tag was not installed on a clean host.
