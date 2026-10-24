"use client";

/**
 * AN ALERT'S RULE, ONE PRESS FROM THE ALERT. Every alert row names the rule that raised it
 * (AppLatencyP95 is not SampleLatencyP95) and opens this sheet: the rule's switches, where the
 * operator turns Respond automatically off without hunting for the app's Rules list.
 *
 *   SampleLatencyP95                                                      ×
 *    ◐ SEV3  p95 latency   Custom PromQL                         Firing
 *    Respond automatically ◉      Triage ◉
 *    Silence   Tune
 *
 * Tune opens the rule sheet (threshold, 24 h preview; a repo rule is proposed as a pull request);
 * Silence the silence sheet. An alert's own Tune opens straight on the sheet (`start="tune"`). The switches write at once, through
 * the same toggleRule the app's Rules list uses.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { endSilencePath, rulesPutPath, tuneTip, useRules, type AlertRule } from "../../lib/app-alerts";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { Sheet } from "../infra/kit";
import { obsWrite } from "./obs-write";
import { RuleSheet } from "./rule-sheet";
import { SevFlag } from "./severity";
import { SilenceSheet } from "./silence-sheet";

export const RESPOND_TIP = "Starts Incident Response when the agent keeps this alert in an incident";
const TRIAGE_TIP = "An agent reads each alert and decides: incident, noise or ask you";

type Ctx = { projectId: string; actor: string; targetId: string };

/** One switch of a rule, written at once (Respond automatically, Triage). */
export async function toggleRule(c: Ctx, r: AlertRule, what: "respond" | "triage"): Promise<boolean> {
  const change = what === "respond" ? { id: r.id, auto_respond: !r.autoRespond }
    // Triage off means nothing to respond to: Respond automatically goes off with it.
    : { id: r.id, triage: !r.triage, ...(r.triage ? { auto_respond: false } : {}) };
  const res = await obsWrite(rulesPutPath(c.targetId), { project_id: c.projectId, actor: c.actor, rules: [change] },
                             { scope: `obs:rule-${what}:${r.id}`, label: what === "respond" ? "Respond automatically" : "Triage" }, "PUT");
  if (res.ok) {
    showToast("success", what === "respond" ? (r.autoRespond ? "Responds only when you ask" : "Responds automatically")
      : r.triage ? "Triage off" : "Triage on");
    notifyChanged("observability", { target_id: c.targetId });
  } else showToast("error", res.error ?? "Not changed");
  return res.ok;
}

export async function endRuleSilence(c: Ctx, r: AlertRule): Promise<boolean> {
  if (!r.silence) return false;
  const res = await obsWrite(endSilencePath(c.targetId, r.silence.id), { project_id: c.projectId, actor: c.actor },
                             { scope: `obs:unsilence:${r.silence.id}`, label: "End silence" });
  if (res.ok) { showToast("success", "Silence ended"); notifyChanged("observability", { target_id: c.targetId }); }
  else showToast("error", res.error ?? "Not ended");
  return res.ok;
}

/** A labelled switch: the press, and its short word beside it. */
export function RuleSwitch({ on, label, tip, disabled, testId, onPress }: {
  on: boolean; label: string; tip: string; disabled: boolean; testId: string; onPress: () => void;
}) {
  return (
    <span className="obs-inc-auto">
      <GatedButton permission="observability:manage" type="button" role="switch" aria-checked={on} aria-label={label}
                   className="obs-switch" data-testid={testId} disabled={disabled} title={tip} onClick={onPress} />
      <span title={tip}>{label}</span>
    </span>
  );
}

/** The rule an alert came from, found by its id (else its alert name) among the app's rules. */
export function RuleOfAlert({ ruleId, alertname, targetId, projectId, actor, onClose, start = "switches" }: {
  ruleId: string | null; alertname: string; targetId: string; projectId: string; actor: string; onClose: () => void;
  /** "tune": open straight on the rule sheet (the alert's own Tune). */
  start?: "switches" | "tune";
}) {
  const rules = useRules(projectId, targetId);
  const [mode, setMode] = useState<"switches" | "edit" | "silence">(start === "tune" ? "edit" : "switches");
  const [busy, setBusy] = useState(false);
  const d = rules.data;
  const r = d?.rules.find((x) => x.id === ruleId) ?? d?.rules.find((x) => x.alert === alertname) ?? null;
  const c = { projectId, actor, targetId };
  const done = () => { rules.refresh(); setMode("switches"); };
  const press = async (fn: () => Promise<boolean>) => { setBusy(true); if (await fn()) rules.refresh(); setBusy(false); };

  if (r && mode === "edit") {
    return <RuleSheet rule={r} severities={d?.severities} targetId={targetId} projectId={projectId} actor={actor}
                      onClose={start === "tune" ? onClose : () => setMode("switches")} onSaved={start === "tune" ? onClose : done} />;
  }
  if (r && mode === "silence") {
    return <SilenceSheet rule={r} targetId={targetId} projectId={projectId} actor={actor} onClose={() => setMode("switches")} onSaved={done} />;
  }
  return (
    <Sheet title={r?.alert ?? alertname} testId="obs-alert-rule-sheet" onClose={onClose}>
      {!d ? (rules.error ? <span className="inf-error" title={rules.error}>Can&apos;t load the rule</span> : <span className="inf-faint">Loading…</span>)
        : !r ? <span className="inf-empty" data-testid="obs-alert-rule-gone" title="It was removed or renamed after this alert">Rule no longer exists</span> : (
        <div className="obs-rform">
          <span className="obs-line">
            <SevFlag sev={r.severity} catalog={d.severities} />
            <span className="obs-strong" title={r.summary ?? undefined}>{r.label}</span>
            <span className="inf-muted" title={r.expr} data-copy="data">{r.condition}</span>
            <span className="ag-spacer" />
            <span className="obs-chip" data-testid="obs-alert-rule-state" title={r.silence ? `Silenced: ${r.silence.reason}` : r.stateTip ?? undefined}>
              {r.silence ? "Silenced" : r.stateWord}</span>
          </span>
          <span className="obs-line">
            <RuleSwitch on={r.autoRespond} label="Respond automatically" testId="obs-alert-rule-respond" onPress={() => void press(() => toggleRule(c, r, "respond"))}
                        disabled={busy || !r.enabled || !r.triage} tip={!r.triage ? "Needs Triage on" : RESPOND_TIP} />
            <RuleSwitch on={r.triage} label="Triage" testId="obs-alert-rule-triage" onPress={() => void press(() => toggleRule(c, r, "triage"))}
                        disabled={busy || !r.enabled} tip={TRIAGE_TIP} />
          </span>
          <div className="inf-actions">
            {r.silence ? (
              <GatedButton permission="observability:manage" type="button" className="ag-btn" data-testid="obs-alert-rule-unsilence"
                           disabled={busy} title={`Silenced: ${r.silence.reason}`} onClick={() => void press(() => endRuleSilence(c, r))}>End silence</GatedButton>
            ) : (
              <GatedButton permission="observability:manage" type="button" className="ag-btn" data-testid="obs-alert-rule-silence"
                           disabled={busy || !r.enabled} title="Stop it alerting for a while, with a reason" onClick={() => setMode("silence")}>Silence</GatedButton>
            )}
            <span className="ag-spacer" />
            <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-alert-rule-edit"
                         title={tuneTip(r)} onClick={() => setMode("edit")}>Tune</GatedButton>
          </div>
        </div>
      )}
    </Sheet>
  );
}
