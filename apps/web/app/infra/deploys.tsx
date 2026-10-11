"use client";

/**
 * Infra › Deploys (spec §4.6): every deploy, newest first — stack, registry and delivery in one
 * rail, grouped by day, each row saying its state, what it shipped, where, who approved it, its
 * steps and the one thing you can do to it. The URL is the state: `state=` (All · Needs you ·
 * Live · Failed), `app=`, `env=`, `deploy=<ref>` (the drawer). One poll (`useDeploys`: 8 s while
 * anything moves, 30 s otherwise); `Show older` pages by the server's cursor. `+ Deploy` is the
 * one way to plan a deploy; `⋯` holds Rules, CI config and Secrets. The approval card itself is
 * the home's NEEDS YOU (one card, every screen), so it is not drawn twice here.
 */
import React, { useEffect, useState } from "react";

import { deploysUrl, parseDeployList, useDeploys, type DeployRow } from "../../lib/infra-view";
import { CiSheet } from "./ci-sheet";
import { DeployDrawer } from "./deploy-drawer";
import { DeployRail } from "./deploy-row";
import { DeploySheet } from "./deploy-sheet";
import type { InfraTabProps } from "./infra-home";
import { ErrorLine, Menu } from "./infra-util";
import { Empty, Skeleton } from "./kit";
import { RulesSheet } from "./rules-sheet";
import { SecretsSheet } from "./secrets-sheet";
import { ChangeSetReleases, DeployPlanView } from "../multirepo/deploy-plan";
import "./deploys.css";

const CHIPS: { key: string | null; label: string; count: "needsYou" | "live" | "failed" | null }[] = [
  { key: null, label: "All", count: null }, { key: "needs_you", label: "Needs you", count: "needsYou" },
  { key: "live", label: "Live", count: "live" }, { key: "failed", label: "Failed", count: "failed" },
];

/** Find narrows the rows on screen by app, env, sha, host or account. */
function matches(row: DeployRow, q: string): boolean {
  if (!q.trim()) return true;
  const hay = [row.app, row.env, row.sha7, row.refName, row.host?.name, row.account, row.word].filter(Boolean).join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w));
}

