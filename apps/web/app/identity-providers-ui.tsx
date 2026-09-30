/**
 * Minimal IdP product-pack browser for the enterprise area.
 * Catalog is API-driven; setup uses dry-run blueprints (no secrets).
 */

import type {
  IdpCatalogItem,
  IdpProjectStatusItem,
  IdentityProvidersFeed,
} from "../lib/identity-providers";

function protocolPills(protocols: string[]) {
  return protocols.map((p) => (
    <span key={p} className="pill" style={{ marginRight: 4, fontSize: "0.75rem" }}>
      {p.toUpperCase()}
    </span>
  ));
}

function statusTone(status: string): string {
  if (status === "available" || status === "active") return "";
  if (status === "planned") return " warn";
  return " warn";
}

export function IdentityProvidersCatalogView({
  feed,
  projectId,
}: {
  feed: IdentityProvidersFeed;
  projectId?: string;
}) {
  const statusById = new Map<string, IdpProjectStatusItem>();
  for (const item of feed.projectStatus) {
    statusById.set(item.idp_id, item);
  }

  return (
    <article className="stack-card identity-providers-pack">
      <h3>Identity provider packs</h3>
      <p className="detail">
        First-class setup blueprints for the top workforce and developer IdPs.
        Built on OpenID Connect, SAML 2.0, and SCIM 2.0 — BOSS never stores raw
        IdP secrets in catalog or blueprint responses.
      </p>

      {!feed.apiReachable ? (
        <p className="detail">
          {feed.blockedReason ?? "Identity provider API is unavailable."}
        </p>
      ) : (
        <>
          <div className="feed-row" style={{ marginBottom: 12 }}>
            <div>
              <p className="detail">
                {feed.catalog.length} packs · {feed.configuredCount} configured
                {projectId ? ` · project ${projectId}` : ""}
              </p>
              {feed.standards.length > 0 ? (
                <p className="detail">Standards: {feed.standards.join(" · ")}</p>
              ) : null}
            </div>
          </div>

          {feed.catalog.length === 0 ? (
            <p className="detail">No IdP packs returned by the API.</p>
          ) : (
            <div className="feed-list">
              {feed.catalog.map((idp: IdpCatalogItem) => {
                const configured = statusById.get(idp.id);
                return (
                  <div className="feed-row" key={idp.id}>
                    <div>
                      <h4>
                        {idp.name}{" "}
                        <span className="detail" style={{ fontWeight: 400 }}>
                          ★ {idp.stars.toFixed(1)}
                        </span>
                      </h4>
                      <p className="detail">
                        {protocolPills(idp.protocol_support)}
                        <span className="detail"> · {idp.category.replace(/_/g, " ")}</span>
                      </p>
                      <p className="detail">{idp.market_notes}</p>
                      {configured?.configured ? (
                        <p className="detail">
                          Configured:{" "}
                          {configured.configurations
                            .map(
                              (c) =>
                                `${c.provider_type ?? "?"} (${c.status ?? "unknown"})`,
                            )
                            .join(", ")}
                        </p>
                      ) : (
                        <p className="detail">Not configured for this project.</p>
                      )}
                      <p className="detail">
                        <a href={idp.docs_url} target="_blank" rel="noreferrer">
                          Vendor docs
                        </a>
                        {" · "}
                        <code>GET /v1/identity-providers/{idp.id}</code>
                      </p>
                    </div>
                    <span className={`pill${statusTone(idp.status)}`}>
                      {idp.status}
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          <p className="detail" style={{ marginTop: 12 }}>
            Materialize a dry-run enterprise payload via{" "}
            <code>POST /v1/identity-providers/&#123;idp_id&#125;/blueprint</code>,
            store credentials as <code>secret_ref</code>, then upsert through{" "}
            <code>/v1/enterprise/identity-providers</code>.
          </p>
        </>
      )}
    </article>
  );
}

/** Convenience export matching other *-ui.tsx patterns. */
export default IdentityProvidersCatalogView;
