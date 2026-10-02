/**
 * Minimal IdP product-pack browser for the enterprise area.
 * Catalog is API-driven; setup uses dry-run blueprints (no secrets).
 */

import { API_AUTH_TRUTH, CONSOLE_SIGN_IN_TRUTH } from "../lib/sign-in-truth";

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
      {/* WHAT SIGN-IN USES TODAY, before any provider is offered — so a configured provider is
          not mistaken for one the login page consults. */}
      <div className="stack-card" data-testid="identity-sign-in-truth" style={{ marginBottom: 12 }}>
        <strong>How sign-in works today</strong>
        <p className="detail">{CONSOLE_SIGN_IN_TRUTH}</p>
        <p className="detail">{API_AUTH_TRUTH}</p>
      </div>
      <p className="detail">
        Setup notes for common workforce and developer identity providers, over OpenID
        Connect, SAML 2.0 and SCIM 2.0. Secrets are referenced by name, never shown here.
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
                      {/* No rating: the stars were catalogue data with no source anyone could
                          check, shown as if they were a measurement. */}
                      <h4>{idp.name}</h4>
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
                          {idp.name} setup guide ↗
                        </a>
                        {" · "}
                        <a href="#identity-provider-form">Record it below</a>
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
            To record a provider, take the issuer, audience and keys from its setup guide and
            fill in <a href="#identity-provider-form">Self-hosted identity providers</a> below.
            Keep the client secret in your secret store and enter only its name as the SecretRef.
          </p>
        </>
      )}
    </article>
  );
}

/** Convenience export matching other *-ui.tsx patterns. */
export default IdentityProvidersCatalogView;
