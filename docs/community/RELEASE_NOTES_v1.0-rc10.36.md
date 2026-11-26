# BOSS Community 1.0.0-rc.10.36 candidate

RC10.36 is the customer-preview build. It automates API gateway onboarding end to end. MCP servers and skills are easier to add and work more reliably. Govern gains the controls a 10–20 developer preview needs. Several security gaps found in review are closed.

This is a test candidate. Cutting the tag does not deploy a host or publish an image.

## What is better for you

**API gateway onboarding runs on its own**

- The "Onboard a config file" workflow runs the whole chain in order:
  1. Validate the partner's file and propose a fix.
  2. Review.
  3. Merge.
  4. Deploy to staging.
  5. Run contract tests.
  6. Promote to production behind one approval.
  7. Report the outcome on the partner's PR.
- **A file that fails its checks** is rejected on the PR, with the validation report and the proposed fix. No person needs to step in.
- **Contract tests that fail on staging** roll staging back on their own, and the rollback is reported on the PR.
- **One approval per change.** Production promotion asks once. An approval counts only for its own run, step and config, and can be used only once.
- **One run per event.** The same GitHub event never starts two runs.
- **Rollback on demand** is available from chat or MCP.
- **Demo files.** A demo tenant with a broken partner config, its fixed version and a reset script ships under `examples/api-gateway-onboarding/`.

**Skills & MCP: one simple page**

- Skills & MCP opens on two sections, **MCP servers** and **Skills**, with **Add MCP server** and **Add skill** at the top.
- Adding a server is one screen:
  1. Pick it or paste its address.
  2. Add its token. The token is saved to the vault.
  3. Test the connection, which lists the server's tools.
  4. Choose which agents get it.
- The same add screen opens from the agent page, the create-agent wizard, the workflow editor, ⌘K search and Get started.
- Every earlier option is still there. Rarely used options are under Advanced.
- **MCP integration fixes:**
  - SSE servers work.
  - Slow tools are cancelled at the server and fail with a plain message, and are never retried blindly.
  - Internal servers behind a company proxy are reached directly, as the proxy settings intend.
  - OAuth sign-in works behind a proxy.
  - A revoked token is refreshed once.
  - Unusual tool schemas no longer break an agent's turn.
  - Local servers get the company proxy and package mirrors.

**Workflows**

- **Workflow Library:** browse workflows, add one to a project, edit a copy, and share your own with the org. Sharing needs an org owner or admin and runs the same checks as Save.
  - A team's new version is only offered to the projects that use it; nothing changes without their consent.
  - API gateway onboarding ships as an editable template.
- **Quality bar:** choose Prototype, Standard or Production in Setup or per run.
  - You can choose per check whether it blocks or becomes a follow-up.
  - Below Production, builds never stop to ask a person.
  - Lowering a run's bar below its workflow's or project's bar needs permission and is recorded.
  - Production promotion always waits for a person.
- **Workflows that cannot run are refused:**
  - A skill with no executor cannot be granted, offered or approved.
  - A step that did nothing but says "completed" fails.
  - A tool call with an argument the tool does not take is refused.
  - Draft workflows are checked against their requirement before publishing.

**Govern for a preview team**

- **Team budgets:** a team is an identity group. There is a one-click $2,000/month pilot preset, a month-end forecast and the costliest runs.
  - A soft cap warns.
  - A hard cap stops new work.
  - Neither asks anyone mid-build.
- **Pilot metrics:** review time, regressions caught before merge, and alerts that BOSS fixed on its own. They show next to your own "before BOSS" numbers, with CSV and JSON export.
- **Subscriptions:**
  - Several Claude Code accounts per install, used in turn.
  - Headroom alerts when an account has 20%, 10% or 5% left.
  - A header chip shows the account closest to its limit.
  - When every account is at its limit, builds wait for the reset instead of failing.
- **Golden journeys:** end-to-end journeys run daily under a narrow service account in a dedicated test project. Govern › More › Journeys shows the results and the green-day count.
  - Only BOSS's daily schedule or a person's **Run now** can start them.
  - The feature freeze lifts after 5 green days.

**Security**

- **Subscription logins stay out of the agent sandbox.** Claude Code, Codex and Grok sessions get a short-lived run token. The model proxy holds the real login, refreshes it, and uses the pool account assigned to the run.
- **Folders inside an agent's run area** are created without following links, so an agent cannot redirect them.
- **Chat cannot run anything that costs money** without a person pressing the button.
- **SSRF checks** in the ticket connectors look at the resolved address.

**Reliability**

- Approved builds resume after a redelivery and are never cancelled as abandoned.
- Cards never show a pass label for a failed check.
- A PR's "[partial]" title clears when a full build lands.
- Background work that the earlier lock limits did not cover now has them (BOSSAPP-1462, 1463).

**Documentation for a preview**

- `docs/preview/`: preview plan, developer onboarding, feature freeze and pilot checklist.
- `docs/security/SECURITY_REVIEW_PACK.md`: data flow, secrets, SSO and RBAC, audit, outbound network and model providers. Every claim cites code or a test, and the known gaps are stated.
- `docs/operations/OKTA_GROUPS_TO_ROLES.md`, `docs/design/API_GATEWAY_ONBOARDING.md` and the supported-stacks matrix.

## Upgrade notes

- Run `boss upgrade` or `remote.sh update` as usual.
- To use golden journeys on a host, set up these once:
  - a service account bound only in a test project, with its key's vault reference in `BOSS_JOURNEY_KEY_REF`;
  - `BOSS_JOURNEY_PROJECT`, `BOSS_JOURNEY_PRINCIPAL`, `BOSS_JOURNEY_VARS` and `BOSS_JOURNEY_HOST`.

  Then turn on the daily run in Govern › Journeys.

## Known issues

- **GitHub Enterprise Server** support is still in review. Until it lands, use github.com connections.
- **Codex subscription** routing was checked against a real ChatGPT login. Claude Code's request format was checked, but no live Claude turn has gone through the proxy yet. Grok has not been checked live.
- **Pending golden journeys:**
  - J8's chain after the merge card waits for a separate approver account.
  - J10 (MCP) waits for a one-time setup step.
  - J7 (Okta) waits for an Okta tenant.
- **Follow-ups:**
  - a per-run spend cap on subscription run tokens;
  - a run folder the agent cannot rename entries in;
  - one-use approvals on the generic deploy path;
  - one scripted-engine build still calls a model.

## Checks

- **Reviews:** every PR had an independent review. Every serious finding was fixed and verified again before merging, the security ones up to four times.
- **Merge gate:** every PR passed the local merge gate on the exact commit that was merged.
- **Enterprise lane:** passed every step on a fresh, locked-down Ubuntu 24.04 box (4 vCPU, 8 GB, 30 GB disk, AppArmor) behind an inspecting proxy with private mirrors:
  - the install took 10 minutes and left 12 GB free;
  - sandbox and Codex sandbox;
  - plumbing 20 of 20, engines 21 of 21, scripted build 3 of 3.
- **Laptop install, on the release commit:**
  - plumbing 20 of 20;
  - engines 21 of 21 and golden journeys 2 of 2, on the commit before the final console fix.
- **Browser walk:** an independent tester clicked every new screen at 1440, 1024, 768 and 400 px. Every gap that blocked the release was fixed and checked again on the live console. Smaller polish items are tracked for the next release.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
