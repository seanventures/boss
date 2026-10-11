"use client";

/**
 * The small parts every multi-repo screen draws the same way: a repository's mark and name, its
 * language, its status pill, the Read · Write · PRs checks, a "?" tip, and the component rows
 * (component → repository, build order) the composer and App › Components both edit.
 */
import React from "react";
import { ArrowRight, GithubLogo, Sparkle, X } from "@phosphor-icons/react";

import {
  buildOrder, langColor, stateWords, type ComponentKind, type ProjectRepo, type RegistryRepo,
} from "../../lib/multirepo";

export function TipQ({ tip }: { tip: string }) {
  return <span className="mr-ui-tipq" title={tip} aria-label={tip} role="img">?</span>;
}

export function GhMark({ size = "md", muted = false }: { size?: "md" | "sm"; muted?: boolean }) {
  return (
    <span className={`mr-ui-gh${size === "sm" ? " sm" : ""}${muted ? " muted" : ""}`} aria-hidden>
      <GithubLogo size={size === "sm" ? 13 : 16} weight="bold" />
    </span>
  );
}

export function Lang({ lang }: { lang?: string | null }) {
  if (!lang) return <span className="mr-ui-lang mr-ui-dim">—</span>;
  return <span className="mr-ui-lang"><i style={{ background: langColor(lang) }} aria-hidden />{lang}</span>;
}

export function RepoName({ name, sub, muted }: { name: string; sub?: React.ReactNode; muted?: boolean }) {
  return (
    <div className="mr-ui-repo">
      <GhMark muted={muted} />
      <div><b data-user-text title={name}>{name}</b>{sub ? <small>{sub}</small> : null}</div>
    </div>
  );
}

export function StatePill({ state, testId }: { state?: string | null; testId?: string }) {
  const w = stateWords(state);
  return <span className="mr-ui-pill" data-tone={w.tone} title={w.tip || undefined} data-testid={testId}>{w.label}</span>;
}

const CHECK_TIPS = {
  read: "Can see the repository and its branches.",
  write: "Can push a boss/* branch. Never the default branch.",
  pull_requests: "Can open a pull request for you to review.",
} as const;
const CHECK_LABELS = { read: "Read", write: "Write", pull_requests: "PRs" } as const;

export function Checks({ repo }: { repo: RegistryRepo }) {
  return (
    <>
      {(["read", "write", "pull_requests"] as const).map((k) => {
        const ok = repo.checks?.[k]?.ok;
        const mark = ok === true ? "✓" : ok === false ? "✗" : "–";
        return (
          <span key={k} className="mr-ui-check" data-ok={ok === true ? "yes" : ok === false ? "no" : undefined}
                title={CHECK_TIPS[k]} data-testid={`mr-check-${repo.id}-${k}`}>
            {mark} {CHECK_LABELS[k]}
          </span>
        );
      })}
    </>
  );
}

/** One row of a component map, as the composer and App › Components edit it. */
export type MapRow = {
  key: string; name: string; binding_id: string; path?: string; kind: ComponentKind;
  depends_on: string[]; reason?: string | null; ship_target?: unknown;
};

let seq = 0;
export const rowKey = () => `row${++seq}`;

/**
 * Component → repository rows with their build order: #1, #2… for code, "deploy" for deploy config.
 * `withPath`/`withNeeds` add the folder and the dependencies (App › Components); the composer
 * shows only what the suggestions filled in.
 */
