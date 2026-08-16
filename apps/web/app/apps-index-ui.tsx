"use client";

import { useMemo } from "react";

const PREVIEW_API = process.env.NEXT_PUBLIC_BOSS_API_URL ?? "";

/**
 * Apps — one row per thing BOSS has built, with its whole life on it.
 *
 * The console has always been able to show you RUNS. It has never been able to show you an
 * APP, which is what a person actually has in mind: the thing with a name, a spec, a pull
 * request, a preview you can click, and a cost. Reconstructing that meant opening four areas
 * and matching ids by eye — the exercise that produced "View ApproveIt" pointing at a failed
 * run last week.
 *
 * An app here is a LINEAGE, not a run. Retries, follow-ups and repairs share a root run id and
 * a branch, so they are attempts at one app rather than four different ones. Grouping by
 * lineage is what makes the newest working build findable instead of whichever row sorted
 * first.
 */

type Run = {
  id: string;
  title?: string;
  status?: string;
  phase?: string;
  pull_request_url?: string | null;
  spec?: { owner?: string; repo?: string; profile?: string } | null;
  finalize_ctx?: { root_run_id?: string } | null;
  created_at?: string;
  cost_usd_micros?: number;
};

type Preview = { id: string; run_id?: string | null; status?: string };

type App = {
  key: string;
  title: string;
  attempts: Run[];
  latest: Run;
  shipped?: Run;
  preview?: Preview;
  repo?: string;
  spend: number;
};

