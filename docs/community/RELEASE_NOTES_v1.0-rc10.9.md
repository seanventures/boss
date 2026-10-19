# BOSS Community 1.0.0-rc.10.9 candidate

RC10.9 builds on rc10.8 and brings **monitoring, alerting and agent-driven operations** (BOSSAPP-598). It was exercised end to end in the "BOSS operations" demo:
- BOSS's SRE agent wrote an app's monitoring before release;
- a faulty release fired the alerts;
- the agent's own rule caught the bug and the agent opened the incident;
- Incident Response started by itself.

This is a test candidate.

## What changed

### Monitoring and alerting
- **Connect any app.** The Connect an app sheet:
  - **Ways to send data:** BOSS-built apps, OpenTelemetry (OTLP), Prometheus remote write, a `/metrics` URL, synthetic checks and heartbeats.
  - **Alerts from other tools:** Alertmanager, Grafana, Sentry, Datadog and CloudWatch, each with its own signed, rate-limited source token.
  - **Confirmation:** each path ends with a live check that data arrived.
- **Rules in PromQL,** in the Prometheus alerting-rule shape, checked against `promtool`.
  - Every rule shows its query, how often it runs, its window, For, Keep firing, threshold, severity and routing.
  - **New alert rule** builds a rule from an app's real metrics, writes the PromQL, and previews "would it have fired in the last 24 h".
- **Severity SEV1–SEV4,** with icons, and status chips: Firing, Acknowledged (by whom), Pending, Resolved (with duration), Silenced. The Alerts page leads with **Needs attention**.
- **Alert lifecycle:** pending, firing, keep-firing, flapping, no data and resolved; absence and heartbeat rules.
- **Noise control:** fingerprint dedupe, grouping by runbook, inhibition, silences, recurring maintenance windows, deploy holds and storm mode.
- **Notification:**
  - contact points (email with a signed ack link, Slack or Mattermost, signed webhooks);
  - an Alertmanager-style routing tree with a preview;
  - one message per incident, urgency and quiet hours;
  - a watchdog for the pipeline itself.
- **A bundled Prometheus and Grafana** in the install, receiving the apps' series. Each app gets a provisioned dashboard. Grafana and Explore links appear only when Grafana answers.
- **The old reliability alert webhook** now needs a per-source token.

### Agents in operations
- **Set up monitoring:** a new workflow step before the release (in 14 workflows). The SRE agent reads the app's code and the metrics from the first deploy. It proposes a pack: SEV-rated rules, a runbook per rule and dashboard panels. It previews them on real traffic and retunes noisy ones, then writes them as code into `boss.deploy.yaml` in the same PR. The person approves the pack with the release; a `monitoring_gate` set to `block` stops a release with no pack.
- **Agent triage names the agent** and its reasoning on every alert.
  - When the agent isn't sure, it asks: the question shows as **buttons**, and a person's click goes through the same path as the agent's own decision.
  - Asking never holds back a notification.
- **Respond automatically is per rule.**
- **The incident card shows who did what:** triage, the investigating agent with its runbook, evidence and root cause, the fix crew and reviewers, the person's approval, deploy, verification and the report.
- **A new release's failure opens its own incident** instead of joining an old, unrelated one.

### Safety
- **A release card lists every commit it ships** beyond the serving one, with each commit's run, that run's approval status and whether it was reviewed.
  - Commits from rejected or unreviewed runs need a written reason.
  - An approval binds to the list it was shown.
  - The deploy ships exactly the approved head.
- **Incident fixes are built on their own branch,** cut from the serving commit.
- **A promised spec approval holds the build.** Approving a PRD whose workflow already builds never starts a second build.
- **The Jev router hold** (from main, PR #10, BOSSAPP-579).

## Validation
- **Targeted tests** for every change above (the operator chose targeted tests only).
- **The "BOSS operations" demo,** run live on the laptop install. It stopped at an incident fix build when the model account ran out of credit.
- **No full suites and no real-agent release gate** were run for this candidate. There was no model credit, and the operator chose targeted tests only.

## Known issues
- **BOSSAPP-602:**
  - The release-commits check doesn't credit a run's own More-tests and Set-up-monitoring commits.
  - Set up monitoring writes duplicate runbooks on retry, and its pack can break an app's own manifest test.
  - investigate() reads older occurrences on a reused incident.
  - A time label is wrong, and there are other console nits.
- **A model running out of credit is shown as "no model is answering".**
- **Plumbing:** the bundled Prometheus's remote-write forwarding is untested against load, and the 5,000-rule benchmark was not run (BOSSAPP-599).

## Install and upgrade

Select `v1.0-rc10.9` explicitly with the installer's `--ref` option, from a checkout of that same tag. See the [Quickstart](QUICKSTART.md).

Before upgrading an existing install, take a complete protected backup (`boss upgrade` does this). Changing the checkout back does not undo database migrations.
