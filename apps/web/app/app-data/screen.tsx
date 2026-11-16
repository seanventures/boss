"use client";

/**
 * Data for one built app: drawer tabs for its tables, a filter, a CSV import,
 * and a trash that holds a row for 30 days.
 */
import React, { useCallback, useEffect, useState } from "react";

const CSS = `
.ad1266 {
  background: #D5E2E8;
  color: #1C2B33;
  padding: 1.5rem 1.25rem 2rem;
  font-family: "Avenir Next", "Segoe UI", sans-serif;
  min-height: 100%;
}
.ad1266-kicker {
  margin: 0;
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-size: 0.72rem;
  color: #245E6E;
}
.ad1266 h1 {
  margin: 0.15rem 0 1rem;
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  font-weight: 700;
  font-size: 2.4rem;
  letter-spacing: 0.01em;
  line-height: 0.95;
}
.ad1266-tabs {
  display: flex;
  gap: 0.35rem;
  align-items: flex-end;
  flex-wrap: wrap;
  margin: 0 0.4rem;
}
.ad1266-tab {
  border: 0;
  background: #E7F0F3;
  color: #1C2B33;
  clip-path: polygon(10px 0, calc(100% - 10px) 0, 100% 100%, 0 100%);
  padding: 0.5rem 1.15rem 0.45rem;
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-size: 0.78rem;
  cursor: pointer;
}
.ad1266-tab[aria-selected="true"] {
  background: #F7FBFC;
  color: #245E6E;
  box-shadow: inset 0 3px 0 #C4A15A;
}
.ad1266-tab.ad1266-trash[aria-selected="true"] {
  box-shadow: inset 0 3px 0 #A33B32;
  color: #A33B32;
}
.ad1266-drawer {
  background: #F7FBFC;
  border: 1px solid #C3D0D6;
  padding: 1rem;
}
.ad1266-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1rem;
  align-items: end;
  margin-bottom: 1rem;
}
.ad1266 label {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-size: 0.68rem;
  color: #245E6E;
}
.ad1266 input, .ad1266 select, .ad1266 textarea {
  font-family: "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 0.85rem;
  color: #1C2B33;
  background: #fff;
  border: 1px solid #C3D0D6;
  padding: 0.35rem 0.45rem;
  text-transform: none;
  letter-spacing: 0;
}
.ad1266 textarea { min-width: 16rem; min-height: 4.5rem; }
.ad1266-go, .ad1266-quiet {
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
}
.ad1266-go {
  background: #245E6E;
  color: #F7FBFC;
  border: 0;
  padding: 0.45rem 0.7rem;
}
.ad1266-quiet {
  background: transparent;
  color: #A33B32;
  border: 1px solid #A33B32;
  padding: 0.25rem 0.45rem;
}
.ad1266 table { width: 100%; border-collapse: collapse; font-family: "SF Mono", ui-monospace, Menlo, monospace; font-size: 0.86rem; }
.ad1266 th {
  text-align: left;
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-size: 0.68rem;
  color: #245E6E;
  border-bottom: 1px solid #C3D0D6;
  padding: 0.35rem 0.4rem;
}
.ad1266 td { padding: 0.45rem 0.4rem; border-bottom: 1px solid #E1EAEE; vertical-align: middle; }
.ad1266-stamp {
  display: inline-block;
  border: 2px solid #A33B32;
  color: #A33B32;
  font-family: "DIN Alternate", "Avenir Next Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-weight: 700;
  padding: 0.05rem 0.35rem;
  transform: rotate(-8deg);
  margin-right: 0.4rem;
}
.ad1266-rule { max-width: 36rem; line-height: 1.45; }
.ad1266 :focus-visible { outline: 2px solid #245E6E; outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) {
  .ad1266-stamp { transform: none; }
}
@media (max-width: 640px) {
  .ad1266 h1 { font-size: 1.8rem; }
  .ad1266-tools { flex-direction: column; align-items: stretch; }
  .ad1266 table { display: block; overflow-x: auto; }
}
`;

type Cells = Record<string, string>;

type DataRow = {
  id: string;
  cells: Cells;
  table?: string;
  days_left?: number;
  restorable?: boolean;
};

type DataPage = {
  app_key: string;
  trash_days: number;
  tables: { name: string; live_count: number }[];
  table: string;
  columns: string[];
  rows: DataRow[];
  trash: DataRow[];
};

