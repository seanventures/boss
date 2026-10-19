"use client";

/**
 * SET UP MONITORING — the SRE agent's monitoring pack (engine services/monitoring_pack.py),
 * on its own step and on the release's approval card, in the incident card's "who did what"
 * voice:
 *
 *   (face) Beacon · Site Reliability Engineer        Set up monitoring
 *          Watches order failures and payment errors on the Order API
 *          ⬣ SEV1  AppHealthCheckFailing   3 failed probes in 10 min   ✓ quiet last hour   Runbook: …
 *          ▲ SEV2  OrderPaymentFailures    > 2% of payments fail …     ✓ quiet last hour   Runbook: …
 *          4 rules · 4 runbooks · 2 panels in boss.deploy.yaml on boss/code/… · Rules ↗ · Dashboard ↗
 *
 * Every rule's PromQL is one click away. Pure render from the parsed card.
 */
import React from "react";

import type { ResponderWho } from "../../lib/incident-response";
import type { Sev } from "../../lib/app-alerts";
import { SevFlag } from "../observability/severity";
import { Who } from "../observability/responders";
import "../observability/obs.css";

export type PackRule = {
  alert: string; kind: string; label: string; expr: string; severity: Sev; threshold: string;
  enabled: boolean; runbook: string | null; runbookTitle: string | null; isNew: boolean;
  fired: number | null; previewWord: string;
};
export type MonitoringPack = {
  app: string; environment: string; summary: string; adding: boolean; branch: string;
  manifestPath: string; rules: PackRule[]; runbooks: { path: string; title: string }[];
  panels: string[]; tuned: { alert: string; before: string; after: string }[];
  gate: "block" | "warn"; rulesHref: string | null; dashboardUrl: string | null;
  dashboardWhy: string | null; missing: boolean; why: string | null; who: ResponderWho;
};

const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : d);
const opt = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);
const SEVS = new Set(["sev1", "sev2", "sev3", "sev4"]);
const LEGACY: Record<string, Sev> = { critical: "sev1", warning: "sev3", info: "sev4" };
const sevOf = (v: unknown): Sev => {
  const s = str(v).toLowerCase();
  return (SEVS.has(s) ? s : LEGACY[s] ?? "sev3") as Sev;
};

/** The pack on a step's output (`output.pack`) or a decision's recommendation
 *  (`recommendation.monitoring_pack`); null when there is none. */
export function monitoringPackOf(raw: unknown): MonitoringPack | null {
  const o = obj(raw);
  if (!Object.keys(o).length) return null;
  const by = obj(o.by);
  return {
    app: str(o.app), environment: str(o.environment), summary: str(o.summary),
    adding: o.adding === true, branch: str(o.branch), manifestPath: str(o.manifest_path, "boss.deploy.yaml"),
    rules: arr(o.rules).map((w) => {
      const r = obj(w);
      const p = obj(r.preview);
      return {
        alert: str(r.alert), kind: str(r.kind), label: str(r.label, str(r.alert)), expr: str(r.expr),
        severity: sevOf(r.severity), threshold: str(r.threshold), enabled: r.enabled !== false,
        runbook: opt(r.runbook), runbookTitle: opt(r.runbook_title) ?? opt(r.runbook), isNew: r.new === true,
        fired: typeof p.fired === "number" ? p.fired : null, previewWord: str(p.word, "Not previewed"),
      };
    }),
    runbooks: arr(o.runbooks).map((w) => ({ path: str(obj(w).path), title: str(obj(w).title) })),
    panels: arr(o.panels).map((p) => str(p)).filter(Boolean),
    tuned: arr(o.tuned).map((w) => ({ alert: str(obj(w).alert), before: str(obj(w).before), after: str(obj(w).after) })),
    gate: o.gate === "warn" ? "warn" : "block",
    rulesHref: opt(o.rules_href), dashboardUrl: opt(o.dashboard_url), dashboardWhy: opt(o.dashboard_why),
    missing: o.missing === true, why: opt(o.why),
    who: { kind: "agent", roleKey: str(by.role_key, "sre"), role: str(by.role, "Site Reliability Engineer"),
           name: null, how: "named", skills: [], onBehalfOf: null, model: null },
  };
}

/** The pack a run step shows: its own (Set up monitoring), or — on the release it precedes —
 *  the pack that release carries. `nextOf` reads the workflow's wiring. */
export function packForStep(steps: Array<{ step_id: string; capability: string | null; status: string; output: Record<string, unknown> | null }>,
                            current: { step_id: string; capability: string | null; status: string; output: Record<string, unknown> | null },
                            nextOf: (stepId: string) => string[]): MonitoringPack | null {
  const own = (s: typeof current): MonitoringPack | null => {
    const out = s.output ?? {};
    if (out.pack) return monitoringPackOf(out.pack);
    if (!["succeeded", "failed", "skipped"].includes(s.status)) return null;
    return monitoringPackOf({ missing: true, gate: out.gate, why: out.note ?? out.error ?? "it did not run" });
  };
  if (current.capability === "monitoring_pack") return own(current);
  if (current.capability !== "deploy") return null;
  const before = steps.find((s) => s.capability === "monitoring_pack" && nextOf(s.step_id).includes(current.step_id));
  return before ? own(before) : null;
}

