# BOSS Roadmap

## 2-week target: v0 vertical slice

- Monorepo scaffold.
- Postgres schema.
- API and worker.
- Command → task → plan artifact → decision card.
- Budget policy/reservation skeleton.
- Receipt dedupe tests.
- Decision inbox UI.

## 4-week target: Software Factory mock

- Mock software factory workflow.
- Review packet artifacts.
- PR proposal artifacts.
- GitHub adapter interface with mock implementation.
- CI/test result artifacts.
- Outbox/inbox states and deadletter UI.

## 8-week target: Real integrations and controlled dogfooding

- Real GitHub adapter behind receipts.
- LiteLLM model gateway integration behind budget reservations.
- Slack outbox in mock then real mode.
- OpenHands/Codex coding worker integration behind policy.
- Self-improvement workflow: observe → propose → PR → test → approve.

## Later

- Budget slider with forecasting and anomaly detection.
- Role trust/autonomy ladder.
- MCP/Activepieces connector pack.
- OpenCost/KEDA/Karpenter for infra elasticity.
- Enterprise audit export and data boundary policies.
