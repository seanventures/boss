# Four-slice swarm — 2026-08-15

Live tag `b6d8ccc`. Not GA. Never print secrets. No `--env-template`.

| Ticket | Slice |
|---|---|
| BOSS-380 `91557761-b6c4-4b39-9264-7de5d5575e69` | SecretRef consumption |
| BOSS-381 `79f85c21-40e7-45ff-8751-c9cedf41c2a1` | Official QA /reports v1.1 |
| BOSS-351 `c1f3b9c6-923e-4b6e-b211-939934f908c5` | Sealed factory E2E |
| BOSS-369 `a8cf92b4-77ab-4298-be98-1ed79a1180a2` | BYOK real paste / bind |

## SecretRef (fail-closed: SecretRef first, env only as last-resort if no ref bound)

1. `credential_resolve` — DONE `engine/boss_engine/services/credential_resolve.py` + tests
2. `openai-secretref` — DONE `openai_adapter.py` (connection+project_id → resolve_project_credential)
3. `factory-secretref` — DONE `_guest_engine_fields` + `make_code_agent` bind lane keys
4. `github-secretref` — DONE `_resolve_secret` uses `resolve_secret_ref` when connection+id bound
5. `plane-secretref` — DONE `plane_client.py` token via SecretRef if `plane_api_secret_ref_id` or `sref_*` pointer
6. `secretref-tests` — DONE credential/openai/github/guest + plane client tests

## Official QA

7. `qa-report-honesty` — ensure /report never invents UI/UX; attach missing evidence helpers
8. `qa-official-runs` — script/docs to POST official BugBox/HookWatch/ApproveIt runs; live only if API + budget allow; never invent scores

## Sealed E2E

9. `sealed-live` — if factory allow-create is on live, start one narrow governed `code.task` with `open_pr=True` and prove receipt+hold decision. Else document blocker honestly.
10. `sealed-tests` — extend sealed contract tests if gaps remain (`sealed_e2e_contract.py` / gate only)

## BYOK

11. `byok-bind` — write-through existing live env model key to local_vault SecretRef + bind provider account. Never print the key. Use easy-connect internally.
12. `byok-tests` — extend `test_provider_easy_connect.py` infra+model if needed
