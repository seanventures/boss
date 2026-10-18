## Summary

<!-- fix(<area>): <one line>. A BOSS self-fix (docs/product/BOSS_FIXES_ITSELF.md §7.6): written by
     Mender, BOSS's maintainer agent, from a failure BOSS placed on its own code, approved by a
     person and verified on their one-box. BOSS fills this in; keep the machine block below — the
     labeler (.github/workflows/boss-agent-labels.yml) reads it. -->

<!-- boss-self-fix v1 {"class_signature": "", "location": "", "area": "", "boss_version": "", "engine": "", "verified_local": false, "red_green": {"tests": 0}, "gates": {}, "upstream_ref": ""} -->

**Found by:** BOSS recovery agent on a real run (signature ``)
**Diagnosis:**
**Rejected:**
**Fixed by:** Mender (BOSS agent) · engine: · BOSS

## Evidence

- [ ] Regression test added: `` (fails on the base, passes with this change)
- [ ] Affected suites · P0 tier · private infrastructure: pass
- [ ] No test deleted, skipped or loosened (`tests_not_weakened`)
- [ ] Reviewers: code · security · regression · consistency
- [ ] Verified on a one-box: the stuck step resumed and passed

Tests run:

<!-- The exact commands and their summaries. -->

## Safety

Paths touched: <!-- each with its tier --> · Host effect: <!-- yes/no, what -->

## Risks and rollback

Revert this commit.

<details><summary>Redacted evidence</summary>

<!-- The symptom, the last log lines, the diagnosis, the red/green output, the gate results —
     scrubbed of this install's names, hosts, addresses and secrets before it left. -->

</details>

<!-- Trailers on the commit: BOSS-Self-Fix-Ref, BOSS-Signature, BOSS-Agent, BOSS-Engine,
     BOSS-Approved: yes. No person's name or email (they stay on the install). -->