function plural(n: number, one: string): string {
  return `${n} ${one}${n === 1 ? "" : "s"}`;
}

function Preview({ r }: { r: PackRule }) {
  if (!r.enabled) return <span className="obs-chip" title="This rule is off in the pack">off</span>;
  const tone = r.fired === null ? undefined : r.fired > 0 ? "amber" : "green";
  const word = r.fired === null ? "no preview" : r.fired > 0 ? `fired ${r.fired}× last hour` : "quiet last hour";
  return <span className="obs-chip" data-tone={tone} data-testid="mp-preview" title={r.previewWord}>{word}</span>;
}

export function MonitoringPackCard({ pack, onCard = false }: { pack: MonitoringPack; onCard?: boolean }) {
  if (pack.missing) {
    return (
      <div className="obs-inc-part mp-pack" data-testid="mp-pack" data-missing="true">
        <span className="obs-line"><Who w={pack.who} testId="mp-who" /><span className="ag-spacer" /><span className="obs-responder-step">Set up monitoring</span></span>
        <p className="detail" data-testid="mp-missing">
          No monitoring pack{pack.why ? `: ${pack.why}` : "."}{" "}
          {pack.gate === "block"
            ? "This release cannot ship without one (monitoring_gate: block)."
            : "The release goes on without one (monitoring_gate: warn)."}
        </p>
      </div>
    );
  }
  const added = pack.rules.filter((r) => r.isNew).length;
  return (
    <div className="obs-inc-part mp-pack" data-testid="mp-pack">
      <span className="obs-line obs-responder-head">
        <Who w={pack.who} testId="mp-who" />
        <span className="ag-spacer" />
        <span className="obs-responder-step">Set up monitoring</span>
      </span>
      {onCard ? (
        <p className="detail" data-testid="mp-approve-with">
          Approving this release approves its monitoring pack: these rules go live with it, managed in the repo.
        </p>
      ) : null}
      {pack.summary ? <q className="obs-words" data-user-text data-testid="mp-summary">{pack.summary}</q> : null}
      <ul className="mp-rules" aria-label="The pack's rules" data-testid="mp-rules">
        {pack.rules.map((r) => (
          <li key={r.alert} data-testid="mp-rule" data-alert={r.alert} data-new={r.isNew || undefined}>
            <SevFlag sev={r.severity} />
            <span className="mp-alert inf-mono" data-copy="data">{r.alert}</span>
            <span className="mp-threshold" title="The threshold">{r.threshold}</span>
            <Preview r={r} />
            {r.runbookTitle ? <span className="inf-faint mp-runbook" data-testid="mp-rule-runbook" title={r.runbook ?? undefined}>Runbook: {r.runbookTitle}</span> : null}
            {r.isNew ? null : <span className="obs-chip" title="Already in the pack; kept as it is">kept</span>}
            <details className="mp-expr">
              <summary>PromQL</summary>
              <code className="inf-mono" data-copy="data" data-testid="mp-rule-expr">{r.expr}</code>
            </details>
          </li>
        ))}
      </ul>
      {pack.tuned.length ? (
        <span className="obs-line inf-faint" data-testid="mp-tuned" title={pack.tuned.map((t) => `${t.alert}: ${t.before} → ${t.after}`).join("\n")}>
          Tuned {plural(pack.tuned.length, "rule")} that would have fired on {pack.environment || "the first environment"}'s healthy traffic
        </span>
      ) : null}
      <span className="obs-line mp-foot" data-testid="mp-foot">
        <span>
          {plural(added, pack.adding ? "new rule" : "rule")}{pack.adding ? ` (${plural(pack.rules.length - added, "kept")})` : ""}
          {" · "}{plural(pack.runbooks.length, "runbook")} · {plural(pack.panels.length, "dashboard panel")}
          {" in "}<code className="inf-mono">{pack.manifestPath}</code>{pack.branch ? <> on <code className="inf-mono">{pack.branch}</code></> : null}
        </span>
        {pack.rulesHref ? <a className="inf-link" href={pack.rulesHref} data-testid="mp-rules-link" title="The app's Alerts tab: these rules, repo-managed">Rules ↗</a> : null}
        {pack.dashboardUrl ? (
          <a className="inf-link" href={pack.dashboardUrl} target="_blank" rel="noreferrer noopener" data-testid="mp-dashboard-link" title="The app's Grafana dashboard">Dashboard ↗</a>
        ) : <span className="inf-faint" data-testid="mp-dashboard-why" title={pack.dashboardWhy ?? undefined}>No dashboard link yet</span>}
      </span>
    </div>
  );
}
