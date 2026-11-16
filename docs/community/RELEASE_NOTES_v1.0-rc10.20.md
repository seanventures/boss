# BOSS Community 1.0.0-rc.10.20 candidate

RC10.20 is public `main` after `v1.0-rc10.19`. Ten slices landed. Origin already held pull requests 148 and 149, and this tag includes that merge.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- A collapsed workflow card says paused when any of its schedules is paused. The calendar icon stays. A card whose schedules are all enabled does not say paused. The last-run status stays.
- Infra Find matches the approver's actor and role key. An empty query still shows every row.
- A deploy row shows who approved it as words next to the face. A role with no person shows the role. A raw user or agent id is not printed. No approver stays empty.
- Project deploy history shows the by name when the payload has one, including when an agent face is also shown. A blank by adds no name.
- The agent scorecard says when the last violation was, when that time is present. A null time adds no words. The 12-row cap is unchanged.
- The workflow registry still shows 8 workflows, 8 runs, and 10 timeline events. A longer list says how many are on the page and how many are not.
- A health environment says "From the app's requests." or "From health probes." for p95. Any other source adds no sentence. The millisecond number stays.
- The project staging tile uses those same two sentences.
- A notify policy's quiet-hours chip shows the timezone when one is set. An empty timezone keeps the hours only.
- A command-center app card still draws five builder faces. More builders show "+N". The accessible name lists every builder.

## Not in this tag

- A failed deploy phase cannot be retried by itself. That button was not added.
- A new SQL file is still applied as soon as it is present. A migration approval was not added.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.
- Pull requests 148 and 149 were already on origin. They were not retested for this tag.

## Checks

- The version pins match `1.0.0-rc.10.20` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Captain retests before the cherry-pick: paused card 2 passed, deploy find 4 passed, deploy row name 5 passed, deploy history 4 passed, scorecard violation 2 passed, workflow registry 6 passed, health p95 source 3 passed, staging tile p95 source 4 passed, quiet-hours timezone 2 passed, builder count 2 passed.
- Those screens were not opened in a browser.
- This tag was not installed on a clean host.
