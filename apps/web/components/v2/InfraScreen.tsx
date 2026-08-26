"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { InfraView } from "../../lib/v2/platform";

/**
 * Infra & Models — where things run, and what the models cost.
 *
 * NO FLEET LOAD, ON PURPOSE
 * -------------------------
 * The reference shows utilisation bars. BOSS records whether a host is VERIFIED, not how busy
 * it is, and a fabricated 62% is indistinguishable from a real one. This console has already
 * shipped one number that was quietly made up; it is not doing it twice.
 *
 * Model spend IS shown, because an LLM call records its own model. Per-agent spend is not,
 * because nothing records which agent a call belongs to — the same rule, applied both ways.
 */

function money(n: number): string {
  return "$" + (n < 100 ? n.toFixed(2) : Math.round(n).toLocaleString());
}

export function InfraScreen({ view }: { view: InfraView }) {
  if (!view.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-infra-unreachable">
        <h1 className="v2-screen-title">Infra &amp; Models</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {view.blockedReason ? ` (${view.blockedReason})` : ""}. An empty list would read as a
          company with no infrastructure.
        </p>
      </section>
    );
  }

  const verified = view.hosts.filter((h) => h.verified).length;
  const spend = view.models.reduce((n, m) => n + m.spend_usd, 0);

  return (
    <section className="v2-screen" data-testid="v2-infra">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Infra &amp; Models</h1>
        <p className="v2-screen-sub">
          {verified} of {view.hosts.length} host{view.hosts.length === 1 ? "" : "s"} verified ·{" "}
          {money(spend)} on models this month
        </p>
      </header>

      <h2 className="v2-ws-h">Hosts</h2>
      {/* Said once, instead of drawing a load bar that would be invented. */}
      <p className="v2-org-isolated-note" data-testid="v2-infra-load-note">
        {view.load_note}
      </p>
      {view.hosts.length === 0 ? (
        <p className="v2-empty" data-testid="v2-infra-no-hosts">
          No deployment hosts are configured. BOSS will not invent one, so nothing can be
          deployed until a host is added and verified.
        </p>
      ) : (
        <ul className="v2-cards" data-testid="v2-infra-hosts">
          {view.hosts.map((h) => (
            <li
              key={h.id}
              className="v2-card"
              data-testid={`v2-host-${h.id}`}
              data-status={h.verified ? "DONE" : "IDLE"}
            >
              <div className="v2-card-head">
                <span className="v2-card-name">{h.name}</span>
                <span className="v2-card-role">{h.arch || "arch unknown"}</span>
              </div>
              <div className="v2-card-meta">
                <span>{h.address}</span>
                {/* Verified or not, said in a word. Only a verified host can take a
                    deployment, and showing them alike offers a destination that fails at
                    deploy time — far from the choice that caused it. */}
                {h.verified ? (
                  <span data-testid={`v2-host-verified-${h.id}`}>verified</span>
                ) : (
                  <span className="v2-wf-violations" data-testid={`v2-host-unverified-${h.id}`}>
                    {h.status} — cannot take a deployment
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <h2 className="v2-ws-h">Deployment targets</h2>
      {view.targets.length === 0 ? (
        <p className="v2-empty" data-testid="v2-infra-no-targets">
          No app is bound to a destination yet.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-infra-targets">
          {view.targets.map((t) => (
            <li className="v2-row" key={t.id} data-testid={`v2-target-${t.id}`}>
              <span className="v2-row-main">
                <span className="v2-row-title">
                  {t.app_name}
                  <span className="v2-row-meta-dim"> · {t.environment}</span>
                </span>
                <span className="v2-row-meta">
                  <span>{t.repo}</span>
                  <span>
                    {t.kind} {t.ref}
                  </span>
                  {t.retired ? (
                    <span className="v2-wf-violations" data-testid={`v2-target-retired-${t.id}`}>
                      retired
                    </span>
                  ) : (
                    <span>{t.status}</span>
                  )}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}

      <h2 className="v2-ws-h">Models this month</h2>
      {view.models.length === 0 ? (
        <p className="v2-empty" data-testid="v2-infra-no-models">
          No model calls recorded this month.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-infra-models">
          {view.models.map((m) => (
            <li className="v2-row" key={m.model} data-testid={`v2-model-${m.model}`}>
              <span className="v2-row-main">
                <span className="v2-row-title v2-mono">{m.model}</span>
                <span className="v2-row-meta">
                  <span>
                    <b>{money(m.spend_usd)}</b>
                  </span>
                  <span>{m.calls.toLocaleString()} calls</span>
                  <span>{m.tokens.toLocaleString()} tokens</span>
                  {/* A price the price map did not know is a guess. It must not sit beside
                      measured figures looking like one. */}
                  {m.estimated ? (
                    <span className="v2-wf-violations" data-testid={`v2-model-estimated-${m.model}`}>
                      price estimated — this model is not in the price map
                    </span>
                  ) : null}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}

      <h2 className="v2-ws-h">Integrations</h2>
      {view.integrations.length === 0 ? (
        <p className="v2-empty" data-testid="v2-infra-no-integrations">
          No MCP servers are configured.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-infra-integrations">
          {view.integrations.map((g) => (
            <li className="v2-row" key={g.id} data-testid={`v2-integration-${g.id}`}>
              <span className="v2-row-main">
                <span className="v2-row-title">{g.name}</span>
                <span className="v2-row-meta">
                  <span>{g.transport}</span>
                  {/* Risk tier and data boundary decide what a server may touch. The name
                      alone makes a tier-3 external server look like a tier-1 internal one. */}
                  <span data-testid={`v2-integration-risk-${g.id}`}>
                    risk tier {g.risk_tier}
                  </span>
                  <span data-testid={`v2-integration-boundary-${g.id}`}>{g.data_boundary}</span>
                  {g.enabled ? (
                    <span>enabled</span>
                  ) : (
                    <span className="v2-row-meta-dim" data-testid={`v2-integration-off-${g.id}`}>
                      disabled
                    </span>
                  )}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
