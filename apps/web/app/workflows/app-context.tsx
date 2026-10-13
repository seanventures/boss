"use client";

/** What "Add a feature" builds onto, in full, before it asks for the change.
 *
 *  The composer said the app's name, repo and build count. The operator asked for "all the
 *  project details including the settings" first: where the app runs and whether it is healthy,
 *  what is configured, which connectors it carries, and its branch and PR. Drawn as the approved
 *  mockup's context card (AddFeature.dc.html, 2026-10-13): three tiles (Live, Connectors,
 *  Settings set), the rest behind Details. Secret and setting NAMES only, never a value. */
import React, { useEffect, useState } from "react";

import { useDeploys, type DeployRow } from "../../lib/infra-view";
import { localFull } from "../../lib/local-time";
import { templatesApi, type AppSecrets, type Applied } from "../../lib/templates";
import type { AppRow } from "../factory/apps-tab";

/** The deploy that says where the app runs now: the live one, else the newest. */
export function currentDeploy(rows: DeployRow[], app: string): DeployRow | null {
  const mine = rows.filter((r) => r.app === app);
  return mine.find((r) => r.phase === "live") ?? mine[0] ?? null;
}

/** The names that are set, per environment, from the app's secrets view. */
export function settingNames(s: AppSecrets | null): { deploy: string[]; other: string[] } {
  if (!s) return { deploy: [], other: [] };
  const deploy = new Set<string>(s.deployValues);
  const other = new Set<string>();
  for (const b of s.bindings) (b.env === "deploy" ? deploy : other).add(b.name);
  for (const n of deploy) other.delete(n);
  return { deploy: [...deploy].sort(), other: [...other].sort() };
}

/** The context card's tiles and its Details fold; the card's header is the composer's. */
export function AppContext({ projectId, app }: { projectId: string; app: AppRow }) {
  const { data } = useDeploys(projectId, { app: app.app });
  const [secrets, setSecrets] = useState<AppSecrets | null>(null);
  const [secretsWhy, setSecretsWhy] = useState("");
  const [applied, setApplied] = useState<Applied[] | null>(null);

  useEffect(() => {
    let live = true;
    const key = app.appKey;
    if (!key) { setSecrets(null); setApplied([]); return undefined; }
    void templatesApi.secrets(projectId, key)
      .then((s) => { if (live) { setSecrets(s); setSecretsWhy(""); } },
            () => { if (live) setSecretsWhy("Hidden from your role"); });
    void templatesApi.applied(projectId, key)
      .then((a) => { if (live) setApplied(a); }, () => { if (live) setApplied([]); });
    return () => { live = false; };
  }, [projectId, app.appKey]);

  const dep = currentDeploy(data?.rows ?? [], app.app);
  const names = settingNames(secrets);
  const allNames = [...names.deploy, ...names.other];
  const templates = (applied ?? []).filter((a) => a.status !== "abandoned");
  const healthy = dep ? dep.phase === "live" : false;

  const where = dep
    ? `${dep.word} · ${dep.env}${dep.host ? ` on ${dep.host.name}` : ""}`
    : app.deploys.length ? `${app.deploys[0].word ?? "Deployed"} · ${app.deploys[0].env}` : "Not deployed";

  return (
    <>
      <div className="np-ctx-tiles" data-testid="np-app-context">
        <div className="np-ctx-tile">
          <small>Live</small>
          <span data-testid="np-ctx-live" data-user-text>
            <i className="np-ctx-dot" data-ok={healthy || undefined} aria-hidden />{where}
          </span>
        </div>
        <div className="np-ctx-tile">
          <small title="Connectors and the starter it was built from">Connectors</small>
          <span data-testid="np-ctx-templates" data-user-text>
            {applied === null ? "Loading…"
              : templates.length ? templates.map((t) => `${t.name} v${t.version}`).join(", ") : "None"}
          </span>
        </div>
        <div className="np-ctx-tile">
          <small title="Names only: a value is never shown">Settings set</small>
          <span data-testid="np-ctx-settings" title={names.deploy.length || names.other.length
            ? `Deploy: ${names.deploy.join(", ") || "none"}. Build and preview: ${names.other.join(", ") || "none"}.`
            : undefined}>
            {secretsWhy ? <span className="np-dim">{secretsWhy}</span>
              : !secrets ? <span className="np-dim">Loading…</span>
              : !allNames.length ? "None set"
              : <code>{allNames.join(", ")}</code>}
          </span>
        </div>
      </div>
      <details className="np-ctx">
        <summary data-testid="np-app-context-summary">
          Details
          {dep?.openUrl ? (
            <a href={dep.openUrl} target="_blank" rel="noreferrer" className="np-ctx-open"
               data-testid="np-app-context-open" onClick={(e) => e.stopPropagation()}>Open app ↗</a>
          ) : null}
        </summary>
        <dl className="np-ctx-grid">
          <dt>Runs</dt>
          <dd data-testid="np-ctx-deploy">
            {dep ? (
              <>
                <span data-user-text>{dep.sentence || dep.word}</span>
                {dep.finishedAt ? <span className="np-dim"> · last deploy {localFull(dep.finishedAt)}</span> : null}
              </>
            ) : "Not deployed yet"}
          </dd>
          <dt>Code</dt>
          <dd data-testid="np-ctx-code" data-user-text>
            {[app.repo, app.prNumber ? `PR #${app.prNumber}${app.prState ? ` (${app.prState})` : ""}` : "no PR yet"]
              .filter(Boolean).join(" · ")}
          </dd>
        </dl>
      </details>
    </>
  );
}
