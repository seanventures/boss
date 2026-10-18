"use client";

/**
 * The app drawer's ALERTS tab (BOSSAPP-598): this app's rules, its signal sources and every
 * alert that fired, in one place.
 *
 *   RULES                                                                    [+ Add rule]
 *    ● Error rate    > 5% for 5 min (≥ 20 req)   Firing   Respond automatically ◉   Edit  Silence
 *    Evaluated by BOSS's own monitoring · from boss.deploy.yaml · changed by Param, Oct 18
 *   SIGNAL SOURCES
 *    ● HelpDesk AI   signed reports · last 07:00 · 7 today                   Rotate token
 *   HISTORY                                                [Firing | 24 h | 7 days]
 *    07:05  Error rate 14.4% (rule 5%)  6bf2673  Agent: Opened an incident  Resolved   ▸
 *           ▾ PromQL · value at breach · Explore ↗ · Runbook · the agent's words
 *
 * Rules are edited in the side sheet (rule-sheet.tsx) and silenced in silence-sheet.tsx. Every
 * write goes through obsWrite: one press, one effect; Respond automatically asks for consent once.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  HISTORY_WINS, endSilencePath, rotatePath, rulesPutPath, triagePath, useHistory, useRules,
  type Alert, type AlertRule, type HistoryWin, type SignalSource,
} from "../../lib/app-alerts";
import { useLocalTime } from "../../lib/local-time";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { DrawerSection, Skeleton } from "../infra/kit";
import { Markdown } from "../markdown";
import { obsWrite } from "./obs-write";
import { RuleSheet } from "./rule-sheet";
import { SilenceSheet } from "./silence-sheet";

const RESPOND_TIP = "Starts Incident Response when the agent keeps this alert in an incident";
const SEVERITY_WORD = { critical: "High", warning: "Low" } as const;

function Dot({ tone }: { tone: string }) {
  return <span className="obs-adot" data-tone={tone} aria-hidden />;
}

export function whenWord(iso: string | null, lt: ReturnType<typeof useLocalTime>, now = Date.now()): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "—";
  return now - t < 20 * 3600_000 ? lt.hhmm(t) : lt.day(t);
}

function RuleRow({ r, busy, onEdit, onSilence, onRespond, onEndSilence }: {
  r: AlertRule; busy: boolean; onEdit: () => void; onSilence: () => void; onRespond: () => void; onEndSilence: () => void;
}) {
  const lt = useLocalTime();
  const tone = r.silence ? "grey" : r.stateTone;
  return (
    <li className="obs-arule" data-testid={`obs-rule-${r.kind === "custom" ? r.alert : r.kind}`} data-state={r.state ?? ""}>
      <span className="obs-arule-name">
        <Dot tone={tone} />
        <span className="obs-strong" title={r.description ?? r.summary ?? undefined}>{r.label}</span>
        <span className="obs-chip" title={`Severity ${SEVERITY_WORD[r.severity]}`} data-tone={r.severity === "critical" ? "red" : undefined}>
          {SEVERITY_WORD[r.severity]}
        </span>
      </span>
      <span className="inf-muted obs-arule-cond" title={r.expr} data-copy="data">{r.condition}</span>
      <span className="obs-arule-state">
        <span className="obs-chip" data-tone={tone === "grey" ? undefined : tone} data-testid="obs-rule-state"
              title={r.silence ? `Silenced: ${r.silence.reason}` : (r.stateTip ?? (r.valueWord ? `Now ${r.valueWord}` : undefined))}>
          {r.silence ? "Silenced" : r.stateWord}
        </span>
        {r.valueWord && !r.silence ? <span className="inf-faint inf-num" data-copy="data">{r.valueWord}</span> : null}
      </span>
      <span className="obs-inc-auto obs-arule-respond">
        <GatedButton permission="observability:manage" type="button" role="switch" aria-checked={r.autoRespond}
                     aria-label={`Respond automatically to ${r.label}`} className="obs-switch" data-testid="obs-rule-respond"
                     disabled={busy || !r.enabled || !r.triage} title={!r.triage ? "Needs the agent's triage on" : RESPOND_TIP}
                     onClick={onRespond} />
        <span title={r.respondOnBehalfOf ? `On ${r.respondOnBehalfOf}'s standing grant` : RESPOND_TIP}>Respond automatically</span>
      </span>
      <span className="obs-arule-acts">
        <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-rule-edit"
                     title="Change this rule" onClick={onEdit}>Edit</GatedButton>
        {r.silence ? (
          <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-rule-unsilence"
                       title={`Silenced until ${lt.full(r.silence.endsAt)}: ${r.silence.reason}`} onClick={onEndSilence}>End silence</GatedButton>
        ) : (
          <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-rule-silence"
                       title="Stop it alerting for a while, with a reason" disabled={!r.enabled} onClick={onSilence}>Silence</GatedButton>
        )}
      </span>
    </li>
  );
}

function SourceRow({ s, targetId, projectId, actor, onDone }: { s: SignalSource; targetId: string; projectId: string; actor: string; onDone: () => void }) {
  const lt = useLocalTime();
  const [sure, setSure] = useState(false);
  const [busy, setBusy] = useState(false);
  const rotate = async () => {
    if (!sure) { setSure(true); return; }
    setBusy(true);
    const r = await obsWrite<{ word?: string }>(rotatePath(targetId, s.id), { project_id: projectId, actor },
                                                { scope: `obs:rotate:${s.id}`, label: "Rotate token" });
    setBusy(false);
    setSure(false);
    if (r.ok) { showToast("success", r.body?.word ?? "Rotated"); onDone(); } else showToast("error", r.error ?? "Not rotated");
  };
  const minted = s.token.rotatedAt ?? s.token.mintedAt;
  return (
    <li className="obs-line" data-testid={`obs-source-${s.id}`}>
      <Dot tone={s.connected ? "green" : "grey"} />
      <span className="obs-strong" data-user-text>{s.app}</span>
      <span className="inf-muted">{s.kind}</span>
      <span className="inf-faint" title={s.lastReportAt ? lt.full(s.lastReportAt) : "No report yet"}>
        {s.lastReportAt ? `last ${whenWord(s.lastReportAt, lt)}` : "no reports yet"}
      </span>
      {s.reportsToday ? <span className="inf-faint inf-num">{s.reportsToday} today</span> : null}
      {!s.connected ? <span className="obs-chip" data-tone="amber" title="No signals token: deploy it with signals: declared">No token</span> : null}
      <span className="ag-spacer" />
      {s.connected ? (
        <GatedButton permission="observability:manage" type="button" className={sure ? "ag-btn ag-btn-sm" : "inf-link"}
                     data-testid="obs-source-rotate" disabled={busy}
                     title={sure ? "Its reports are refused until it is redeployed" : minted ? `Token from ${lt.full(minted)}; never shown` : "A new token; never shown"}
                     onClick={rotate}>{busy ? "Rotating…" : sure ? "Confirm rotate" : "Rotate token"}</GatedButton>
      ) : null}
    </li>
  );
}

function AlertDetail({ a, targetId, projectId, actor, onDone }: { a: Alert; targetId: string; projectId: string; actor: string; onDone: () => void }) {
  const [book, setBook] = useState(false);
  const [busy, setBusy] = useState(false);
  const again = async () => {
    setBusy(true);
    const r = await obsWrite(triagePath(targetId, a.id), { project_id: projectId, actor }, { scope: `obs:triage:${a.id}`, label: "Ask again" });
    setBusy(false);
    if (r.ok) { showToast("success", "Triaged"); onDone(); } else showToast("error", r.error ?? "Not triaged");
  };
  return (
    <div className="obs-adetail" data-testid="obs-alert-detail">
      {a.expr ? (
        <div className="obs-adetail-row">
          <span className="obs-fact-k">PromQL</span>
          <code className="obs-promql" data-copy="data" data-testid="obs-alert-expr">{a.expr}</code>
        </div>
      ) : null}
      <span className="obs-line">
        <span className="inf-muted">At breach</span>
        <span className="inf-num obs-strong" data-testid="obs-alert-value">{a.valueWord}</span>
        {a.thresholdWord ? <span className="inf-faint">rule {a.thresholdWord}</span> : null}
        {a.fireCount > 1 ? <span className="obs-chip" title="Fired again within five minutes of clearing">{a.fireCount}× fired</span> : null}
        {a.evaluatedBy ? <span className="inf-faint" title="Who evaluated the rule">{a.evaluatedBy}</span> : null}
        {a.exploreUrl ? (
          <a className="inf-link" href={a.exploreUrl} target="_blank" rel="noreferrer noopener" data-testid="obs-alert-explore"
             title="This query in Grafana Explore, for the breach window">Explore ↗</a>
        ) : a.exploreReason ? (
          <span className="inf-faint" data-testid="obs-alert-explore" title={a.exploreReason}>No Explore link</span>
        ) : null}
        {a.runbook.kind === "builtin" ? (
          <button type="button" className="inf-link" aria-expanded={book} data-testid="obs-alert-runbook" title={a.runbook.title}
                  onClick={() => setBook((v) => !v)}>{book ? "Hide runbook" : "Runbook"}</button>
        ) : a.runbook.href ? (
          <a className="inf-link" href={a.runbook.href} target="_blank" rel="noreferrer noopener" data-testid="obs-alert-runbook"
             title={a.runbook.url}>Runbook ↗</a>
        ) : null}
      </span>
      {book && a.runbook.markdown ? <div className="obs-inc-md" data-testid="obs-alert-runbook-md"><Markdown text={a.runbook.markdown} /></div> : null}
      <div className="obs-adetail-row" data-testid="obs-alert-triage">
        <span className="obs-fact-k">Agent</span>
        <span className="obs-strong">{a.triage.word}</span>
        {a.triage.reason ? <span className="obs-inc-text" data-user-text>{a.triage.reason}</span> : null}
        {a.triage.question ? <span className="obs-inc-text" data-user-text data-testid="obs-alert-question">{a.triage.question}</span> : null}
        {a.triage.by ? <span className="inf-faint" title="Who decided" data-copy="data">{a.triage.by}</span> : null}
        {a.triage.state === "no_model" && a.state === "firing" ? (
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-sm" data-testid="obs-alert-triage-again"
                       disabled={busy} title="Connect a model in Repo and models, then ask again" onClick={again}>Ask again</GatedButton>
        ) : null}
      </div>
      {a.heldBack ? <span className="inf-faint" data-testid="obs-alert-held">{a.heldBack.word}</span> : null}
      {a.response ? <span className="obs-line"><span className="inf-muted">Response</span><span className="obs-strong">{a.response.word}</span></span> : null}
    </div>
  );
}

/** What holds an alert back or changes how loud it is, as short chips with the reason in a tooltip. */
export function AlertBadges({ a }: { a: Alert }) {
  const lt = useLocalTime();
  return (
    <>
      {a.source ? <span className="obs-chip" data-testid="obs-alert-source" title="Where this alert came from">{a.source}</span> : null}
      {a.urgency === "low" ? (
        <span className="obs-chip" data-testid="obs-alert-low" title="Low urgency: it waits for working hours and never pages">Low urgency</span>
      ) : null}
      {a.flapping ? (
        <span className="obs-chip" data-tone="amber" data-testid="obs-alert-flapping"
              title={a.flapTuning ? `${a.flapTuning.reason} Suggested: ${a.flapTuning.action}` : `Firing and clearing over and over${a.flapScore !== null ? ` (score ${a.flapScore})` : ""}`}>
          Flapping
        </span>
      ) : null}
      {a.acked ? (
        <span className="obs-chip" data-tone="green" data-testid="obs-alert-acked"
              title={`Acked by ${a.acked.by} at ${lt.full(a.acked.at)}. Repeats stop; it is not silenced.`}>
          Acked{a.acked.until ? ` until ${whenWord(a.acked.until, lt)}` : ""}
        </span>
      ) : null}
      {a.suppressed.map((x) => (
        <span key={`${x.kind}:${x.id}`} className="obs-chip" data-testid="obs-alert-suppressed"
              title={[x.reason, x.until ? `until ${lt.full(x.until)}` : null].filter(Boolean).join(" · ") || x.word}>{x.word}</span>
      ))}
      {a.noDataSince ? (
        <span className="obs-chip" data-tone="amber" data-testid="obs-alert-nodata" title={`No data since ${lt.full(a.noDataSince)}`}>
          No data since {whenWord(a.noDataSince, lt)}
        </span>
      ) : null}
      {a.keepFiringSince ? (
        <span className="obs-chip" data-testid="obs-alert-keepfiring" title={`Recovered at ${lt.full(a.keepFiringSince)}; kept firing a while to be sure`}>
          Holding since {whenWord(a.keepFiringSince, lt)}
        </span>
      ) : null}
      {a.links.map((l) => (
        <a key={l.href} className="inf-link" href={l.href} target="_blank" rel="noreferrer noopener" data-testid="obs-alert-link"
           title={l.href}>{l.text} ↗</a>
      ))}
    </>
  );
}

