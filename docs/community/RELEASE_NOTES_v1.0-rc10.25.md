# BOSS Community 1.0.0-rc.10.25 candidate

RC10.25 is public `main` after `v1.0-rc10.24`. Ten slices landed. No other pull request landed on main between that tag and this cut.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- The deploy tag says why a deploy that is not live is in that state, beside the state word. A live deploy adds nothing.
- The deploy chip says why, beside Deploying, Deploy waiting for approval, or Last deploy failed. The chip words stay.
- Profile checks say what failed, beside Passed or Pending. The checks title stays.
- A failure note says what to do, beside the headline, when that step is not already the headline. The headline, the Fix it link, and Retry stay.
- The database line says why Postgres was chosen, beside Postgres for this build. The title stays.
- A fix card says the rejected guess beside First guess rejected. The confidence reasons stay as they were.
- A linked ticket says its stored sync error beside the status tag. The status words stay. The last-written clock stays in the title.
- A paused webhook says its last error beside Paused: renew authority. The pause line stays.
- The promote sheet says why evidence is short, beside Not enough evidence.
- A rejected option says why, beside the option. Not chosen stays a title when the option has no why.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it and the test was not re-run.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.
- Pull request 154 was already on main inside `v1.0-rc10.24`. This captain did not retest it for this tag.

## Checks

- The version pins match `1.0.0-rc.10.25` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests on this tree, one suite at a time: deploy tag 4 passed, deploy chip 4 passed, profile checks 4 passed, failure note 5 passed, database line 4 passed, rejected guess 4 passed, linked ticket 6 passed, paused webhook 6 passed, promote sheet 4 passed, rejected option 5 passed.
- A fresh schema apply of this tree ran 2040 of 2040 statements twice, exit 0. The merge-marker test passed once. Python was 3.11.15. `tsc --noEmit` reported 23 errors, the same test files that already failed on rc10.24, and none in these ten slices. One database-line test needed a named component before render so the optional import was not used as JSX.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
