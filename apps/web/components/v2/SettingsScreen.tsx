"use client";

// esbuild uses the classic JSX runtime here (no vitest config, tsconfig jsx=preserve),
// so a component rendered in a test must have React in scope -- see app/previews-ui.tsx.
import React from "react";

import type { SettingsView } from "../../lib/v2/platform";

/**
 * Settings — what is configured, and what this deployment simply does not have.
 *
 * THE HONESTY IS THE FEATURE
 * --------------------------
 * The reference has tabs for GitHub, SSO and Members, with forms behind each. This deployment
 * authenticates with an API key and models AGENT roles rather than human seats, so those forms
 * would be decorative.
 *
 * Rendering an empty SSO form implies SSO is configurable here and merely unconfigured. That is
 * a different and false claim, and a settings page full of controls that cannot act teaches the
 * operator that the console is decorative — which then applies to the controls that DO work.
 */

export function SettingsScreen({ view }: { view: SettingsView }) {
  if (!view.apiReachable) {
    return (
      <section className="v2-screen" data-testid="v2-settings-unreachable">
        <h1 className="v2-screen-title">Settings</h1>
        <p className="v2-empty">
          Could not reach the control plane
          {view.blockedReason ? ` (${view.blockedReason})` : ""}. Nothing here reflects your
          configuration.
        </p>
      </section>
    );
  }

  return (
    <section className="v2-screen" data-testid="v2-settings">
      <header className="v2-screen-head">
        <h1 className="v2-screen-title">Settings</h1>
        <p className="v2-screen-sub">what this company is configured with</p>
      </header>

      <h2 className="v2-ws-h">Agent roles</h2>
      {view.roles.length === 0 ? (
        <p className="v2-empty" data-testid="v2-settings-no-roles">
          No agent roles are defined for this company yet.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-settings-roles">
          {view.roles.map((r) => (
            <li className="v2-row" key={r.key} data-testid={`v2-role-${r.key}`}>
              <span className="v2-row-main">
                <span className="v2-row-title">{r.name}</span>
                <span className="v2-row-meta">
                  <span className="v2-mono">{r.key}</span>
                  <span>autonomy {r.autonomy_level}</span>
                  <span>trust {r.trust_score}</span>
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}

      <h2 className="v2-ws-h">Repositories</h2>
      {view.repos.length === 0 ? (
        <p className="v2-empty" data-testid="v2-settings-no-repos">
          No code task has targeted a repository yet.
        </p>
      ) : (
        <ul className="v2-rows" data-testid="v2-settings-repos">
          {view.repos.map((r) => (
            <li className="v2-row" key={r.name} data-testid={`v2-repo-${r.name}`}>
              <span className="v2-row-main">
                <span className="v2-row-title v2-mono">{r.name}</span>
                <span className="v2-row-meta">
                  <span>
                    <b>{r.runs}</b> runs
                  </span>
                  <span>
                    <b>{r.prs}</b> PRs opened
                  </span>
                  {r.last_run_at ? <span>last {r.last_run_at.slice(0, 10)}</span> : null}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}

      {/* NOT AN EMPTY FORM. Each of these is something the reference offers and this
          deployment does not have, said with the reason — because "unconfigured" and
          "impossible here" look identical as a blank form and mean different things. */}
      {view.unavailable.length > 0 ? (
        <>
          <h2 className="v2-ws-h">Not on this deployment</h2>
          <ul className="v2-rows" data-testid="v2-settings-unavailable">
            {view.unavailable.map((u) => (
              <li className="v2-row" key={u.id} data-testid={`v2-unavailable-${u.id}`}>
                <span className="v2-row-main">
                  <span className="v2-row-title">{u.label}</span>
                  <span className="v2-row-desc">{u.reason}</span>
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