export function AlertRow({ a, targetId, projectId, actor, onDone, showApp, onApp, onIncident, selected, onSelect, onAck }: {
  a: Alert; targetId: string; projectId: string; actor: string; onDone: () => void;
  showApp?: boolean; onApp?: () => void; onIncident?: () => void;
  /** Observability › Alerts: select-many for Ack selected, and a per-row Ack. */
  selected?: boolean; onSelect?: (on: boolean) => void; onAck?: () => void;
}) {
  const lt = useLocalTime();
  const [open, setOpen] = useState(false);
  const tone = a.state === "firing" ? (a.heldBack || a.acked ? "amber" : "red") : a.state === "pending" ? "amber" : "grey";
  return (
    <li className="obs-alert" data-testid={`obs-alert-${a.id}`} data-state={a.state}>
      <span className="obs-line">
        {onSelect ? (
          <input type="checkbox" checked={Boolean(selected)} aria-label={`Select ${a.label} on ${a.app}`} data-testid="obs-alert-select"
                 onChange={(e) => onSelect(e.target.checked)} />
        ) : null}
        <Dot tone={tone} />
        <span className="inf-num obs-alert-time" title={lt.full(a.firedAt ?? a.activeAt)}>{whenWord(a.firedAt ?? a.activeAt, lt)}</span>
        {showApp ? (
          <button type="button" className="inf-link obs-strong" data-testid="obs-alert-app" title={`${a.app} · ${a.env}: its Alerts tab`}
                  onClick={onApp}>{a.app}</button>
        ) : null}
        <span className="obs-strong obs-alert-what" title={a.description ?? a.summary} data-user-text>{a.label}</span>
        <span className="inf-num" data-copy="data">{a.valueWord}</span>
        {a.thresholdWord ? <span className="inf-faint">(rule {a.thresholdWord})</span> : null}
        {a.release?.sha7 ? <span className="inf-mono inf-faint" title={`Release ${a.release.ref ?? ""} was serving`} data-copy="data">{a.release.sha7}</span> : null}
        <span className="obs-chip" data-testid="obs-alert-triage-word" data-tone={a.triage.state === "no_model" ? "amber" : undefined}
              title={a.triage.reason ? "The agent's words are in the details" : a.triage.word}>Agent: {a.triage.word}</span>
        {a.incident ? (
          <button type="button" className="inf-link" data-testid="obs-alert-incident" title={a.incident.title}
                  onClick={onIncident}>{a.incident.word}</button>
        ) : null}
        <AlertBadges a={a} />
        <span className="ag-spacer" />
        <span className="inf-faint">{a.state === "resolved" ? (a.clearedWord ?? "Resolved") : a.stateWord}</span>
        {onAck && a.state !== "resolved" && !a.acked ? (
          <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-alert-ack"
                       title="Stops repeat notifications for 4 hours. It does not silence the alert." onClick={onAck}>Ack</GatedButton>
        ) : null}
        <button type="button" className="ag-icon-btn" aria-expanded={open} aria-label={open ? "Hide details" : "Show details"}
                data-testid="obs-alert-open" title={open ? "Hide details" : "PromQL, value, Explore, runbook, triage"}
                onClick={() => setOpen((v) => !v)}>{open ? "▾" : "▸"}</button>
      </span>
      {open ? <AlertDetail a={a} targetId={targetId || a.targetId} projectId={projectId} actor={actor} onDone={onDone} /> : null}
    </li>
  );
}

