# BOSS Community 1.0.0-rc.10.23 candidate

RC10.23 is public `main` after `v1.0-rc10.22`. Ten slices landed.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- The project header says the health reason beside the health chip. The chip still shows the health word and the p95. A blank reason adds nothing.
- A deploy card says the environment health reason beside the health tag. The health word stays. The p95 source sentence and the shared-host sentence stay.
- The cap preview names the stopped days from the replay. The "would have stopped N days" count stays. The names are the stored strings.
- The limits screen names those same paused days beside the existing count. The count is the number of those names.
- An agent profile says its description under the name. The name stays.
- The identity overview names each access role. The role list already showed the description. This is the overview row.
- A pipeline route shows its description when that description is different from the title. The title stays.
- A factory preview says its error beside Stopped or Failed. Those words stay.
- The waive sheet says why a check cannot be waived, when a reason is stored. A check that can be waived adds nothing.
- A start proposal says why beside the word Why.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.23` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests before the cherry-pick, one suite at a time: project header 4 passed, deploy card 4 passed, cap preview 5 passed, limits screen 6 passed, agent profile 3 passed, overview role 5 passed, pipeline route 5 passed, preview error 3 passed, waive reason 5 passed, start proposal 4 passed. The start-proposal test was run again on this tree after a type fix, and 4 passed.
- A fresh schema apply of this tree ran 2040 of 2040 statements twice, exit 0. The merge-marker test passed once. `tsc --noEmit` reported 23 errors, all in test files that already failed on rc10.22, and none in these ten slices. The first tsc run had one extra error in the start-proposal test. That was fixed before this commit.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
