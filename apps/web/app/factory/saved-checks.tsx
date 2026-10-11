"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { GatedButton } from "../../components/gated";
import { postJson } from "./cockpit-actions";
import styles from "./saved-checks.module.css";

type Saved = {
  template_id: string; revision: number; revision_hash: string; title: string; criteria: string[];
  source: { run_id: string; review_id: string; snapshot_hash: string; delivered_head: string; check_state: string };
};

export function SavedChecks({ projectId, runId, acceptedReviewId, snapshotHash, criteria }: {
  projectId: string; runId: string; acceptedReviewId: string | null; snapshotHash: string; criteria: string[];
}) {
  const [items, setItems] = useState<Saved[]>([]);
  const [error, setError] = useState("");
  const [readError, setReadError] = useState("");
  const [status, setStatus] = useState("");
  const [editing, setEditing] = useState<Saved | null>(null);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const flight = useRef(false);
  const intent = useRef<{ body: string; id: string } | null>(null);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const fieldId = useId();
  const path = "/v1/acceptance-checks";
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    try {
      const response = await fetch(`${path}?project_id=${encodeURIComponent(projectId)}`, {
        cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Saved checks could not load (${response.status}).`);
      const body = await response.json() as { items?: Saved[] };
      if (!Array.isArray(body.items)) throw new Error("Saved checks are unavailable.");
      if (!controller.signal.aborted) { setItems(body.items); setReadError(""); }
    } catch (e) {
      if (!controller.signal.aborted) setReadError(e instanceof Error ? e.message : "Saved checks could not load.");
    }
  }, [projectId]);
  useEffect(() => {
    mounted.current = true; void load();
    return () => { mounted.current = false; request.current?.abort(); };
  }, [load]);

  async function save(edit: Saved | null) {
    if (flight.current || (!edit && !acceptedReviewId)) return;
    const url = edit ? `${path}/${encodeURIComponent(edit.template_id)}/revisions` : path;
    const body = edit ? { project_id: projectId, expected_revision_hash: edit.revision_hash,
      title: title.trim(), criteria: text.split("\n").map(s => s.trim()).filter(Boolean) } : {
      project_id: projectId, run_id: runId, review_id: acceptedReviewId,
      snapshot_hash: snapshotHash, title: "Checks from accepted result",
    };
    const encoded = JSON.stringify({ url, body });
    if (intent.current?.body !== encoded) intent.current = { body: encoded, id: crypto.randomUUID() };
    flight.current = true; setBusy(true); setError(""); setStatus("");
    try {
      const result = await postJson(url, { ...body, submission_id: intent.current.id });
      if (!mounted.current) return;
      if (!result.ok) { setError(result.error ?? "Checks were not saved."); return; }
      setEditing(null); setStatus("Checks saved. No work started and approvals are unchanged.");
      await load();
    } finally { flight.current = false; if (mounted.current) setBusy(false); }
  }

  async function copy(item: Saved) {
    setError(""); setStatus("");
    try {
      await navigator.clipboard.writeText(item.criteria.map(check => `- ${check}`).join("\n"));
      if (mounted.current) setStatus("Checks copied. Paste them into a fresh task, then review its current work agreement before starting.");
    } catch { if (mounted.current) setError("Copy is unavailable. Select the checks below and copy them manually."); }
  }

  return <section className={styles.panel} aria-label="Reusable acceptance checks">
    <h3>Use these checks again</h3>
    <p>Save a starting point from an accepted result, then edit it for another task. Saved checks do not prove a future result or grant permission to work.</p>
    <GatedButton permission="workflow:create" type="button" className="ag-btn" disabled={busy || !acceptedReviewId || !criteria.length}
      onClick={() => void save(null)}>Save accepted checks</GatedButton>
    {!acceptedReviewId ? <p>Accept the current recorded result before saving its checks.</p> : !criteria.length ? <p>No acceptance checks were recorded for this result.</p> : null}
    {error || readError ? <div role="alert">{error || readError} <button className="ag-link" type="button" onClick={() => void load()}>Reload saved checks</button></div> : null}
    {status ? <div role="status">{status}</div> : null}
    <ul className={styles.list}>{items.map(item => <li key={item.template_id}>
      <strong data-user-text>{item.title}</strong> <span>Revision {item.revision}</span>
      <p>From recorded result <code>{item.source.run_id}</code>, revision <code>{item.source.delivered_head.slice(0, 12)}</code>. Recorded checks: {item.source.check_state.replaceAll("_", " ")}.</p>
      <ul>{item.criteria.map((check, i) => <li key={i} data-user-text>{check}</li>)}</ul>
      <button className="ag-link" type="button" disabled={busy} onClick={() => void copy(item)}>Copy checks for a fresh task</button>
      <GatedButton permission="workflow:create" type="button" className="ag-link" disabled={busy} onClick={() => {
        setEditing(item); setTitle(item.title); setText(item.criteria.join("\n")); setError(""); setStatus("");
      }}>Edit saved checks</GatedButton>
      {editing?.template_id === item.template_id ? <div className={styles.editor}>
        <label htmlFor={`${fieldId}-title`}>Check name</label>
        <input id={`${fieldId}-title`} value={title} maxLength={120} disabled={busy} onChange={e => setTitle(e.target.value)} />
        <label htmlFor={`${fieldId}-checks`}>Acceptance checks (one per line)</label>
        <textarea id={`${fieldId}-checks`} rows={5} value={text} maxLength={6000} disabled={busy} onChange={e => setText(e.target.value)} />
        <GatedButton permission="workflow:create" type="button" className="ag-btn" disabled={busy || !title.trim() || !text.trim()} onClick={() => void save(editing)}>Save check revision</GatedButton>
        <button className="ag-link" type="button" disabled={busy} onClick={() => setEditing(null)}>Cancel edit</button>
      </div> : null}
    </li>)}</ul>
  </section>;
}
