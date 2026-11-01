"use client";

/**
 * KNOWLEDGE GRAPH › DOCUMENTS › AML SAMPLE (case study 4). One row: "Load AML sample" puts
 * NorthPeak Lending's synthetic bank into the project — the banking-aml pack, the sample's mock
 * connectors (case management, KYC, transactions), the customers/accounts/alerts in the graph and
 * the aml_alert_investigation workflow — through POST /v1/knowledge/samples/aml, the same action
 * the agent's `load_aml_sample` tool takes. Then each alert offers "Investigate", which starts
 * aml_alert_investigation with that alert (app/workflows/delivery-api.ts `post("start")`), and
 * shows the latest investigation's case status and the person's determination.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  DOCUMENT_MANAGE, SCAN_START, getAmlSample, kgNodeHref, kgRunHref, loadAmlSample,
  type KgAmlAlert, type KgAmlSample,
} from "../../lib/kg-view";
import { post } from "../workflows/delivery-api";

type Who = { projectId: string; actor: string };

const STATUS_WORDS: Record<string, string> = {
  open: "Investigating",
  closed_false_positive: "Closed as a false positive",
  escalated_filed: "Escalated, SAR filed",
};

function AlertRow({ w, alert, workflowKey, archived, onStarted }: {
  w: Who; alert: KgAmlAlert; workflowKey: string; archived?: boolean; onStarted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string; href?: string | null } | null>(null);

  const start = async () => {
    setBusy(true); setMsg(null);
    const r = await post("start", {
      project_id: w.projectId, workflow_key: workflowKey, actor: w.actor,
      input: { alert_id: alert.alert_id, title: `Investigate ${alert.alert_id}` },
      prefer_project_copy: true,
    });
    setBusy(false);
    if (!r.ok) { setMsg({ ok: false, text: r.error ? String(r.error) : "The investigation did not start." }); return; }
    const data = (r.data ?? {}) as { run?: { id?: string }; id?: string; run_id?: string };
    const runId = data.run?.id ?? data.run_id ?? data.id ?? null;
    setMsg({ ok: true, text: `Investigating ${alert.alert_id}.`, href: runId ? kgRunHref({ workflow_run_id: runId }) : null });
    onStarted();
  };

  const status = alert.case_status ? (STATUS_WORDS[alert.case_status] ?? alert.case_status) : "Not investigated";
  return (
    <li className="kg-doc-clause" data-testid="kg-aml-alert">
      {alert.node_id
        ? <a className="inf-link kg-doc-clause-section" href={kgNodeHref(alert.node_id)} data-testid="kg-aml-alert-link">{alert.alert_id}</a>
        : <span className="kg-doc-clause-section">{alert.alert_id}</span>}
      <span className="kg-doc-clause-text">{alert.rule} on {alert.account_id} ({alert.customer_id}), raised {alert.raised_at}</span>
      <span className="kg-quiet" data-testid="kg-aml-alert-status">{status}</span>
      <span className="kg-doc-check">
        <GatedButton permission={SCAN_START} className="ag-btn" data-testid="kg-aml-investigate"
                     disabled={archived || busy || !alert.node_id}
                     title={`Start the AML alert investigation for ${alert.alert_id}`}
                     onClick={() => void start()}>Investigate</GatedButton>
        {msg ? (
          <span className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-aml-investigate-msg">
            {msg.text}{msg.href ? <> <a className="inf-link" href={msg.href}>Open the run</a></> : null}
          </span>
        ) : null}
      </span>
    </li>
  );
}

export function AmlSample({ projectId, actor, archived = false, onLoaded }: {
  projectId: string; actor: string; archived?: boolean; onLoaded?: () => void;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [sample, setSample] = useState<KgAmlSample | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const read = useCallback(async () => {
    const r = await getAmlSample(w);
    if (r.ok) setSample(r.data);
  }, [w]);
  useEffect(() => { void read(); }, [read]);

  const load = async () => {
    setBusy(true); setMsg(null);
    const r = await loadAmlSample(w);
    setBusy(false);
    if (!r.ok) { setMsg({ ok: false, text: r.error }); return; }
    setMsg({ ok: true, text: r.data.note });
    void read();
    onLoaded?.();
  };

  if (!sample) return null;
  return (
    <section className="kg-doc" data-testid="kg-aml-sample" aria-label="AML sample">
      <div className="kg-doc-row">
        <span className="kg-doc-pack-label">AML sample</span>
        <span className="kg-doc-pack-value" data-testid="kg-aml-sample-state">
          {sample.loaded ? "NorthPeak Lending's synthetic bank is loaded: 3 open alerts" : "Not loaded"}
        </span>
        <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn" data-testid="kg-aml-load" disabled={archived || busy}
                     title="Load NorthPeak Lending's synthetic customers, accounts, transactions and alerts, the banking-aml pack and its connectors"
                     onClick={() => void load()}>
          {busy ? "Loading…" : sample.loaded ? "Reload" : "Load AML sample"}
        </GatedButton>
        {msg ? <span className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-aml-load-msg">{msg.text}</span> : null}
      </div>
      {sample.loaded ? (
        <ul className="kg-doc-clause-list" data-testid="kg-aml-alerts">
          {sample.alerts.map((alert) => (
            <AlertRow key={alert.alert_id} w={w} alert={alert} workflowKey={sample.workflow_key}
                      archived={archived} onStarted={() => void read()} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}