export function DeploysTab({ projectId, actor, url, go, query }: InfraTabProps) {
  const filter = { state: url.state, app: url.app, env: url.env };
  const list = useDeploys(projectId, { ...filter, viewer: actor });
  const [older, setOlder] = useState<DeployRow[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [olderError, setOlderError] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [sheet, setSheet] = useState<null | "deploy" | "rules" | "ci" | "secrets">(null);

  // A new filter is a new list: forget the pages fetched under the old one.
  const key = `${url.state ?? ""}|${url.app ?? ""}|${url.env ?? ""}`;
  useEffect(() => { setOlder([]); setCursor(null); setOlderError(null); }, [key]);

  const data = list.data;
  const next = cursor ?? data?.nextCursor ?? null;
  const showOlder = async () => {
    if (!next) return;
    setLoadingOlder(true); setOlderError(null);
    try {
      const r = await fetch(deploysUrl(projectId, { ...filter, cursor: next, viewer: actor }), { cache: "no-store" });
      if (!r.ok) { setOlderError(`The API answered ${r.status}.`); return; }
      const page = parseDeployList(await r.json());
      setOlder((o) => [...o, ...page.rows.filter((x) => !o.some((y) => y.ref === x.ref))]);
      setCursor(page.nextCursor ?? "");
    } catch (e) {
      setOlderError(e instanceof Error ? e.message : "The API did not answer.");
    } finally {
      setLoadingOlder(false);
    }
  };

  const seen = new Set((data?.rows ?? []).map((r) => r.ref));
  const rows = [...(data?.rows ?? []), ...older.filter((r) => !seen.has(r.ref))].filter((r) => matches(r, query));
  const open = (ref: string) => go({ deploy: ref });
  const filtered = !!(url.state || url.app || url.env);
  const more = cursor === "" ? null : next;

  // A change across repositories: its plan, one release with one rollback (`changeset=<id>`).
  if (url.changeset) {
    return (
      <div className="inf-deploys" data-testid="inf-deploys">
        <DeployPlanView projectId={projectId} changeSetId={url.changeset} onBack={() => go({ changeset: null })} />
      </div>
    );
  }

  return (
    <div className="inf-deploys" data-testid="inf-deploys">
      <ChangeSetReleases projectId={projectId} onOpen={(id) => go({ changeset: id, deploy: null })} />
      <div className="inf-dep-bar" role="toolbar" aria-label="Deploy filters">
        <span className="inf-seg" role="radiogroup" aria-label="Show">
          {CHIPS.map((c) => {
            const n = c.count && data ? data.counts[c.count] : 0;
            return (
              <button key={c.label} type="button" role="radio" aria-checked={(url.state ?? null) === c.key} className="inf-seg-btn"
                      data-testid={`inf-deploys-chip-${c.key ?? "all"}`} onClick={() => go({ state: c.key, deploy: null }, "replace")}>
                {c.label}{n ? <span className="inf-tab-count"> {n}</span> : null}
              </button>
            );
          })}
        </span>
        <label className="inf-dep-filter"><span className="ag-sr">App</span>
          <select value={url.app ?? ""} data-testid="inf-deploys-app" onChange={(e) => go({ app: e.target.value || null }, "replace")}>
            <option value="">App</option>
            {(data?.filters.apps ?? []).map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <label className="inf-dep-filter"><span className="ag-sr">Env</span>
          <select value={url.env ?? ""} data-testid="inf-deploys-env" onChange={(e) => go({ env: e.target.value || null }, "replace")}>
            <option value="">Env</option>
            {(data?.filters.envs ?? []).map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
        <span className="ag-spacer" />
        <button type="button" className="ag-btn ag-btn-primary" data-testid="inf-deploy-new" onClick={() => setSheet("deploy")}>+ Deploy</button>
        <Menu label="⋯" testId="inf-deploys-more" tip="Rules, CI config, secrets" items={[
          { key: "rules", label: "Rules", testId: "inf-deploys-rules", tip: "What each environment allows", onPick: () => setSheet("rules") },
          { key: "ci", label: "CI config", testId: "inf-deploys-ci", tip: "Generate and check CI pipelines", onPick: () => setSheet("ci") },
          { key: "secrets", label: "Secrets", testId: "inf-deploys-secrets", tip: "Secrets deploys can use", onPick: () => setSheet("secrets") },
        ]} />
      </div>

      {list.loading ? <Skeleton testId="inf-deploys-loading" /> : null}
      {!data && list.error ? <ErrorLine error={list.error} testId="inf-deploys-error" /> : null}
      {data && rows.length === 0 ? (
        filtered || query ? <Empty testId="inf-deploys-none">No deploys match</Empty> : (
          <Empty testId="inf-deploys-empty">
            <span className="inf-actions">No deploys yet
              <button type="button" className="ag-btn" onClick={() => setSheet("deploy")}>+ Deploy</button></span>
          </Empty>
        )
      ) : null}
      {rows.length ? (
        <DeployRail rows={rows} projectId={projectId} actor={actor} onOpen={open} onChanged={list.refresh} />
      ) : null}
      {data && more ? (
        <div className="inf-actions inf-dep-older">
          <button type="button" className="ag-btn" disabled={loadingOlder} data-testid="inf-deploys-older" onClick={() => void showOlder()}>
            {loadingOlder ? "Loading…" : "Show older"}
          </button>
          <ErrorLine error={olderError} />
        </div>
      ) : null}

      {url.deploy ? (
        <DeployDrawer projectId={projectId} actor={actor} deployRef={url.deploy} onClose={() => go({ deploy: null })}
                      onOpen={open} onChanged={list.refresh} />
      ) : null}
      {sheet === "deploy" ? (
        <DeploySheet projectId={projectId} actor={actor} onClose={() => setSheet(null)}
                     onPlanned={(ref) => { list.refresh(); if (ref) open(ref); }} />
      ) : null}
      {sheet === "rules" ? <RulesSheet projectId={projectId} actor={actor} onClose={() => setSheet(null)} /> : null}
      {sheet === "ci" ? <CiSheet projectId={projectId} onClose={() => setSheet(null)} /> : null}
      {sheet === "secrets" ? <SecretsSheet projectId={projectId} actor={actor} onClose={() => setSheet(null)} /> : null}
    </div>
  );
}
