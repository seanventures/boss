/**
 * Infra Fabric panel — multi-cloud catalog, elastic fabric, reference host PaaS,
 * and provider usage rollups for Mission Control ProviderOps.
 *
 * Community honesty: catalog discovery + BYOK self-managed fleets only.
 * Real host effects and managed capacity are never implied when flags are off.
 */

import type { InfraFabricFeed } from "../lib/infra-fabric";

function formatMicros(amount: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 4,
  }).format(amount / 1_000_000);
}

function statusClass(status: string): string {
  const value = status.toLowerCase();
  if (value === "available" || value === "ready" || value === "active" || value === "running") {
    return "tone-good";
  }
  if (value === "planned" || value === "pending" || value === "bootstrapping") {
    return "tone-warn";
  }
  if (value === "unavailable" || value === "error" || value === "failed") {
    return "tone-bad";
  }
  return "tone-muted";
}

export function InfraFabricPanel({ feed }: { feed: InfraFabricFeed }) {
  const { multiCloud, elastic, providerUsage, partialFailures } = feed;
  const anyReachable =
    multiCloud.apiReachable ||
    elastic.apiReachable ||
    providerUsage.apiReachable;

  return (
    <article className="stack-card" id="infra-fabric" aria-label="Infra fabric">
      <h3>Infra Fabric</h3>
      <p className="detail">
        Multi-cloud catalog, elastic worker fabric, and
        per-provider usage. Community is BYOK + self-managed only — no hosted fleets
        or silent real-host writes. Operator docs:{" "}
        <code className="mono">docs/api/MULTI_CLOUD.md</code> ·{" "}
      </p>

      {partialFailures.length > 0 ? (
        <p className="detail" role="status">
          Partial fabric data: {partialFailures.join("; ")}.
        </p>
      ) : null}

      {!anyReachable ? (
        <p className="detail" role="alert">
          Infra fabric APIs are unreachable. Confirm the control plane is up and
          routes <code className="mono">/v1/multi-cloud/*</code>,{" "}
          <code className="mono">/v1/elastic/*</code>, and{" "}
          <code className="mono">/v1/provider-usage/*</code> are mounted.
        </p>
      ) : null}

      <div className="stack-grid">
        {/* Multi-cloud catalog */}
        <section aria-label="Multi-cloud providers">
          <h4>Multi-cloud providers</h4>
          {!multiCloud.apiReachable ? (
            <p className="detail">
              Catalog unavailable
              {multiCloud.blockedReason ? `: ${multiCloud.blockedReason}` : "."}
            </p>
          ) : multiCloud.providers.length === 0 ? (
            <p className="detail">No multi-cloud providers in catalog.</p>
          ) : (
            <>
              <p className="detail">
                {multiCloud.providerCount} providers · {multiCloud.skillCount} skills
                in index · read-only discovery (connect keys via ProviderOps accounts)
              </p>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Provider</th>
                      <th scope="col">Adapter</th>
                      <th scope="col">Categories</th>
                      <th scope="col">Skills</th>
                    </tr>
                  </thead>
                  <tbody>
                    {multiCloud.providers.map((provider) => (
                      <tr key={provider.providerKey}>
                        <td>
                          <strong>{provider.displayName}</strong>
                          <div className="detail mono">{provider.providerKey}</div>
                        </td>
                        <td>
                          <span className={statusClass(provider.adapterStatus)}>
                            {provider.adapterStatus}
                          </span>
                        </td>
                        <td>{provider.categories.join(", ") || "—"}</td>
                        <td>
                          {provider.skillCount}
                          <span className="detail">
                            {" "}
                            ({provider.infraSkillCount} infra ·{" "}
                            {provider.modelSkillCount} model)
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>

        {/* Elastic fabric */}
        <section aria-label="Elastic fabric" data-fabric-status={elastic.status}>
          <h4>Elastic fabric</h4>
          {elastic.status === "not_configured" || !elastic.configured ? (
            <p className="detail" data-empty-state="elastic-not-configured">
              Elastic fabric is not configured. No worker instances and
              host.connected=false — Community does not invent a managed fleet.
              {elastic.blockedReason ? ` ${elastic.blockedReason}` : ""}
            </p>
          ) : elastic.status === "error" ? (
            <p className="detail">
              Elastic status unavailable
              {elastic.blockedReason ? `: ${elastic.blockedReason}` : "."}
            </p>
          ) : (
            <>
              <div className="provider-suite-summary" aria-label="Elastic fabric summary">
                <div>
                  <span>Pools</span>
                  <strong>{elastic.poolCount}</strong>
                </div>
                <div>
                  <span>Instances</span>
                  <strong>{elastic.instanceCounts.total}</strong>
                  <small>{elastic.instanceCounts.active} active</small>
                </div>
                <div>
                  <span>Real host effects</span>
                  <strong className={elastic.realHostEffects ? "tone-warn" : "tone-good"}>
                    {elastic.realHostEffects ? "allowed" : "off"}
                  </strong>
                  <small>
                    {elastic.realHostEffects
                      ? "BOSS_INFRA_REAL_PROVISION_ALLOWED"
                      : "simulated / local only"}
                  </small>
                </div>
              </div>
              {Object.keys(elastic.instanceCounts.byKind).length > 0 ? (
                <p className="detail">
                  By kind:{" "}
                  {Object.entries(elastic.instanceCounts.byKind)
                    .map(([kind, count]) => `${kind}=${count}`)
                    .join(" · ")}
                </p>
              ) : null}
              {elastic.pools.length > 0 ? (
                <div className="feed-list">
                  {elastic.pools.slice(0, 6).map((pool) => (
                    <div className="feed-row" key={pool.id || pool.poolKey}>
                      <div>
                        <h4>{pool.poolKey || pool.id}</h4>
                        <p className="detail">
                          {pool.kind} · {pool.status} · workers{" "}
                          {pool.currentWorkers}/{pool.desiredWorkers}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="detail">
                  No worker pools yet. Request capacity via{" "}
                  <code className="mono">POST /v1/elastic/capacity</code> against
                  your own ProviderOps connections — Community does not provision
                  managed fleets.
                </p>
              )}
              {elastic.message ? (
                <p className="detail">{elastic.message}</p>
              ) : null}
            </>
          )}
        </section>

        {/* Provider usage */}
        <section aria-label="Provider usage summary" data-fabric-status={providerUsage.status}>
          <h4>Provider usage</h4>
          {providerUsage.status === "error" ? (
            <p className="detail">
              Usage summary unavailable
              {providerUsage.blockedReason
                ? `: ${providerUsage.blockedReason}`
                : "."}
            </p>
          ) : providerUsage.providers.length === 0 ? (
            <p className="detail">
              No usage ledger events yet. Elastic start/heartbeat/terminate and
              adapter instrumentation write here.
            </p>
          ) : (
            <div className="feed-list">
              {providerUsage.providers.slice(0, 8).map((row) => (
                <div className="feed-row" key={row.provider}>
                  <div>
                    <h4>{row.provider}</h4>
                    <p className="detail">
                      {row.eventCount} events · ledger{" "}
                      {formatMicros(row.costUsdMicros)}
                      {row.billing
                        ? ` · billing mirror ${formatMicros(row.billing.amountUsdMicros)} (${row.billing.costEventCount} cost events)`
                        : ""}
                    </p>
                    {row.categories.length > 0 ? (
                      <p className="detail">
                        {row.categories
                          .slice(0, 4)
                          .map(
                            (cat) =>
                              `${cat.category}: ${cat.quantitySum} ${cat.unit}`,
                          )
                          .join(" · ")}
                      </p>
                    ) : null}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </article>
  );
}