export function ComponentRows({ rows, repos, onChange, disabled, withPath = false, withNeeds = false, testPrefix = "mr-map" }: {
  rows: MapRow[]; repos: ProjectRepo[]; onChange: (next: MapRow[]) => void; disabled?: boolean;
  withPath?: boolean; withNeeds?: boolean; testPrefix?: string;
}) {
  const order = buildOrder(rows);
  const set = (key: string, patch: Partial<MapRow>) =>
    onChange(rows.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const rename = (key: string, name: string) => {
    const old = rows.find((r) => r.key === key)?.name ?? "";
    // A renamed component keeps the components that depend on it.
    onChange(rows.map((r) => (r.key === key ? { ...r, name }
      : { ...r, depends_on: r.depends_on.map((d) => (d === old ? name : d)) })));
  };
  const remove = (key: string) => {
    const gone = rows.find((r) => r.key === key)?.name ?? "";
    onChange(rows.filter((r) => r.key !== key).map((r) => ({ ...r, depends_on: r.depends_on.filter((d) => d !== gone) })));
  };
  return (
    <div className="mr-map" data-testid={`${testPrefix}-rows`}>
      <div className="mr-map-cols" aria-hidden>
        <span>Component <TipQ tip="A part of the app with its own repository (or folder), e.g. frontend or backend." /></span>
        <span />
        <span>Repository</span>
        {withPath ? <span>Path</span> : null}
        <span>Order</span>
      </div>
      {rows.map((r, i) => {
        const repo = repos.find((x) => x.binding_id === r.binding_id);
        const n = order.get(r.name.trim());
        const others = rows.filter((o) => o.key !== r.key && o.kind === "code" && o.name.trim());
        return (
          <div key={r.key} className="mr-map-row" data-testid={`${testPrefix}-row-${i}`}>
            <input className="mr-ui-input" value={r.name} disabled={disabled} aria-label={`Component ${i + 1} name`}
                   placeholder="Component" data-testid={`${testPrefix}-name-${i}`}
                   onChange={(e) => rename(r.key, e.target.value)} />
            <span className="mr-map-arrow" aria-hidden><ArrowRight size={13} /></span>
            <div className="mr-map-repo">
              <select className="mr-ui-select mono" value={r.binding_id} disabled={disabled}
                      aria-label={`Repository for ${r.name || `component ${i + 1}`}`}
                      data-testid={`${testPrefix}-repo-${i}`}
                      onChange={(e) => set(r.key, { binding_id: e.target.value, reason: null })}>
                <option value="">Choose a repository</option>
                {repos.map((x) => (
                  <option key={x.binding_id} value={x.binding_id}>
                    {x.full_name}{x.language ? ` · ${x.language}` : ""}
                  </option>
                ))}
              </select>
              {r.reason ? (
                <span className="mr-map-sugg" data-testid={`${testPrefix}-sugg-${i}`}>
                  <Sparkle size={11} weight="fill" aria-hidden />{r.reason}
                </span>
              ) : null}
              {withNeeds && r.kind === "code" && others.length ? (
                <span className="mr-map-needs" data-testid={`${testPrefix}-needs-${i}`}>
                  Needs
                  {others.map((o) => {
                    const on = r.depends_on.includes(o.name.trim());
                    return (
                      <button key={o.key} type="button" className="mr-ui-chip static" aria-pressed={on} disabled={disabled}
                              title={on ? `Builds after ${o.name}` : `Build after ${o.name}`}
                              onClick={() => set(r.key, { depends_on: on ? r.depends_on.filter((d) => d !== o.name.trim())
                                : [...r.depends_on, o.name.trim()] })}>
                        {o.name}
                      </button>
                    );
                  })}
                </span>
              ) : null}
              {withPath && repo === undefined && r.binding_id ? (
                <span className="mr-ui-error">That repository is no longer in this project.</span>
              ) : null}
            </div>
            {withPath ? (
              <input className="mr-ui-input mono mr-map-path" value={r.path ?? "/"} disabled={disabled}
                     aria-label={`Folder for ${r.name || `component ${i + 1}`}`} title="Folder inside the repository. Lets a monorepo hold several components."
                     data-testid={`${testPrefix}-path-${i}`} onChange={(e) => set(r.key, { path: e.target.value })} />
            ) : null}
            <span className="mr-map-end">
              {r.kind === "deploy_config" ? (
                <span className="mr-map-ord" data-kind="deploy" data-testid={`${testPrefix}-ord-${i}`}
                      title="Deploy config: read only. BOSS reads the deploy plan here; it never builds or changes it.">deploy</span>
              ) : (
                <span className="mr-map-ord" data-testid={`${testPrefix}-ord-${i}`}
                      title="Build order. A component builds after the ones it needs.">{n ? `#${n}` : "—"}</span>
              )}
              {withPath ? (
                <select className="mr-ui-select" value={r.kind} disabled={disabled} aria-label={`Kind of ${r.name || `component ${i + 1}`}`}
                        data-testid={`${testPrefix}-kind-${i}`} title="Code is built; deploy config is read only"
                        onChange={(e) => set(r.key, { kind: e.target.value as ComponentKind, depends_on: e.target.value === "code" ? r.depends_on : [] })}>
                  <option value="code">Code</option>
                  <option value="deploy_config">Deploy config</option>
                </select>
              ) : null}
              <button type="button" className="mr-map-x" aria-label={`Remove ${r.name || `component ${i + 1}`}`}
                      title="Remove this component" disabled={disabled} data-testid={`${testPrefix}-remove-${i}`}
                      onClick={() => remove(r.key)}><X size={12} weight="bold" aria-hidden /></button>
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** The rows the API takes: blank rows are UI; deploy config is always read. */
export function rowsToInput(rows: MapRow[]) {
  return rows.filter((r) => r.name.trim() && r.binding_id).map((r) => ({
    name: r.name.trim(), binding_id: r.binding_id,
    ...(r.path && r.path !== "/" ? { path: r.path } : {}),
    kind: r.kind, access: r.kind === "deploy_config" ? "read" as const : "write" as const,
    depends_on: r.kind === "code" ? r.depends_on : [],
    ...(r.ship_target ? { ship_target: r.ship_target as never } : {}),
  }));
}
