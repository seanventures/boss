# Community target repository controls — 2026-09-28

**Target:** `seanventures/boss`  
**Reviewed source:** `1a8102a2eb7945afda64f72fb18906c134541bdd`  
**Status:** PF-8 open. The target is private and has no commits, branches, tags, or releases. No Community export has been pushed.

## Observed controls

| Control | Current observation |
|---|---|
| GitHub Actions | `GET /repos/seanventures/boss/actions/permissions` initially returned `enabled=true`, `allowed_actions=all`. The release-preparation change set `enabled=false`; a fresh GET verified `enabled=false`. |
| Branch protection and repository rulesets | Both API checks returned HTTP 403 with GitHub's instruction to upgrade to GitHub Pro or make the repository public. There is no branch to protect yet. |
| Secret scanning and Dependabot alerts | Both API checks reported disabled on this private target. No code-security configuration is attached. |
| Private vulnerability reporting | The API returned HTTP 404 while the target is private. The exported tree includes `SECURITY.md` with a private reporting address, but that file has not been pushed. |
| Rollback | No target commit, tag, or release exists. The reviewed local export history has not been pushed. |

GitHub documents [protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches) and [rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets) for public repositories on Free, and for private repositories on paid plans. Its [secret-scanning scope](https://docs.github.com/en/code-security/reference/secret-security/secret-scanning-scope) says public repositories are scanned for free; private organization repositories need GitHub Secret Protection on an eligible plan. [Private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository) is documented for public repositories.

The current checklist asks for branch protection and scanning before visibility changes. That cannot be evidenced on this empty private target under its current plan. The release owner must arrange the eligible private-plan controls or approve a revised, tightly controlled publication sequence after every other gate passes. A visibility change is not a safe way to test the controls: making the repository public exposes the tree and its history immediately, and making it private again cannot retract copies. Recheck every control after any eventual visibility change; GitHub warns that private-to-public transitions disable existing push rulesets.

Keep the repository private and empty while PF-1 through PF-7 remain open. GitHub Actions are disabled and should stay disabled unless a separate reviewed change calls for them.
