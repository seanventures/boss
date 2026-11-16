# BOSS Community 1.0.0-rc.10.16 candidate

RC10.16 is public `main` after `v1.0-rc10.15`. Ten slices landed.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

- Cycle time is the seconds from a code task's start to its merge, and from that start to a healthy deploy. The window is the moment the merge or the deploy finished. An empty window has no median.
- Stack deploys in a window are counted as live, failed, rollback, or still open. A row with a rollback parent is a rollback even when its status is healthy. No rate is returned.
- Model spend in a window splits into shipped, failed or retried, and still in progress. A subscription call is not money. A shipped call is not also counted as failed.
- The AI SRE page shows the noise report: raw signals, clustered incidents, and up to ten signatures. A raw count of zero does not show a reduction ratio. A report that did not load is not filled with zeros.
- The notification outbox keeps the message body. A time window and a text query narrow it. The list says when it is capped. The dedupe key is not returned.
- The activity log counts people. A custom window can follow a retention setting longer than 30 days. The default stays 30 days. A longer ask is cut, and the response says it was cut.
- The reconciliation card can import an invoice and save a seat price for Anthropic, OpenAI, or OpenRouter. It does not close a month.
- Error-budget hours left are for the last 24 hours. An unused budget says it is not being used. A spent budget is 0 hours. 99.5 stays the install default until a person stores an objective.
- Firing alerts that are silenced or held back are counted. Zero held back is not a claim that the system is healthy.
- Finished failures and decision approvals compare this window with the previous window of the same length. A previous count of zero has no percentage.
- A fresh schema applies. `field_acl_records` was missing its closing parenthesis, so the next table failed to parse. That close was already on main as pull request 146.

## Not in this tag

- A new SQL file is still applied as soon as it is present. Recording a migration approval would need a new table or a connection this plugin does not have. That slice was not built.
- No image tag, no live URL, and no release-gate JSON.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That assertion was already failing on `v1.0-rc10.14`. This tag does not change it.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Checks

- The version pins match `1.0.0-rc.10.16` (`engine/tests/test_distribution_contract.py` and the heartbeat version check).
- Each landed slice was retested on a disposable database, or with vitest for a pure function, before it was cherry-picked. Cycle time 5 passed. Deploy outcomes 1 passed. Shipped spend 3 passed. Noise report 4 passed. Notification outbox 5 passed. Activity window 3 passed. Invoice form 5 passed. Error-budget hours 4 passed. Silenced firing 5 passed. Window compare 2 passed.
- The noise card, the invoice form, the notification list, and the health card were not opened in a browser.
- This tag was not installed on a clean host, and the release gate was not re-run.
