# Onboarding for preview developers

Your first day with BOSS on the preview install. Your admin has already installed BOSS, connected
Okta, GitHub, Jira and the model keys. If a step fails, see [Report a problem](#report-a-problem).

## 1. Sign in

1. Your admin adds you to an Okta group assigned to BOSS (for example `boss-preview-developers`).
   Okta creates your BOSS account over SCIM.
2. Open the BOSS address your admin gave you and choose **Continue with Okta** (or the name your admin gave
   the provider).
3. Check your access: **My account** shows your roles; your person page shows **Effective access**,
   each permission and the group it came from.

No role? Your group has no role bound yet. Ask your admin
([OKTA_GROUPS_TO_ROLES.md](../operations/OKTA_GROUPS_TO_ROLES.md)).

## 2. Find your repository and tickets

- **Repositories.** Your admin connects the GitHub organisation, or a GitHub Enterprise Server
  with a fine-grained token ([CONNECT_GITHUB.md](../community/CONNECT_GITHUB.md),
  [IDENTITY_ADMIN.md › GitHub Enterprise](../community/IDENTITY_ADMIN.md#github-enterprise)).
  Your project's repository appears in the project.
- **Jira.** Your admin connects Jira and maps your Jira project to a BOSS project. A ticket's type
  and size pick its workflow
  ([TICKET_CONNECTORS.md › The workflow a ticket starts on](../infra/TICKET_CONNECTORS.md#the-workflow-a-ticket-starts-on)).

## 3. Models and budget

- The preview uses the team's own model keys, connected once by the admin
  ([MODEL_CONNECTIONS.md](../community/MODEL_CONNECTIONS.md)). You do not need your own key.
- Every run shows its cost. Budgets and hard stops are set in **Govern**. A run that would pass a
  hard stop stops; it does not ask you mid-run.

## 4. Your first change

1. Start from a Jira ticket (its card in **New work**, or **Import from a ticket system**), or
   describe the change.
2. Read the plan before treating the run as started.
3. Watch the run: steps, decisions, tests, the acceptance check.
4. Review the PR in GitHub. Production deploys always wait for a person.

Step by step, with what each screen shows: [FIRST_USEFUL_CHANGE.md](../community/FIRST_USEFUL_CHANGE.md).

## 5. Onboard a gateway config

The pipeline the preview is for. Try it first on the demo data:
[examples/api-gateway-onboarding](../../examples/api-gateway-onboarding/). What runs today and
what is still being built: [API_GATEWAY_ONBOARDING.md](../design/API_GATEWAY_ONBOARDING.md#what-is-on-main-today).

## What is in the preview

See [PREVIEW_PLAN.md › Scope](PREVIEW_PLAN.md#scope). Stack support, cell by cell:
[SUPPORTED_STACKS.md › Preview matrix](../community/SUPPORTED_STACKS.md#preview-matrix).

## Report a problem

- Stuck during a run: download the run's report and send it as
  [FEEDBACK_LOOP.md](../community/FEEDBACK_LOOP.md) describes. It collects no usage data.
- Include the run link, what you expected and what happened.
- Never paste a key, token or password into a report.
