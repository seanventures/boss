"use client";

import React, { useCallback, useEffect, useState } from "react";
import { GatedButton } from "../../components/gated";
import { ReadFailure } from "../../components/locked-panel";
import { SectionLabel } from "../section-label";
import { formatDate } from "../../lib/datetime";
import {
  createTrigger,
  deleteTrigger,
  previewTriggerMatch,
  previewTriggerSchedule,
  readTriggers,
  rotateTriggerSecret,
  setTriggerPaused,
  updateTrigger,
  type TriggerDescribe,
  type TriggersData,
} from "../../lib/agent-platform";
import { readConnections, type Connection } from "../../lib/intake";
import "./agent-platform.css";

const KINDS = ["cron", "event", "webhook", "monitor", "intake"] as const;
const CONCURRENCY = ["skip_if_active", "always_enqueue", "replace", "queue_one"] as const;

export function PassportTriggers({
  projectId,
  roleKey,
  actor,
}: {
  projectId: string;
  roleKey: string;
  actor: string;
}) {
  const [data, setData] = useState<TriggersData | null>(null);
  const [readError, setReadError] = useState<Awaited<ReturnType<typeof readTriggers>> | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [kindFilter, setKindFilter] = useState<string>("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [msg, setMsg] = useState<string>("");
  const [err, setErr] = useState<string>("");
  const [onceSecret, setOnceSecret] = useState<string | null>(null);

  // Create/edit form
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [environment, setEnvironment] = useState("prod");
  const [kind, setKind] = useState<string>("cron");
  const [name, setName] = useState("");
  const [concurrency, setConcurrency] = useState<string>("skip_if_active");
  const [cronExpr, setCronExpr] = useState("0 * * * *");
  const [timezone, setTimezone] = useState("UTC");
  const [everySeconds, setEverySeconds] = useState("300");
  const [eventPattern, setEventPattern] = useState("com.github.pull_request.*");
  const [monitorQuery, setMonitorQuery] = useState("error_rate > 0.05");
  const [connectionId, setConnectionId] = useState("");
  const [matchField, setMatchField] = useState("type");
  const [matchOp, setMatchOp] = useState("prefix");
  const [matchValue, setMatchValue] = useState("com.");
  const [identityMode, setIdentityMode] = useState("owner");
  const [goalText, setGoalText] = useState("");
  const [schedPreview, setSchedPreview] = useState<Array<{ at: string; label: string }>>([]);
  const [matchPreview, setMatchPreview] = useState<{
    matched?: number;
    total?: number;
    items?: Array<{ id?: string; type?: string; matched?: boolean; reason?: string }>;
  } | null>(null);

  const load = useCallback(async () => {
    const r = await readTriggers(projectId, roleKey, kindFilter ? { kind: kindFilter } : {});
    if (!r.ok) {
      setReadError(r);
      return;
    }
    setReadError(null);
    setData(r.data);
  }, [projectId, roleKey, kindFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    let alive = true;
    void readConnections(projectId).then((r) => {
      if (alive && r.ok) setConnections(r.data);
    });
    return () => { alive = false; };
  }, [projectId]);

  useEffect(() => {
    if (!creating && !editingId) return;
    if (kind !== "cron" && kind !== "monitor") {
      setSchedPreview([]);
      return;
    }
    let alive = true;
    const params = kind === "cron"
      ? { kind: "cron", cron: cronExpr, timezone, count: 3 }
      : { kind: "monitor", every_seconds: Number(everySeconds) || 300, timezone, count: 3 };
    void previewTriggerSchedule(projectId, params).then((r) => {
      if (alive && r.ok) setSchedPreview(r.data.next ?? []);
    });
    return () => { alive = false; };
  }, [creating, editingId, kind, cronExpr, timezone, everySeconds, projectId]);

  const resetForm = () => {
    setCreating(false);
    setEditingId(null);
    setName("");
    setGoalText("");
    setMatchPreview(null);
  };

  const openEdit = (t: TriggerDescribe) => {
    setCreating(false);
    setEditingId(t.id);
    setEnvironment(t.environment);
    setKind(t.kind);
    setName(t.name);
    setConcurrency(t.concurrency || "skip_if_active");
    const cfg = t.config || {};
    if (typeof cfg.cron === "string") setCronExpr(cfg.cron);
    if (typeof cfg.timezone === "string") setTimezone(cfg.timezone);
    if (cfg.every_seconds) setEverySeconds(String(cfg.every_seconds));
    if (typeof cfg.event === "string") setEventPattern(cfg.event);
    if (typeof cfg.query === "string") setMonitorQuery(cfg.query);
    if (t.connection_id) setConnectionId(t.connection_id);
    if (t.identity_mode) setIdentityMode(t.identity_mode);
    const m = (cfg.match as Record<string, unknown> | undefined) || {};
    if (typeof m.field === "string") setMatchField(m.field);
    if (typeof m.op === "string") setMatchOp(m.op);
    if (typeof m.value === "string") setMatchValue(m.value);
    if (typeof cfg.goal === "string") setGoalText(cfg.goal);
    setErr("");
    setMsg("");
  };

  const buildConfig = () => {
    const cfg: Record<string, unknown> = {};
    if (kind === "cron") {
      cfg.cron = cronExpr.trim();
      cfg.timezone = timezone.trim() || "UTC";
    } else if (kind === "monitor") {
      cfg.every_seconds = Number(everySeconds) || 300;
      cfg.query = monitorQuery.trim();
    } else if (kind === "event") {
      cfg.event = eventPattern.trim();
    } else if (kind === "intake") {
      cfg.match = { field: matchField.trim() || "type", op: matchOp, value: matchValue };
      if (goalText.trim()) cfg.goal = goalText.trim();
    }
    if (goalText.trim() && kind !== "intake") cfg.goal = goalText.trim();
    return cfg;
  };

  const onSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    setMsg("");
    setBusyId(editingId || "new");
    const cfg = buildConfig();
    if (editingId) {
      const r = await updateTrigger(projectId, actor, editingId, {
        name: name.trim(),
        concurrency,
        config: cfg,
        ...(kind === "intake" ? { identity_mode: identityMode } : {}),
      });
      setBusyId(null);
      if (!r.ok) { setErr(r.error); return; }
      setMsg(`Updated trigger ${r.data.name || editingId}.`);
      resetForm();
      void load();
      return;
    }
    const r = await createTrigger(projectId, actor, roleKey, {
      environment,
      kind,
      name: name.trim() || `${kind} trigger`,
      concurrency,
      config: cfg,
      ...(kind === "intake" && connectionId ? { connection_id: connectionId, identity_mode: identityMode } : {}),
    });
    setBusyId(null);
    if (!r.ok) { setErr(r.error); return; }
    if (r.data.secret) setOnceSecret(r.data.secret);
    setMsg(`Created ${kind} trigger.`);
    resetForm();
    void load();
  };

  const onPreviewMatch = async () => {
    if (!connectionId) { setErr("Select an intake connection first."); return; }
    setErr("");
    const r = await previewTriggerMatch(projectId, actor, roleKey, {
      connection_id: connectionId,
      match: { field: matchField.trim() || "type", op: matchOp, value: matchValue },
      limit: 20,
    });
    if (!r.ok) { setErr(r.error); return; }
    setMatchPreview(r.data as unknown as typeof matchPreview);
  };

  const onPauseToggle = async (t: TriggerDescribe) => {
    const paused = t.status !== "paused";
    setBusyId(t.id);
    setErr("");
    const r = await setTriggerPaused(projectId, actor, t.id, paused, paused ? "Paused from console" : "");
    setBusyId(null);
    if (!r.ok) { setErr(r.error); return; }
    setMsg(paused ? `Paused ${t.name || t.id}.` : `Resumed ${t.name || t.id}.`);
    void load();
  };

  const onRotate = async (t: TriggerDescribe) => {
    setBusyId(t.id);
    setErr("");
    const r = await rotateTriggerSecret(projectId, actor, t.id);
    setBusyId(null);
    if (!r.ok) { setErr(r.error); return; }
    if (r.data.secret) setOnceSecret(r.data.secret);
    setMsg(`Rotated signing secret for ${t.name || t.id}.`);
    void load();
  };

  const onDelete = async (t: TriggerDescribe) => {
    setBusyId(t.id);
    setErr("");
    const r = await deleteTrigger(projectId, actor, t.id);
    setBusyId(null);
    if (!r.ok) { setErr(r.error); return; }
    setMsg(`Deleted trigger ${t.name || t.id}.`);
    void load();
  };

  if (readError && !readError.ok) {
    return (
      <section className="ct-block ap-wrap" id="ag-sec-triggers" data-testid="ap-triggers-sec">
        <SectionLabel level={1} as="h2">Triggers</SectionLabel>
        <ReadFailure problem={{ status: readError.status, text: readError.error }} onRetry={() => void load()} testId="ap-triggers-err" />
      </section>
    );
  }

  const triggers = data?.triggers ?? [];
  const kinds = data?.kinds?.length ? data.kinds : [...KINDS];
  const concOptions = data?.concurrency?.length ? data.concurrency : [...CONCURRENCY];

  return (
    <section className="ct-block ap-wrap" id="ag-sec-triggers" data-testid="ap-triggers-sec">
      <div className="ap-head">
        <div>
          <SectionLabel level={1} as="h2">Triggers</SectionLabel>
          <p className="ap-sub">Schedules, webhooks, monitors, internal events, and intake routes.</p>
        </div>
        <div className="ap-grow" />
        <div className="ap-row">
          <select
            className="ap-select"
            aria-label="Filter trigger kind"
            value={kindFilter}
            data-testid="ap-trig-filter"
            onChange={(e) => setKindFilter(e.target.value)}
          >
            <option value="">All kinds</option>
            {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          <GatedButton
            permission="agent:deploy"
            type="button"
            className="ap-btn"
            data-kind="primary"
            data-testid="ap-trig-new-btn"
            onClick={() => { resetForm(); setCreating(true); }}
          >
            + Add trigger
          </GatedButton>
        </div>
      </div>

      {onceSecret ? (
        <div className="ap-secret-box" data-testid="ap-trig-secret-once" role="status">
          <div className="ap-row">
            <strong>Webhook signing secret (shown once)</strong>
            <div className="ap-grow" />
            <button type="button" className="ap-link-btn" onClick={() => setOnceSecret(null)}>Dismiss</button>
          </div>
          <code className="ap-code">{onceSecret}</code>
        </div>
      ) : null}

      {msg ? <p className="ap-ok" data-testid="ap-trig-msg">{msg}</p> : null}
      {err ? <p className="ap-err" role="alert" data-testid="ap-trig-err">{err}</p> : null}

      {(creating || editingId) ? (
        <form className="ap-card ap-wrap" onSubmit={onSave} data-testid="ap-trig-form">
          <div className="ap-row">
            <strong>{editingId ? "Edit trigger" : "Create trigger"}</strong>
            <div className="ap-grow" />
            <button type="button" className="ap-link-btn" onClick={resetForm}>Cancel</button>
          </div>
          <div className="ap-grid3">
            {!editingId ? (
              <>
                <div className="ap-field">
                  <label htmlFor="ap-trig-env">Environment</label>
                  <select id="ap-trig-env" className="ap-select" value={environment} onChange={(e) => setEnvironment(e.target.value)}>
                    <option value="dev">dev</option>
                    <option value="staging">staging</option>
                    <option value="prod">prod</option>
                  </select>
                </div>
                <div className="ap-field">
                  <label htmlFor="ap-trig-kind">Kind</label>
                  <select id="ap-trig-kind" className="ap-select" data-testid="ap-trig-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
                    {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
                  </select>
                </div>
              </>
            ) : null}
            <div className="ap-field">
              <label htmlFor="ap-trig-name">Name</label>
              <input id="ap-trig-name" className="ap-input" data-testid="ap-trig-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Hourly triage" />
            </div>
            <div className="ap-field">
              <label htmlFor="ap-trig-conc">Concurrency</label>
              <select id="ap-trig-conc" className="ap-select" value={concurrency} onChange={(e) => setConcurrency(e.target.value)}>
                {concOptions.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
          </div>

          {kind === "cron" ? (
            <div className="ap-grid2">
              <div className="ap-field">
                <label htmlFor="ap-trig-cron">Cron expression</label>
                <input id="ap-trig-cron" className="ap-input" data-testid="ap-trig-cron" value={cronExpr} onChange={(e) => setCronExpr(e.target.value)} />
              </div>
              <div className="ap-field">
                <label htmlFor="ap-trig-tz">Timezone</label>
                <input id="ap-trig-tz" className="ap-input" value={timezone} onChange={(e) => setTimezone(e.target.value)} />
              </div>
            </div>
          ) : null}

          {kind === "monitor" ? (
            <div className="ap-grid2">
              <div className="ap-field">
                <label htmlFor="ap-trig-every">Every (seconds)</label>
                <input id="ap-trig-every" className="ap-input" type="number" min={10} value={everySeconds} onChange={(e) => setEverySeconds(e.target.value)} />
              </div>
              <div className="ap-field">
                <label htmlFor="ap-trig-q">Monitor query</label>
                <input id="ap-trig-q" className="ap-input" value={monitorQuery} onChange={(e) => setMonitorQuery(e.target.value)} />
              </div>
            </div>
          ) : null}

          {kind === "event" ? (
            <div className="ap-field">
              <label htmlFor="ap-trig-ev">Event pattern</label>
              <input id="ap-trig-ev" className="ap-input" value={eventPattern} onChange={(e) => setEventPattern(e.target.value)} />
            </div>
          ) : null}

          {kind === "intake" ? (
            <div className="ap-wrap">
              <div className="ap-grid3">
                <div className="ap-field">
                  <label htmlFor="ap-trig-conn">Connection</label>
                  <select id="ap-trig-conn" className="ap-select" value={connectionId} onChange={(e) => setConnectionId(e.target.value)}>
                    <option value="">Select connection…</option>
                    {connections.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.kind})</option>)}
                  </select>
                </div>
                <div className="ap-field">
                  <label htmlFor="ap-trig-idmode">Identity mode</label>
                  <select id="ap-trig-idmode" className="ap-select" value={identityMode} onChange={(e) => setIdentityMode(e.target.value)}>
                    <option value="owner">owner</option>
                    <option value="person">person</option>
                    <option value="person_or_link">person_or_link</option>
                    <option value="service">service</option>
                  </select>
                </div>
                <div className="ap-field">
                  <label htmlFor="ap-trig-mval">Match ({matchField} {matchOp})</label>
                  <input id="ap-trig-mval" className="ap-input" value={matchValue} onChange={(e) => setMatchValue(e.target.value)} />
                </div>
              </div>
              <div className="ap-row">
                <select className="ap-select" aria-label="Match field" value={matchField} onChange={(e) => setMatchField(e.target.value)}>
                  <option value="type">type</option>
                  <option value="subject">subject</option>
                  <option value="source">source</option>
                </select>
                <select className="ap-select" aria-label="Match operator" value={matchOp} onChange={(e) => setMatchOp(e.target.value)}>
                  <option value="prefix">prefix</option>
                  <option value="eq">eq</option>
                  <option value="contains">contains</option>
                </select>
                <GatedButton permission="integration:read" type="button" className="ap-btn" data-testid="ap-trig-preview-match" onClick={() => void onPreviewMatch()}>
                  Preview match
                </GatedButton>
              </div>
              {matchPreview ? (
                <div className="ap-subcard" data-testid="ap-trig-match-res">
                  <span className="ap-mono">Matched {matchPreview.matched ?? 0} of {matchPreview.total ?? 0} recent events</span>
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="ap-field">
            <label htmlFor="ap-trig-goal">Prompt / goal override (optional)</label>
            <input id="ap-trig-goal" className="ap-input" value={goalText} onChange={(e) => setGoalText(e.target.value)} placeholder="Summarize the event and open a review." />
          </div>

          {schedPreview.length > 0 ? (
            <div className="ap-subcard" data-testid="ap-trig-sched-preview">
              <div className="ap-k">Next firings</div>
              <div className="ap-row">
                {schedPreview.map((n) => (
                  <span key={n.at} className="ap-pill">{n.label || formatDate(n.at)}</span>
                ))}
              </div>
            </div>
          ) : null}

          <div className="ap-row">
            <GatedButton permission="agent:deploy" type="submit" className="ap-btn" data-kind="primary" disabled={busyId !== null} data-testid="ap-trig-save">
              {busyId ? "Saving…" : editingId ? "Save trigger" : "Create trigger"}
            </GatedButton>
          </div>
        </form>
      ) : null}

      {triggers.length === 0 ? (
        <p className="ap-sub" data-testid="ap-triggers-empty">No triggers configured for this agent yet.</p>
      ) : (
        <div className="ap-table-wrap">
          <table className="ap-table" data-testid="ap-triggers-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Env</th>
                <th>Status</th>
                <th>Fires / Skips</th>
                <th>Next</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {triggers.map((t) => (
                <tr key={t.id} data-testid={`ap-trig-row-${t.id}`}>
                  <td>
                    <strong>{t.name || t.id}</strong>
                    {t.url_path ? <div className="ap-mono ap-sub">{t.url_path}</div> : null}
                  </td>
                  <td><span className="ap-pill">{t.kind_label || t.kind}</span></td>
                  <td className="ap-mono">{t.environment}</td>
                  <td>
                    <span className="ap-pill" data-tone={t.status === "active" ? "green" : "amber"}>
                      {t.status}
                    </span>
                    {t.paused_reason ? <div className="ap-sub">{t.paused_reason}</div> : null}
                  </td>
                  <td className="ap-mono">{t.fires ?? 0} / {t.skips ?? 0}</td>
                  <td className="ap-mono">{t.next_fires?.[0]?.label || formatDate(t.next_fires?.[0]?.at, "—")}</td>
                  <td>
                    <div className="ap-row">
                      <GatedButton permission="agent:deploy" type="button" className="ap-chip-btn" disabled={busyId === t.id} onClick={() => openEdit(t)}>
                        Edit
                      </GatedButton>
                      <GatedButton permission="agent:lifecycle" type="button" className="ap-chip-btn" disabled={busyId === t.id} data-testid={`ap-trig-pause-${t.id}`} onClick={() => void onPauseToggle(t)}>
                        {t.status === "paused" ? "Resume" : "Pause"}
                      </GatedButton>
                      {t.kind === "webhook" ? (
                        <GatedButton permission="agent:deploy" type="button" className="ap-chip-btn" disabled={busyId === t.id} data-testid={`ap-trig-rotate-${t.id}`} onClick={() => void onRotate(t)}>
                          Rotate secret
                        </GatedButton>
                      ) : null}
                      <GatedButton permission="agent:deploy" type="button" className="ap-chip-btn" disabled={busyId === t.id} data-testid={`ap-trig-del-${t.id}`} onClick={() => void onDelete(t)}>
                        Delete
                      </GatedButton>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
