# BOSS Community 1.0.0-rc.10.27 candidate

RC10.27 is public `main` after `v1.0-rc10.26`. Ten slices landed. No other pull request landed on main during this cut. Pull requests 155, 156, and 158 were already on main before rc10.26. This captain did not retest them. Pull requests 151 and 154 were already on main inside earlier tags and were not retested for this tag.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- The preflight line says the detail, beside the label. The label stays. A blank detail uses the remedy. The same words as the label stay off the page.
- The learned row says the detail, beside the row. The label, the sub line, and used or new stay.
- The journey says the detail, after the button. The timeline on the same file stays title-only.
- The gateway test says the detail, beside Works or Failed. Those two words stay the result.
- The alert says what it is about, beside the label, when that label is not the rule button. Description wins, then summary. The alert line is a div so a long sentence can be a paragraph.
- The custom step says its summary, beside the label. The unusable reason stays where it was.
- The plugin says its description, beside the name. The blocked line stays.
- The starter says its description, beside an orphan profile and beside a template. The names stay.
- The backlog row says the detail, beside the label. The why line stays. The checklist still prints its own detail and was not given this sentence. The backlog label cell is a div so a long sentence can be a paragraph.
- The extension says its description, beside the name. The problem line and the tools title stay.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it and the test was not re-run.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.
- Pull requests 151, 154, 155, 156, and 158 were not retested.

## Checks

- The version pins match `1.0.0-rc.10.27` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests on this tree, one suite at a time: preflight 6 passed, learned 4 passed, journey 4 passed, gateway 4 passed, alert 4 passed, custom step 4 passed, plugin 6 passed, starter 5 passed, backlog 4 passed, extension 6 passed. The existing roadmap, onboarding, and projects suite passed 15 after the backlog cell became a div.
- A fresh schema apply of this tree ran 2040 of 2040 statements twice, exit 0. The first apply printed the known non-fatal orgs and workspaces column-reconcile note. The second apply did not. The merge-marker test passed once. Python was 3.11.15. `tsc --noEmit` reported 23 errors, the same seven test files that already failed on rc10.26, and none in these ten slices.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