type AppSummary = { app_key: string; tables: number; live_rows: number; trash_rows: number };

function queryValue(name: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  const body = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "The data page failed.";
    throw new Error(detail);
  }
  return body;
}

function heldLabel(days: number, restorable: boolean): string {
  if (!restorable) return "Too old";
  if (days <= 0) return "Last day";
  if (days === 1) return "1 day";
  return `${days} days`;
}

function Shell({ title, kicker, children }: { title: string; kicker: string; children: React.ReactNode }) {
  return (
    <section className="ad1266" data-testid="app-data" aria-label="Data">
      <style>{CSS}</style>
      <p className="ad1266-kicker">{kicker}</p>
      <h1>{title}</h1>
      {children}
    </section>
  );
}

function AppPicker({ projectId, onOpen }: { projectId: string; onOpen: (appKey: string) => void }) {
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let stop = false;
    (async () => {
      try {
        const response = await fetch(`/v1/apps/data?project_id=${encodeURIComponent(projectId)}`);
        const body = await readJson(response);
        if (!stop) setApps((body.apps as AppSummary[]) ?? []);
      } catch (err) {
        if (!stop) setError(err instanceof Error ? err.message : "The app list failed.");
      }
    })();
    return () => { stop = true; };
  }, [projectId]);

  return (
    <>
      <p>Open a built app. Each one keeps its own tables.</p>
      {error ? <p role="alert">{error}</p> : null}
      <div className="ad1266-tabs">
        {apps.map((app) => (
          <button type="button" className="ad1266-tab" key={app.app_key} onClick={() => onOpen(app.app_key)}>
            {app.app_key}
          </button>
        ))}
      </div>
      <form className="ad1266-tools" onSubmit={(event) => {
        event.preventDefault();
        const key = String(new FormData(event.currentTarget).get("app") ?? "").trim();
        if (key) onOpen(key);
      }}>
        <label>
          App
          <input name="app" />
        </label>
        <button type="submit" className="ad1266-go">Open</button>
      </form>
    </>
  );
}

