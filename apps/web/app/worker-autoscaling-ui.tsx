/**
 * Worker autoscaling panel — desired vs actual, queue pressure, multi-pool policies.
 */

import type {
  WorkerAutoscalingHistoryItem,
  WorkerAutoscalingStatusFeed,
} from "../lib/worker-autoscaling";

function actionClass(action: string): string {
  const value = action.toLowerCase();
  if (value === "scale_up" || value === "applied") return "tone-good";
  if (value === "scale_down") return "tone-warn";
  if (value.startsWith("held") || value === "disabled") return "tone-warn";
  if (value === "no_change" || value === "no_policy") return "tone-muted";
  return "tone-muted";
}

export function WorkerAutoscalingPanel({
  feed,
  history = [],
}: {
  feed: WorkerAutoscalingStatusFeed;
  history?: WorkerAutoscalingHistoryItem[];
}) {
  return (
    <article
      className="stack-card"
      id="worker-autoscaling"
      aria-label="Worker autoscaling"
    >
      <h3>Worker autoscaling</h3>
      <p className="detail">
        Queue-driven multi-pool scale-out over elastic fabric. Policy is
        deterministic (min/max, scale-up threshold, cooldown). Apply requests
        capacity through elastic fabric — real provision only when{" "}
        <code className="mono">BOSS_INFRA_REAL_PROVISION_ALLOWED</code> is on;
        otherwise cloud/VM slots are simulated. Docs:{" "}
        <code className="mono">docs/product/WORKER_AUTOSCALING.md</code>
      </p>

      {!feed.apiReachable ? (
        <p className="detail" role="alert">
          Worker autoscaling API unreachable
          {feed.blockedReason ? `: ${feed.blockedReason}` : "."}
        </p>
      ) : (
        <>
          <div className="stack-grid" aria-label="Queue pressure">
            <section>
              <h4>Queue</h4>
              <dl className="kv">
                <div>
                  <dt>Queue depth</dt>
                  <dd className="mono">{feed.queueDepth}</dd>
                </div>
                <div>
                  <dt>Leased jobs</dt>
                  <dd className="mono">{feed.leasedJobs}</dd>
                </div>
                <div>
                  <dt>Running jobs</dt>
                  <dd className="mono">{feed.runningJobs}</dd>
                </div>
                <div>
                  <dt>Real provision</dt>
                  <dd className={feed.infraRealProvisionAllowed ? "tone-warn" : "tone-muted"}>
                    {feed.infraRealProvisionAllowed ? "allowed" : "simulated only"}
                  </dd>
                </div>
              </dl>
            </section>

            <section aria-label="Pools">
              <h4>Pools (desired vs actual)</h4>
              {feed.pools.length === 0 ? (
                <p className="detail">
                  No autoscaling policies yet. Worker pools scale only once a policy sets their
                  minimum and maximum; until then each pool keeps the workers it started with.
                </p>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Pool</th>
                      <th scope="col">Desired</th>
                      <th scope="col">Actual</th>
                      <th scope="col">Policy target</th>
                      <th scope="col">Action</th>
                      <th scope="col">Limits</th>
                    </tr>
                  </thead>
                  <tbody>
                    {feed.pools.map((pool) => (
                      <tr key={pool.poolKey}>
                        <td className="mono">{pool.poolKey}</td>
                        <td className="mono">{pool.desiredWorkers}</td>
                        <td className="mono">{pool.actualWorkers}</td>
                        <td className="mono">
                          {pool.policyDesired == null ? "—" : pool.policyDesired}
                        </td>
                        <td className={actionClass(pool.action)}>
                          {pool.action}
                          {pool.holdReason ? (
                            <span className="detail"> ({pool.holdReason})</span>
                          ) : null}
                        </td>
                        <td className="mono detail">
                          {pool.minWorkers ?? "—"}–{pool.maxWorkers ?? "—"}
                          {pool.cooldownSeconds != null
                            ? ` · cd ${pool.cooldownSeconds}s`
                            : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          </div>

          {history.length > 0 ? (
            <section aria-label="Recent scale history">
              <h4>Recent history</h4>
              <ul className="plain-list">
                {history.slice(0, 8).map((item) => (
                  <li key={item.id}>
                    <span className={`mono ${actionClass(item.status)}`}>
                      {item.status}
                    </span>{" "}
                    <span className="mono">{item.poolKey}</span>{" "}
                    <span className="detail">
                      {item.previousDesired}→{item.desiredWorkers} (q=
                      {item.queueDepth}, actual={item.actualWorkers})
                      {item.receiptId ? ` · rcpt ${item.receiptId.slice(0, 12)}…` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </>
      )}
    </article>
  );
}
