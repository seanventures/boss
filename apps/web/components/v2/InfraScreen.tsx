"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { InfraView, V2Target } from "../../lib/v2/platform";

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

/**
 * One deployed app: which machine it lands on, how the last deploy went, and a link to the
 * thing itself.
 *
 * The v1 CI/CD screen puts each app on a card with its pipeline and an "open app" link. v2 had
 * a flat list of rows that named a target and an ssh string, so the reader had to join app to
 * machine by eye and had no way to reach the running app at all -- the address was in the
 * database the whole time and was simply never selected.
 */
function TargetCard({ t }: { t: V2Target }) {
  const last = t.last_deploy;
  return (
    <article className="v2-deploy-card" data-retired={t.retired} data-testid={`v2-target-${t.id}`}>
      <header className="v2-deploy-card-head">
        <span className="v2-deploy-card-name">{t.app_name}</span>
        <span className="v2-deploy-env v2-mono" data-status={t.status}>
          {t.environment}
        </span>
        {/* Retired is not a failure and not a running app. Said in a word, not by colour
            alone. */}
        {t.retired ? (
          <span className="v2-wf-violations v2-mono" data-testid={`v2-target-retired-${t.id}`}>
            retired
          </span>
        ) : null}
      </header>

      <p className="v2-deploy-card-where v2-mono">
        <span className="v2-row-meta-dim">{t.repo}</span>
        {/* The machine, by NAME, not by ssh string — and only when it resolves to a registered
            host. An app filed under the wrong machine is worse than one filed under none. */}
        {t.host ? (
          <span data-testid={`v2-target-host-${t.id}`}> on {t.host}</span>
        ) : (
          <span className="v2-step-agent-gap" data-testid={`v2-target-nohost-${t.id}`}>
            {" "}on an unregistered host
          </span>
        )}
        <span className="v2-row-meta-dim"> · {t.kind} {t.ref}</span>
      </p>

      <div className="v2-deploy-state">
        {last ? (
          <span className="v2-deploy-last v2-mono" data-status={last.status}
                data-testid={`v2-target-last-${t.id}`}>
            last deploy {last.status}
            {last.commit ? ` · ${last.commit}` : ""}
            {/* Why, on the screen. "failed" on its own is another dead end. */}
            {last.failure_reason ? ` — ${last.failure_reason}` : ""}
          </span>
        ) : (
          <span className="v2-row-meta-dim v2-mono" data-testid={`v2-target-never-${t.id}`}>
            never deployed
          </span>
        )}

        {t.url ? (
          <a className="v2-deploy-url v2-mono" href={t.url} target="_blank" rel="noreferrer"
             data-testid={`v2-target-url-${t.id}`}>
            open app ↗
          </a>
        ) : (
          <span className="v2-row-meta-dim v2-mono" data-testid={`v2-target-nourl-${t.id}`}>
            no address recorded
          </span>
        )}
      </div>
    </article>
  );
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
        <ul className="v2-cards v2-host-cards" data-testid="v2-infra-hosts">
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

      <h2 className="v2-ws-h">Apps and where they deploy</h2>
      {view.targets.length === 0 ? (
        <p className="v2-empty" data-testid="v2-infra-no-targets">
          No app is bound to a destination yet.
        </p>
      ) : (
        <div className="v2-deploy-grid" data-testid="v2-infra-targets">
          {view.targets.map((t) => <TargetCard key={t.id} t={t} />)}
        </div>
      )}

      <h2 className="v2-ws-h">Models this month</h2>
      {view.models.length === 0 ? (
        <p className="v2-empty" data-testid="v2-infra-no-models">
          No model calls recorded this month.
        </p>
      ) : (
        // A TABLE, because these are four figures per model that only mean anything compared
        // DOWN the column. As stacked rows -- name on one line, figures on the next -- each
        // model cost two lines and no two numbers ever lined up.
        <table className="v2-figs" data-testid="v2-infra-models">
          <thead>
            <tr>
              <th>Model</th>
              <th className="v2-figs-num">Spend</th>
              <th className="v2-figs-num">Calls</th>
              <th className="v2-figs-num">Tokens</th>
            </tr>
          </thead>
          <tbody>
            {view.models.map((m) => (
              <tr key={m.model} data-testid={`v2-model-${m.model}`}>
                <td className="v2-mono v2-figs-name">
                  {m.model}
                  {/* A price the price map did not know is a guess, and it must not sit beside
                      measured figures looking like one. */}
                  {m.estimated ? (
                    <span className="v2-figs-flag" data-testid={`v2-model-estimated-${m.model}`}
                          title="not in the price map — this cost is estimated">
                      estimated
                    </span>
                  ) : null}
                </td>
                <td className="v2-figs-num"><b>{money(m.spend_usd)}</b></td>
                <td className="v2-figs-num">{m.calls.toLocaleString()}</td>
                <td className="v2-figs-num">{m.tokens.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="v2-ws-h">Integrations</h2>
      {view.integrations.length === 0 ? (
        <p className="v2-empty" data-testid="v2-infra-no-integrations">
          No MCP servers are configured.
        </p>
      ) : (
        // Small cards rather than full-width rows: an integration is four short facts, and a
        // row stretched across a 1700px screen puts a huge gap between the name and the tier
        // that qualifies it.
        <div className="v2-chip-grid" data-testid="v2-infra-integrations">
          {view.integrations.map((g) => (
            <div className="v2-chip-card" key={g.id} data-testid={`v2-integration-${g.id}`}
                 data-enabled={g.enabled} data-tier={g.risk_tier}>
              <span className="v2-chip-name">{g.name}</span>
              <span className="v2-chip-meta v2-mono">
                <span>{g.transport}</span>
                {/* Risk tier and data boundary decide what a server may touch. The name alone
                    makes a tier-3 external server look like a tier-1 internal one. */}
                <span data-testid={`v2-integration-risk-${g.id}`}>tier {g.risk_tier}</span>
                <span data-testid={`v2-integration-boundary-${g.id}`}>{g.data_boundary}</span>
              </span>
              {g.enabled ? (
                <span className="v2-chip-state" data-on="true">enabled</span>
              ) : (
                <span className="v2-chip-state" data-on="false"
                      data-testid={`v2-integration-off-${g.id}`}>disabled</span>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
