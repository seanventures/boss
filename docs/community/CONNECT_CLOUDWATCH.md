# Connect AWS CloudWatch

BOSS reads CloudWatch **metrics, logs and alarms** the same way it reads Prometheus and Loki, and
receives CloudWatch alarms as alerts. Its runbooks can then look at the data and, with a person's
approval, take a remediation step. BOSS never writes to the AWS account.

## 1. A read-only AWS identity

Give BOSS an IAM role (preferred: instance or task role) or a user with **only** these actions:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "cloudwatch:GetMetricData",
      "cloudwatch:DescribeAlarms",
      "logs:StartQuery",
      "logs:GetQueryResults",
      "logs:FilterLogEvents"
    ],
    "Resource": "*"
  }]
}
```

Narrow `Resource` to your log groups if you like. The connector also refuses any other AWS call in
code, so a bug cannot widen what the policy allows.

**Credentials never go in the connection.** Either:

- run BOSS with a role (the AWS credential chain finds it) and name no keys, or
- store the access key pair in the secret store, deployed as env vars, and name those vars:
  `access_key_env`, `secret_key_env` (and `session_token_env` for temporary keys).
  A named var that is empty fails the connection with a 422 instead of returning nothing.

## 2. The connection

```json
{"kind": "cloudwatch", "region": "us-east-1", "log_group": "/aws/apigateway/orders",
 "access_key_env": "CW_ACCESS_KEY_ID", "secret_key_env": "CW_SECRET_ACCESS_KEY"}
```

Register it with `POST /v1/reliability/sources`, or pass it inline to `POST /v1/telemetry/query`.
`log_group` may list several groups, comma-separated (Logs Insights reads them all).

| kind | query |
|---|---|
| `metrics` | `AWS/ApiGateway 5XXError Sum ApiName=orders` (Namespace Metric [Stat] [Dim=Value ...]), a Metrics Insights `SELECT ...`, or MetricDataQueries JSON |
| `logs` | a Logs Insights query (`fields @timestamp, @message \| filter ...`) or a filter pattern (`ERROR`) |
| `alerts` | none: the alarms in ALARM state |

## 3. Alarms as alerts

Point a CloudWatch alarm's SNS topic at an HTTPS subscription to
`/v1/ingest/<source_id>/cloudwatch` (a monitoring source of kind `cloudwatch_sns`). BOSS confirms
the subscription, checks the SNS signature, and opens or resolves the alert per alarm and
dimensions. `INSUFFICIENT_DATA` never resolves anything.

## 4. Runbooks: look, then remediate with approval

A runbook step may be:

- `query_telemetry` — read-only; runs without approval; the rows go on the receipt.
  `params`: `source` (the connection above), `capability`, `query`, `window_minutes`.
- `rollback_deployment` — roll a BOSS-managed deployment back to its last known-good commit.
  `params.deployment_id`.
- `redeploy_deployment` — redeploy a build's latest deployment. `params.run_id`.

Remediation steps always stop for a person: the run holds on a "hold" decision card. Once a
person approves it, the run carries the step out, and that approval is also the rollback deploy's
approval (no second card). Each step leaves a receipt with what ran, who approved it and what
happened. A step's own "kind" is ignored; BOSS classifies every action itself.

## Live check (needs read-only credentials)

The tests use botocore's Stubber, so nothing above has run against a real account yet. To check
a live account:

1. Create the read-only identity above in a non-production account, and create one alarm that is
   in ALARM (for example a threshold of 0 on any metric that has data).
2. Put its keys in the secret store as `CW_ACCESS_KEY_ID` / `CW_SECRET_ACCESS_KEY` and redeploy,
   or run BOSS with the role.
3. `POST /v1/telemetry/query` three times with the connection above and `kind` `metrics`,
   `logs` and `alerts`. Expect points, log lines, and that alarm.
4. Subscribe the alarm's SNS topic to the ingest URL and flip the alarm; the alert opens and
   resolves in Monitoring.
