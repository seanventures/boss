# BOSS Community 1.0.0-rc.10.15 candidate

RC10.15 is public `main` after `v1.0-rc10.14`. Nine slices landed. One slice stopped because it needed a new table.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- Easy Mode checks the blurb and the prompt before it reserves a budget. A card number or a national id blocks the launch. An email or a phone number asks, and nothing is reserved until that ask is allowed. A normal prompt still reserves once. The mode key stays `lucky`.
- Report tables use the same today, 7-day, and 30-day windows as the headline counts. A finished failure is `failed_final` or `deadlettered`. `failed_retryable` is still scheduled. `cancelled` is not a failure.
- An identity audit export sets `truncated` when the row cap filled the bundle. That bundle is not a proof the range was whole. Fewer rows than the cap means the cap did not cut the range.
- Open work for one project is grouped by the person on the row. An `agent:` owner, or a project role, is not a person. A row with no person is the group `none`.
- One project's `app_logs` can be searched by a text fragment and a time window. Those rows are BOSS process lines, not a customer's deployed app. An empty match is an empty list.
- Closing a calendar month records who closed it. Closing it again returns the same row. Spend dated in that month is refused and writes one receipt. Spend in an open month is reserved, then receipted.
- An invite can grant one project role. Accepting it again does not add a second binding. It does not grant an org role.
- Replaying a saved failure copies that run's inputs into a new run, with one budget reservation and a new receipt. The same click does not reserve again. A run that did not fail is left alone. The builder is not started.
- The availability objective is the percent a person stores on the project. Until that row exists, environments are held to 99.5 and the card says that number is the install default. A person who sets 99.5 is not told it is still the default.

## Not in this tag

- A new SQL file is still applied as soon as it is present. Recording a migration approval would need a new table or a connection this plugin does not have. That slice was not built.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing on `v1.0-rc10.14`. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.15` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Each landed slice was retested on a disposable database before it was cherry-picked. Logs 6 passed. By-person 1 passed. Month close 1 passed. Invite 1 passed. Audit export 3 passed. Replay 1 passed. Reports 1 passed. Easy Mode launch gate 7 passed. Availability objective, with the existing health checks, 15 passed.
- This tag was not installed on a clean host, and the release gate was not re-run.
