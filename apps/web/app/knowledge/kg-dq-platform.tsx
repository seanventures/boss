"use client";

/**
 * KNOWLEDGE GRAPH › DOCUMENTS › DQ PLATFORM (the banking DQ case study). "Load the NorthPeak
 * Bank data platform" is the product action (POST /v1/knowledge/samples/dq, confirmed; the
 * agent's `load_dq_platform` tool takes the same one): the banking-dq pack beside the project's
 * other packs, the DQ Monitor and the Profile Service connectors, the platform into the graph, the DQ workflows
 * and the daily escalation sweep. The rules come from the rulebook, never from here — the row
 * says how many are in the graph. Then the DQ Monitor's exception groups are listed by close of business,
 * each with "Investigate" (starts dq_incident_rca with that group), and the scheduled trigger can
 * be turned on: every morning, an investigation per group the DQ Monitor graded Error.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  DOCUMENT_MANAGE, SCAN_START, getDqPlatform, kgRunHref, loadDqPlatform, scheduleDqIntake,
  type KgDqGroup, type KgDqPlatform,
} from "../../lib/kg-view";
import { StartRefusal, asReadiness, type PendingRefusal } from "../workflow-readiness-ui";
import { post } from "../workflows/delivery-api";

type Who = { projectId: string; actor: string };

function groupStatus(g: KgDqGroup): string {
  if (g.grade !== "Error") return `${g.grade}: dashboard only`;
  if (g.incident_id) return `${g.incident_id} · ${g.incident_status ?? "open"}`;
  if (g.run_status) return `Investigation ${g.run_status}`;
  return "Not investigated";
}

function GroupRow({ w, group, workflowKey, archived, onStarted }: {
  w: Who; group: KgDqGroup; workflowKey: string; archived?: boolean; onStarted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string; href?: string | null } | null>(null);
  const label = `${group.monitor_rule_id} v${group.rule_version} on COB ${group.cob}`;
  const [refusal, setRefusal] = useState<PendingRefusal | null>(null);

  const start = async (startAnyway: string[] = []) => {
    setBusy(true); setMsg(null); setRefusal(null);
    const r = await post("start", {
      project_id: w.projectId, workflow_key: workflowKey, actor: w.actor,
      input: { exception_group_id: group.group_id, title: `Investigate ${label}` },
      prefer_project_copy: true,
      ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
    });
    setBusy(false);
    // A start refused for what the project lacks: the shared checklist and "Investigate anyway".
    const readiness = r.ok ? null : asReadiness(r.readiness);
    if (readiness) {
      setRefusal({ label: "Investigate", message: String(r.error ?? ""), readiness, retry: (keys) => start(keys) });
      return;
    }
    if (!r.ok) { setMsg({ ok: false, text: r.error ? String(r.error) : "The investigation did not start." }); return; }
    const data = (r.data ?? {}) as { run?: { id?: string }; id?: string; run_id?: string };
    const runId = data.run?.id ?? data.run_id ?? data.id ?? null;
    setMsg({ ok: true, text: `Investigating ${label}.`, href: runId ? kgRunHref({ workflow_run_id: runId }) : null });
    onStarted();
  };

  return (
    <li className="kg-doc-clause" data-testid="kg-dq-group">
      <span className="kg-doc-clause-section">{label}</span>
      <span className="kg-doc-clause-text">
        {group.rule_id}: {group.failed} of {group.population} failed on {group.column} — the DQ Monitor graded {group.grade}
      </span>
      <span className="kg-quiet" data-testid="kg-dq-group-status">{groupStatus(group)}</span>
      <span className="kg-doc-check">
        <GatedButton permission={SCAN_START} className="ag-btn" data-testid="kg-dq-investigate"
                     disabled={archived || busy}
                     title={`Start the DQ RCA and remediation pipeline for ${label}`}
                     onClick={() => void start()}>Investigate</GatedButton>
        {msg ? (
          <span className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-dq-investigate-msg">
            {msg.text}{msg.href ? <> <a className="inf-link" href={msg.href}>Open the run</a></> : null}
          </span>
        ) : null}
      </span>
      {refusal ? <StartRefusal refusal={refusal} projectId={w.projectId} busy={busy} onDismiss={() => setRefusal(null)} /> : null}
    </li>
  );
}

export function DqPlatformSample({ projectId, actor, archived = false, onLoaded }: {
  projectId: string; actor: string; archived?: boolean; onLoaded?: () => void;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [platform, setPlatform] = useState<KgDqPlatform | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [cob, setCob] = useState<string>("");

  const read = useCallback(async () => {
    const r = await getDqPlatform(w);
    if (r.ok) setPlatform(r.data);
  }, [w]);
  useEffect(() => { void read(); }, [read]);

  const load = async () => {
    setBusy(true); setMsg(null);
    const r = await loadDqPlatform(w);
    setBusy(false);
    if (!r.ok) { setMsg({ ok: false, text: r.error }); return; }
    setMsg({ ok: true, text: r.data.note });
    void read();
    onLoaded?.();
  };

  const schedule = async () => {
    setBusy(true); setMsg(null);
    const r = await scheduleDqIntake(w);
    setBusy(false);
    if (!r.ok) { setMsg({ ok: false, text: r.error }); return; }
    setMsg(r.data.error ? { ok: false, text: r.data.error }
      : { ok: true, text: r.data.created ? "Every morning the Cause Router investigates the DQ Monitor's Error groups."
                                         : "The morning intake is already scheduled." });
  };

  if (!platform) return null;
  const cobs = platform.cobs ?? [];
  const shown = cob || cobs[cobs.length - 1] || "";
  const groups = platform.groups.filter((g) => !shown || g.cob === shown);
  return (
    <section className="kg-doc" data-testid="kg-dq-platform" aria-label="DQ platform sample">
      <div className="kg-doc-row">
        <span className="kg-doc-pack-label">DQ platform</span>
        <span className="kg-doc-pack-value" data-testid="kg-dq-platform-state">
          {platform.loaded
            ? `NorthPeak Bank's data platform is loaded · ${platform.rules_in_graph} of ${platform.rules_in_rulebook} DQ rules in the graph`
            : "Not loaded"}
        </span>
        <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn" data-testid="kg-dq-load" disabled={archived || busy}
                     title="Load NorthPeak Bank's systems, tables, feeds, lineage, CDEs, reports, calendar, issues, the DQ Monitor's exception groups and past incidents, the banking-dq pack and its connectors"
                     onClick={() => void load()}>
          {busy ? "Working…" : platform.loaded ? "Reload" : "Load the NorthPeak Bank data platform"}
        </GatedButton>
        {platform.loaded ? (
          <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn" data-testid="kg-dq-schedule" disabled={archived || busy}
                       title="Every morning, start an investigation for each exception group the DQ Monitor graded Error"
                       onClick={() => void schedule()}>Investigate every morning</GatedButton>
        ) : null}
        {msg ? <span className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-dq-msg">{msg.text}</span> : null}
      </div>
      {platform.loaded && platform.rules_in_graph < platform.rules_in_rulebook ? (
        <p className="kg-quiet" data-testid="kg-dq-rules-missing">
          Load the rulebook (DQ rulebook sample, above) so the pipeline can read each rule and its thresholds;
          a rule not in the graph is recorded as a coverage gap.
        </p>
      ) : null}
      {platform.loaded ? (
        <>
          <div className="kg-doc-row">
            <label className="kg-doc-pack-label" htmlFor="kg-dq-cob">Close of business</label>
            <select id="kg-dq-cob" data-testid="kg-dq-cob" value={shown} onChange={(e) => setCob(e.target.value)}>
              {cobs.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <ul className="kg-doc-clause-list" data-testid="kg-dq-groups">
            {groups.map((g) => (
              <GroupRow key={g.group_id} w={w} group={g} workflowKey={platform.workflow_key}
                        archived={archived} onStarted={() => void read()} />
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