export function AppAlertsTab({ projectId, actor, targetId, onIncident }: {
  projectId: string; actor: string; targetId: string; onIncident: () => void;
}) {
  const lt = useLocalTime();
  const rules = useRules(projectId, targetId);
  const [win, setWin] = useState<HistoryWin>("24h");
  const history = useHistory(projectId, targetId, win);
  const [edit, setEdit] = useState<AlertRule | "new" | null>(null);
  const [silenceOf, setSilenceOf] = useState<AlertRule | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const refresh = () => { rules.refresh(); history.refresh(); notifyChanged("observability", { target_id: targetId }); };

  const respond = async (r: AlertRule) => {
    if (busy) return;
    setBusy(r.id);
    const res = await obsWrite(rulesPutPath(targetId), { project_id: projectId, actor, rules: [{ id: r.id, auto_respond: !r.autoRespond }] },
                               { scope: `obs:rule-respond:${r.id}`, label: "Respond automatically" }, "PUT");
    setBusy(null);
    if (res.ok) { showToast("success", r.autoRespond ? "Responds only when you ask" : "Responds automatically"); refresh(); }
    else showToast("error", res.error ?? "Not changed");
  };
  const endSilence = async (r: AlertRule) => {
    if (!r.silence || busy) return;
    setBusy(r.id);
    const res = await obsWrite(endSilencePath(targetId, r.silence.id), { project_id: projectId, actor },
                               { scope: `obs:unsilence:${r.silence.id}`, label: "End silence" });
    setBusy(null);
    if (res.ok) { showToast("success", "Silence ended"); refresh(); } else showToast("error", res.error ?? "Not ended");
  };

  const d = rules.data;
  const newest = d?.rules.filter((r) => r.changedAt).sort((a, b) => Date.parse(b.changedAt!) - Date.parse(a.changedAt!))[0];
  const sourceWord = d ? [...new Set(d.rules.map((r) => r.sourceWord))].join(" · ") : "";
  return (
    <div className="obs-alerts-tab" data-testid="obs-app-alerts">
      <DrawerSection label="RULES" testId="obs-app-rules" actions={
        <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-rule-add"
                     title="A rule of your own, in PromQL" onClick={() => setEdit("new")}>+ Add rule</GatedButton>}>
        {!d ? (rules.error ? <span className="inf-error" title={rules.error}>Can&apos;t load rules</span> : <Skeleton testId="obs-rules-loading" />) : (
          <>
            <ul className="obs-list obs-arules">
              {d.rules.map((r) => (
                <RuleRow key={r.id} r={r} busy={busy === r.id} onEdit={() => setEdit(r)} onSilence={() => setSilenceOf(r)}
                         onRespond={() => void respond(r)} onEndSilence={() => void endSilence(r)} />
              ))}
            </ul>
            <span className="inf-faint obs-arule-source" data-testid="obs-rules-source" title={d.evaluatorTip ?? undefined}>
              <span>Evaluated by {d.evaluator}</span>
              <span aria-hidden> · </span><span>{sourceWord}</span>
              {newest ? <><span aria-hidden> · </span>
                <span title={lt.full(newest.changedAt)}>changed by {newest.changedBy ?? "someone"}, {lt.day(Date.parse(newest.changedAt!))}</span></> : null}
            </span>
          </>
        )}
      </DrawerSection>

      <DrawerSection label="SIGNAL SOURCES" testId="obs-app-sources">
        {d && d.sources.length ? (
          <ul className="obs-list">{d.sources.map((s) => <SourceRow key={s.id} s={s} targetId={targetId} projectId={projectId} actor={actor} onDone={refresh} />)}</ul>
        ) : d ? <span className="inf-empty">No signal sources</span> : null}
        <span className="inf-faint" data-testid="obs-source-connect"
              title="Declare signals: {path: /boss/signals} in its boss.deploy.yaml, then deploy it">+ Connect a source</span>
      </DrawerSection>

      <DrawerSection label="HISTORY" testId="obs-app-history" actions={
        <span className="inf-seg" role="radiogroup" aria-label="History window" data-testid="obs-history-win">
          {HISTORY_WINS.map((w) => (
            <button key={w.key} type="button" role="radio" aria-checked={win === w.key} className="inf-seg-btn" title={w.tip}
                    data-testid={`obs-history-${w.key}`} onClick={() => setWin(w.key)}>{w.label}</button>
          ))}
        </span>}>
        {!history.data ? (history.error ? <span className="inf-error" title={history.error}>Can&apos;t load alerts</span> : <Skeleton testId="obs-history-loading" />)
          : history.data.alerts.length === 0 ? (
            <span className="inf-empty" data-testid="obs-history-empty">{win === "firing" ? "Nothing firing" : "No alerts in this window"}</span>
          ) : (
            <ul className="obs-list obs-alerts">
              {history.data.alerts.map((a) => <AlertRow key={a.id} a={a} targetId={targetId} projectId={projectId} actor={actor}
                                                        onDone={refresh} onIncident={onIncident} />)}
            </ul>
          )}
      </DrawerSection>

      {edit ? <RuleSheet rule={edit === "new" ? null : edit} targetId={targetId} projectId={projectId} actor={actor}
                         onClose={() => setEdit(null)} onSaved={() => { setEdit(null); refresh(); }} /> : null}
      {silenceOf ? <SilenceSheet rule={silenceOf} targetId={targetId} projectId={projectId} actor={actor}
                                 onClose={() => setSilenceOf(null)} onSaved={() => { setSilenceOf(null); refresh(); }} /> : null}
    </div>
  );
}
