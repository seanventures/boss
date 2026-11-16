# BOSS Community 1.0.0-rc.10.14 candidate

RC10.14 is the public `main` after `v1.0-rc10.13`. It keeps that candidate's tool-call controls and local change review, and adds the build launch, the gap controls, and the leftover branch work that was still only on side branches.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

### Build

- Easy Mode launches a project from one description. The button says Easy Mode. The mode key stays `lucky`. GitHub and Drive say they are not connected until a person connects them. The template catalog is on this candidate. A template does not build until launch (BOSSAPP-1273, BOSSAPP-1274, BOSSAPP-1276).
- The console can open on the build composer. An agent or bot is created in a wizard. A workflow is built from the library (BOSSAPP-1278, BOSSAPP-1279).
- A running app can sit beside the chat. A preview element can be marked. A visitor snapshot is separate from the live preview. A named draft stays off git sync. An in-progress app can be shared by a named pass. A finished task can be applied from an isolated copy (BOSSAPP-1241 through BOSSAPP-1253).
- A brief records the required stack for each component. A PRD that swaps that stack asks before the build continues. Add project stops when the build image lacks the stack (BOSSAPP-1207, BOSSAPP-1208, BOSSAPP-1161).
- A merged pull request is shown as merged. A retry of that same work does not open a second pull request (BOSSAPP-933).
- Wiki pages, comments, holds, and watches show a person's name (BOSSAPP-1040).

### Controls and records

- Builder prompts and the live publish commit are audited. A member can have a monthly build cap. Elapsed work and settled spend are metered on a turn. Security audit rows can stream to a SIEM URL. A local gate can ask or block personal data before a builder reserve. That gate is not called from Easy Mode launch (BOSSAPP-1246 through BOSSAPP-1250).
- Prompts can be saved and promoted only through an approved decision. One prompt can be compared across models, with a receipt per column. Recorded CVEs and the SBOM are on one Security screen. An admin can guard publish, severity, export, and chat. A built app can pin where it runs, keep a design system, set field permissions, freeze files and tables, and refuse traffic off an IP list (BOSSAPP-1254 through BOSSAPP-1271).
- One person has one primary agent in a company. Connection and default model can be set on many agents. Codex and Claude Code traces can be exported to a company endpoint. A submitted voice transcript can be filed on a task (BOSSAPP-1280, BOSSAPP-1281, BOSSAPP-1298, BOSSAPP-1299).
- A host must own the VM or the deployment it names. A host run whose payload names no repository takes git scope from its grant, then GitHub from the project's bindings (BOSSAPP-1177).
- Public-tree privacy scan hits that were names and example addresses were cleared (BOSSAPP-968).

## Checks

- The version pins match `1.0.0-rc.10.14` (`engine/tests/test_distribution_contract.py`).
- The identity matrix on this tip matches the live route table (`test_the_committed_matrix_is_current`, 1 passed). The older matrix rewrite was not applied.
- The four leftover slices were retested on disposable databases before they landed. Console: 39 pytest and 30 vitest. Stack: 29 pytest and 1 vitest. Tests: 96 pytest and 9 vitest. Host grant scope: the bench world had no person in the org, so the fixture creates one; the scope tests and the granted fetch then passed.
- BOSSAPP-1252's five tests passed when their database was listening. An earlier run skipped four because that database was down.
- This tag was not installed on a clean host, and the release gate was not re-run.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Known gaps

- A live image tag, live URL, and release-gate JSON are not part of this tag. Plane issues that wait on those stay In Progress.
- The personal-data gate is a route of its own. Easy Mode launch does not call it.
- `test_compose_hands_hosts_the_gateway_url_and_exposes_strict_mode` still expects `max-size` on the model-gateway service. That block does not have it. The assertion was already on main before these leftover commits. This tag does not change it.
- The installer note that pinned examples to `v1.0-rc10.10` was not merged. These notes and the install manual point at `v1.0-rc10.14`.