/** Strip the noise BOSS adds to a title so two attempts at one app read as one app. */
function appName(title: string): string {
  return title
    .replace(/^Follow-up: deferred review findings — /i, "")
    .replace(/^Web app — /i, "")
    .replace(/^QA benchmark: /i, "")
    .replace(/\s*·\s*retry \d+$/i, "")
    .replace(/^#\s*/, "")
    .split(/[\n–—]/)[0]
    .trim()
    .slice(0, 70) || "Untitled app";
}

function money(micros: number): string {
  return micros > 0 ? `$${(micros / 1_000_000).toFixed(2)}` : "—";
}

export function AppsIndex({
  projectId, runs, previews, meetings, benchmarks,
}: {
  projectId: string;
  runs: Run[];
  previews: Preview[];
  meetings: any[];
  benchmarks: any[];
}) {
  const apps = useMemo(() => groupIntoApps(runs, previews), [runs, previews]);

  if (apps.length === 0) {
    return (
      <div className="bx-empty">
        <h3>Nothing built yet</h3>
        <p>
          Apps appear here as soon as a run produces something. Start one from Discovery if you
          are still deciding, Meetings if you know the outcome, or Code Tasks if you already
          know the change.
        </p>
        <a className="cap-btn primary" href="?area=code_tasks">Start a build →</a>
      </div>
    );
  }

  return (
    <>
      <section className="ct-block">
        <div className="ct-block-title">{apps.length} app{apps.length === 1 ? "" : "s"}</div>
        <p className="detail" style={{ maxWidth: "64ch", marginTop: 4 }}>
          Everything BOSS has built here. Each row is one app — its attempts grouped together,
          its latest working build, and where to open it.
        </p>
      </section>

      <div style={{ display: "grid", gap: 12 }}>
        {apps.map((app) => (
          <AppCard key={app.key} app={app} />
        ))}
      </div>
    </>
  );
}

function AppCard({ app }: { app: App }) {
  const shipped = app.shipped;
  const live = app.preview && (app.preview.status === "running" || app.preview.status === "starting");
  const previewUrl = live
    ? `${PREVIEW_API}/v1/previews/${encodeURIComponent(app.preview!.id)}/web/`
    : null;

  return (
    <article className="ct-card">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <div>
          <h3 style={{ margin: 0, fontSize: "0.98rem" }}>{app.title}</h3>
          <p className="detail" style={{ margin: "4px 0 0" }}>
            {app.repo ? `${app.repo} · ` : ""}
            {app.attempts.length} attempt{app.attempts.length === 1 ? "" : "s"}
            {app.spend > 0 ? ` · ${money(app.spend)}` : ""}
          </p>
        </div>
        <StatusChip run={app.latest} shipped={Boolean(shipped)} />
      </div>

      {/* The life of the app, left to right. Each step is either reachable or plainly not
          there yet — the console's old failure was showing a control that led nowhere. */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 12 }}>
        {previewUrl ? (
          <a className="cap-btn primary" href={previewUrl} target="_blank" rel="noreferrer">
            Open the app →
          </a>
        ) : (
          <span className="detail">No preview running</span>
        )}
        {shipped?.pull_request_url ? (
          <a className="cap-btn" href={shipped.pull_request_url} target="_blank" rel="noreferrer">
            Pull request ↗
          </a>
        ) : null}
        <a className="cap-btn" href={`?area=code_tasks&run=${app.latest.id}`}>Change this app</a>
      </div>

      {app.attempts.length > 1 ? (
        <details style={{ marginTop: 10 }}>
          <summary className="detail">Attempts</summary>
          <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            {app.attempts.map((a) => (
              <li key={a.id} className="detail">
                {a.status} · {a.phase ?? "—"} · <code className="mono">{a.id.slice(0, 18)}</code>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}

function StatusChip({ run, shipped }: { run: Run; shipped: boolean }) {
  const s = run.status ?? "unknown";
  const tone = shipped ? "live" : s === "running" || s === "queued" ? "warn" : s === "failed" ? "bad" : "muted";
  const label = shipped ? "built" : s === "running" ? "building" : s;
  return <span className={`ra-dot ${tone}`} title={`latest run: ${s}`}>{label}</span>;
}

/** Is this run an attempt at building an APP, or internal work on BOSS itself?
 *
 * The first version treated every governed run as an app and produced a card reading
 * "Captain: QUALITY IMPROVE PASS — 44 attempts", which is not an app anybody has. A run earns
 * a place here by having produced something you can open: a pull request or a preview. Runs
 * still in flight count too, so a build in progress is visible rather than appearing only once
 * it finishes.
 */
function looksLikeAnApp(run: Run, hasPreview: boolean): boolean {
  if (hasPreview || run.pull_request_url) return true;
  if (run.status === "running" || run.status === "queued") return true;
  return false;
}

/** Internal BOSS work that is not a product, however it is titled. */
const NOT_AN_APP = /^(captain:|quality improve|follow-up: deferred|goldeneye profile)/i;

/** Group runs into apps by lineage, newest first, and pick each one's best build. */
export function groupIntoApps(runs: Run[], previews: Preview[]): App[] {
  const previewByRunId = new Map<string, Preview>();
  for (const p of previews) if (p.run_id) previewByRunId.set(p.run_id, p);

  const byKey = new Map<string, Run[]>();
  for (const run of runs) {
    if (!looksLikeAnApp(run, previewByRunId.has(run.id))) continue;
    // NAME first, lineage second.
    //
    // Lineage was the primary key at first, on the reasoning that retries share a root run
    // id. True, but incomplete: a follow-up started as its own root has a DIFFERENT lineage
    // while being obviously the same app, so "ApproveIt" appeared twice — the same confusion
    // this page exists to remove, one level up. The name is what a person means by "the same
    // app"; the run id is only a fallback for work with no usable title.
    const named = appName(run.title ?? "");
    const key = named !== "Untitled app" ? named.toLowerCase() : (run.finalize_ctx?.root_run_id ?? run.id);
    const list = byKey.get(key) ?? [];
    list.push(run);
    byKey.set(key, list);
  }

  const previewByRun = previewByRunId;

  const apps: App[] = [];
  for (const [key, attempts] of byKey) {
    const sorted = [...attempts].sort(
      (a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    );
    const latest = sorted[0];
    // "Shipped" means completed — NOT merely newest. A failed run is not a build, and
    // reporting one as the app's state is how View landed on a failure.
    const shipped = sorted.find((r) => r.status === "completed");
    const preview =
      sorted.map((r) => previewByRun.get(r.id)).find(Boolean) ?? undefined;
    const title = appName(shipped?.title ?? latest.title ?? "");
    // A lineage whose newest title is plainly internal is not a product, whatever it built.
    if (NOT_AN_APP.test(title)) continue;
    apps.push({
      key,
      title,
      attempts: sorted,
      latest,
      shipped,
      preview,
      repo: latest.spec?.repo ? `${latest.spec.owner}/${latest.spec.repo}` : undefined,
      spend: sorted.reduce((n, r) => n + (r.cost_usd_micros ?? 0), 0),
    });
  }
  return apps.sort((a, b) =>
    (b.latest.created_at ?? "").localeCompare(a.latest.created_at ?? ""),
  );
}
