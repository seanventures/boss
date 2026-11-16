# BOSS Community 1.0.0-rc.10.26 candidate

RC10.26 is public `main` after `v1.0-rc10.25`. Ten slices landed. Pull requests 155, 156, and 158 were already on main before this cut. This captain did not retest them. Their files do not overlap these ten slices. Pull requests 151 and 154 were already on main inside earlier tags and were not retested for this tag.

One prepared slice was not shipped. A linked change set already shows its blocked reason as the paragraph `mr-set-reason`, so a second sentence would have repeated it.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- The last test says its summary, beside the status word. The status stays. Profile checks still name the check failures, and the skills row stays a count.
- The workforce says why the control plane is unreachable, beside Could not reach the control plane.
- The send sheet says why that channel, beside the channel line. The channel words stay.
- The experiment says why that verdict, beside the verdict label.
- The pack says why there is no dashboard, beside No dashboard link yet. The foot is a div so that sentence can be a paragraph. The pack counts stay.
- The old deploy line says why it failed, beside the status and the time. The status words stay.
- The govern switch says why install is refused, beside Can't install here. The Off switch why stays.
- A refused fix says the detail, beside the refusal words, when the detail is not the same as the refusal.
- The problem note says the message, beside the headline, when the message is not already the headline.
- The paused rule says why, beside the list switch. The editor chip still keeps that reason on its tip.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it and the test was not re-run.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.
- Pull requests 155, 156, and 158 were already on main. This captain did not retest them. Pull requests 151 and 154 were already on main inside earlier tags and were not retested.

## Checks

- The version pins match `1.0.0-rc.10.26` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests on this tree, one suite at a time: last test 4 passed, workforce reach 5 passed, send channel 5 passed, experiment verdict 4 passed, monitoring pack why 6 passed, old deploy line 6 passed, install refusal 4 passed, refused fix 5 passed, problem note 5 passed, paused rule 4 passed. The existing profile-checks suite was updated for the last-test summary and passed 4. The existing monitoring-pack suite passed 4. The events console suite passed 29.
- A fresh schema apply of this tree ran 2040 of 2040 statements twice, exit 0. The merge-marker test passed once. Python was 3.11.15. `tsc --noEmit` reported 23 errors, the same test files that already failed on rc10.25, and none in these ten slices.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
