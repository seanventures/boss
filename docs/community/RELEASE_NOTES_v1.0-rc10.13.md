# BOSS Community 1.0.0-rc.10.13 candidate

RC10.13 is the public `main` after `v1.0-rc10.12`. It keeps that candidate's project experience and local-first work, and adds the tool-call controls and the git-style change review that landed after the tag.

This is a test candidate. Cutting the tag does not deploy a host, publish an image, or re-run the release gate.

## What changed

### Tool calls

- A held call stores `args_sha256` and a redacted preview. Resume refuses when the pending arguments differ. The decision card shows that preview, including the exact `[redacted]`. The receipt refuses before it calls the tool when the approved decision has a digest and the arguments differ. A decision with no digest still runs (BOSSAPP-816). The HMAC signature is not in this candidate.
- Tool results are screened for instruction-override phrases. The tool text is kept. A hit turns the next write from allow into ask (BOSSAPP-822).
- A reachable probe stores schema and description hashes. A same-named tool whose hash changed is stored as ask, and one decision is opened. An unreachable probe does not (BOSSAPP-818).
- Each receipt-path tool call is appended once. Update and delete are rejected. One project's rows list newest first (BOSSAPP-819). There is no HTTP export in this candidate.
- An always-allow row can be granted, listed, and revoked. The live allow/ask/off read is unchanged (BOSSAPP-817).

### Review and local projects

- The change page reviews a local change the way it reviews a GitHub pull request, including comments, request changes, and publish (BOSSAPP-948, BOSSAPP-954).
- A local project deploys from its own repository. A preview keeps its own origin (BOSSAPP-946, BOSSAPP-947).
- Setup can add a GitHub account (BOSSAPP-945).

### Decisions and checks

- Keyword decision eval is checked in. Model rows print `not measured` (BOSSAPP-580).
- A drafted typed decision is a real branch with `on_true` and `on_false` (BOSSAPP-581).
- Ordered tool rules run before the build loop (BOSSAPP-815). The database policy table, scopes, and rate limits are not switched on here.
- Official verify uses the shipped working directory (BOSSAPP-953).
- The install template states that microVM claims stay frozen (BOSSAPP-950).
- The always-on agents ship on. Off is one Govern choice, or `BOSS_AMBIENT_COS=off`.

## Checks

- The version pins match `1.0.0-rc.10.13` (`engine/tests/test_distribution_contract.py`).
- The tool-call slices were tested on disposable databases before they landed. This tag was not installed on a clean host, and the release gate was not re-run.
- `EXPORT_CANDIDATE_STATUS.json` still cites `docs/community/RC9_VALIDATION.md`.

## Known gaps

- Knowledge-graph pull requests #11–#15 stay open.
- HMAC for a held call, the tool catalog table, the audit HTTP route, and switching the live policy read to the always-allow rows stay open.
- A live image tag, live URL, and release-gate JSON are not part of this tag. Plane issues that wait on those stay In Progress.
- Paperclip gaps (BOSSAPP-812) and Kiro gaps (BOSSAPP-934) remain open.