export function AppDataScreen({ projectId, appKey, appName }: {
  projectId?: string;
  appKey?: string;
  appName?: string;
}) {
  const project = projectId || queryValue("project_id");
  const [chosen, setChosen] = useState(appKey ?? "");
  const app = appKey || chosen || queryValue("app");
  const title = appName || app || "Data";
  const [page, setPage] = useState<DataPage | null>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState<"rows" | "trash">("rows");

  const load = useCallback(async (extra?: { table?: string; field?: string; value?: string; mode?: string }) => {
    const params = new URLSearchParams({ project_id: project });
    if (extra?.table) params.set("table", extra.table);
    if (extra?.field) params.set("field", extra.field);
    if (extra?.value) params.set("value", extra.value);
    if (extra?.mode) params.set("mode", extra.mode);
    const response = await fetch(`/v1/apps/${encodeURIComponent(app)}/data?${params.toString()}`);
    const body = await readJson(response);
    setPage(body as unknown as DataPage);
    setError("");
  }, [project, app]);

  useEffect(() => {
    if (!project || !app) return;
    let stop = false;
    (async () => {
      try {
        const params = new URLSearchParams({ project_id: project });
        const response = await fetch(`/v1/apps/${encodeURIComponent(app)}/data?${params.toString()}`);
        const body = await readJson(response);
        if (!stop) {
          setPage(body as unknown as DataPage);
          setError("");
        }
      } catch (err) {
        if (!stop) setError(err instanceof Error ? err.message : "The data page failed.");
      }
    })();
    return () => { stop = true; };
  }, [project, app]);

  async function mutate(path: string, body: unknown) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    await readJson(response);
    await load();
  }

  if (!project) {
    return <Shell title="Data" kicker="Built app"><p>Pick a project.</p></Shell>;
  }
  if (!app) {
    return (
      <Shell title="Data" kicker="Built app">
        <AppPicker projectId={project} onOpen={setChosen} />
      </Shell>
    );
  }
  if (!page && !error) {
    return <Shell title={title} kicker="Built app"><p>Reading</p></Shell>;
  }

  const columns = page?.columns?.length
    ? page.columns
    : Object.keys(page?.rows[0]?.cells ?? {});

  return (
    <Shell title={title} kicker="Built app">
      {error ? <p role="alert">{error}</p> : null}
      <div className="ad1266-tabs" role="tablist">
        {(page?.tables ?? []).map((table) => (
          <button
            type="button"
            className="ad1266-tab"
            key={table.name}
            role="tab"
            aria-selected={view === "rows" && page?.table === table.name}
            onClick={() => { setView("rows"); void load({ table: table.name }); }}
          >
            {table.name}
          </button>
        ))}
        <button
          type="button"
          className="ad1266-tab ad1266-trash"
          data-testid="trash-tab"
          role="tab"
          aria-selected={view === "trash"}
          onClick={() => setView("trash")}
        >
          Trash
        </button>
      </div>
      <div className="ad1266-drawer">
        {view === "trash" ? (
          <>
            <p className="ad1266-rule">
              A deleted row stays for 30 days. Restore it until then. The purge never touches a live row.
            </p>
            {(page?.trash ?? []).length === 0 ? <p>Trash is empty.</p> : null}
            <table>
              <tbody>
                {(page?.trash ?? []).map((row) => (
                  <tr key={row.id} data-testid={`trash-row-${row.id}`}>
                    <td><span className="ad1266-stamp">Held</span>{heldLabel(row.days_left ?? 0, row.restorable !== false)}</td>
                    {Object.values(row.cells).map((cell, index) => <td key={`${row.id}-${index}`}>{cell}</td>)}
                    <td>
                      {row.restorable !== false ? (
                        <button
                          type="button"
                          className="ad1266-quiet"
                          data-testid={`restore-${row.id}`}
                          onClick={() => {
                            void mutate(
                              `/v1/apps/${encodeURIComponent(app)}/data/rows/${encodeURIComponent(row.id)}/restore`,
                              { project_id: project },
                            ).then(() => setView("rows"));
                          }}
                        >
                          Restore
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <>
            <form className="ad1266-tools" onSubmit={(event) => {
              event.preventDefault();
              const data = new FormData(event.currentTarget);
              void load({
                table: page?.table,
                field: String(data.get("field") ?? ""),
                value: String(data.get("value") ?? ""),
                mode: String(data.get("mode") ?? "is"),
              }).catch((err: unknown) => setError(err instanceof Error ? err.message : "The filter failed."));
            }}>
              <label>
                Field
                <input name="field" data-testid="filter-field" />
              </label>
              <label>
                Match
                <select name="mode" data-testid="filter-mode" defaultValue="is">
                  <option value="is">is</option>
                  <option value="contains">contains</option>
                </select>
              </label>
              <label>
                Value
                <input name="value" data-testid="filter-value" />
              </label>
              <button type="submit" className="ad1266-go" data-testid="apply-filter">Show matches</button>
            </form>
            <form className="ad1266-tools" onSubmit={(event) => {
              event.preventDefault();
              const data = new FormData(event.currentTarget);
              const table = String(data.get("table") ?? "");
              const csv = String(data.get("csv") ?? "");
              void mutate(
                `/v1/apps/${encodeURIComponent(app)}/data/import`,
                { project_id: project, table, csv },
              ).catch((err: unknown) => setError(err instanceof Error ? err.message : "The import failed."));
            }}>
              <label>
                Table
                <input name="table" data-testid="import-table" defaultValue={page?.table ?? ""} />
              </label>
              <label>
                CSV
                <textarea name="csv" data-testid="import-text" />
              </label>
              <button type="submit" className="ad1266-go" data-testid="import-submit">Bring in</button>
            </form>
            {(page?.tables ?? []).length === 0 ? (
              <p>Bring in a CSV. The first row names the columns.</p>
            ) : null}
            {(page?.rows ?? []).length === 0 && (page?.tables ?? []).length > 0 ? <p>No rows match.</p> : null}
            <table>
              <thead>
                <tr>{columns.map((column) => <th key={column}>{column}</th>)}<th></th></tr>
              </thead>
              <tbody>
                {(page?.rows ?? []).map((row) => (
                  <tr key={row.id} data-testid={`row-${row.id}`}>
                    {columns.map((column) => <td key={column}>{row.cells[column] ?? ""}</td>)}
                    <td>
                      <button
                        type="button"
                        className="ad1266-quiet"
                        data-testid={`trash-${row.id}`}
                        onClick={() => {
                          void mutate(
                            `/v1/apps/${encodeURIComponent(app)}/data/rows/${encodeURIComponent(row.id)}/trash`,
                            { project_id: project },
                          );
                        }}
                      >
                        Move to trash
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </div>
    </Shell>
  );
}
