# BOSS Community 1.0.0-rc.10.24 candidate

RC10.24 is public `main` after `v1.0-rc10.23`. Ten slices landed. Pull request 154, acceptance evidence hardening, was already on main and is included. This captain did not retest that pull request.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- The stop pill says why agents are stopped, beside Stopped. The pill still says Stopped or On. A running pill adds nothing.
- The merge button says why the merge is held. The button label stays. A merge that is not held adds nothing.
- A plugin row says why it is blocked. The word Blocked stays.
- An extension row says why it is blocked. The word Blocked stays. The checksum stays off the row.
- A fix card says its confidence reasons beside the confidence level. The level stays.
- A recent finished run says the first line of its error beside the run name. Succeeded and blank errors add nothing.
- A ticket says why it was flagged, beside the existing flag sentence.
- A failed call row says its error beside ok or the failure mark. A successful call adds nothing.
- An Ask BOSS action says why beside its label. The label stays.
- A takeover row says the question, or the reason when the question is the title. The title stays.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it and the test was not re-run.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.24` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests on this tree, one suite at a time: stop pill 6 passed, merge button 6 passed, plugin row 9 passed, extension row 5 passed, confidence reasons 4 passed, recent finished run 7 passed, ticket flag 4 passed, call row 3 passed, ask action 5 passed, takeover row 6 passed.
- A fresh schema apply of this tree ran 2040 of 2040 statements twice, exit 0. The merge-marker test passed once. Python was 3.11.15. `tsc --noEmit` reported 23 errors, the same test files that already failed on rc10.23, and none in these ten slices.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
