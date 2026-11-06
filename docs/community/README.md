---
title: Home
hide:
  - toc
---

<div class="boss-home" markdown="1">

# A self-hosted control plane for AI software work

<p class="boss-lede">An agent can plan, spend, and change things. You review the plan, the result, and the cost. BOSS keeps the receipt.</p>

## Who it is for

A technical founder who will run BOSS on a server they administer. You should be comfortable with one Linux machine, Docker, a GitHub repository you are allowed to change, and a diff. The people you invite share that install. It is not a hosted service.

## The path

<div class="boss-path" markdown="1">

1. [Install](QUICKSTART.md#install) <span class="boss-path__note">Pin v1.0-rc10.11 on a machine you administer.</span>
2. [Claim](QUICKSTART.md#claim-the-install) <span class="boss-path__note">Open the one-hour link and become the Owner.</span>
3. [Repository](QUICKSTART.md#connect-a-repository) <span class="boss-path__note">Connect one GitHub repository you may change.</span>
4. [Model](QUICKSTART.md#connect-a-model) <span class="boss-path__note">Connect one model. A coding subscription signs in inside BOSS.</span>
5. [One pull request](QUICKSTART.md#open-one-pull-request) <span class="boss-path__note">Describe one small change, then review what opened.</span>

</div>

The [quickstart](QUICKSTART.md) is that path. [Your hostname](hostname.md) is where TLS goes. The page after the pull request is the [first change](FIRST_USEFUL_CHANGE.md).

## What you get

<dl class="boss-gets">
  <dt>A plan you can read</dt>
  <dd>A governed workflow turns a goal into a plan before the work runs.</dd>
  <dt>A budget reservation before work</dt>
  <dd>The reservation is an estimate. It is not a cap on a provider's invoice. Review held and uncertain exposure before you continue.</dd>
  <dt>An approval when it matters</dt>
  <dd>Gated steps wait, with the context for the decision. A recommendation is not permission.</dd>
  <dt>A receipt after</dt>
  <dd>The receipt and its dedupe key identify an attempt. A dedupe key does not make every provider idempotent. Reconcile an ambiguous provider outcome before a retry.</dd>
</dl>

## Limits

This is a release candidate, not a promise that every path is qualified. The bounded installed checks behind this candidate are an owned Linux arm64 machine that already had Docker. A clean-host install and useful provider work are separate checks. The [v1.0-rc10.11 notes](RELEASE_NOTES_v1.0-rc10.11.md) and the [RC9 validation record](RC9_VALIDATION.md) say what has been checked, and what has not.

Credentials stay on the install. A hosted model receives the authentication and the task data for the calls you authorize. No telemetry collector is set, so nothing is sent until you set one. "Improve BOSS" is off unless you turn it on. `BOSS_TELEMETRY=off` or `DO_NOT_TRACK=1` sends nothing. [What a payload can contain](https://github.com/seanventures/boss/blob/main/docs/product/RUN_SCORECARDS.md).

## Where to go next

- [Do the work](FIRST_USEFUL_CHANGE.md), then [review a result](RESULT_REVIEW.md) and [return to it](RETURNING_TO_WORK.md)
- [People](PEOPLE_AND_ACCESS.md), [spend and approvals](ARCHITECTURE_GOVERNANCE.md)
- [GitHub](CONNECT_GITHUB.md), [models](MODEL_CONNECTIONS.md), [terminal](terminal.md), [MCP](MCP.md), [CLI](cli.md)
- [Features](FEATURES.md) and the [release notes](RELEASE_NOTES_v1.0-rc10.11.md)
- The [fifteen example workflows](https://github.com/seanventures/boss/blob/main/examples/README.md). Installing an example does not start work.
- [Contributing](https://github.com/seanventures/boss/blob/main/CONTRIBUTING.md) and the [Apache License 2.0](https://github.com/seanventures/boss/blob/main/LICENSE)
- [Report a problem](FEEDBACK_LOOP.md). Security reports go to the address in the [security policy](https://github.com/seanventures/boss/blob/main/SECURITY.md), not a public issue.

</div>
