"use client";

/**
 * GOVERN › WHERE PEOPLE DECIDE › AUTONOMY (BOSSAPP-1441, approved wireframe 2026-11-23).
 *
 *   Autonomy                                   [Org default] [This project (stricter only)]
 *   Development     [Agents act | Act within cap | Ask me]
 *   Production      🔒 You decide
 *   Unknown remote  🔒 Treated as production
 *   Top-up          [$1] / item  [$10] / day
 *   Explicit stops  🔒 Honoured
 *                                                                                    [Save]
 *
 * The three locked rows are fixed in the engine (services/autonomy_policy.py) and are never
 * controls here. "This project" may only be stricter than the org default: a looser mode is
 * greyed and says why, a larger top-up is refused before it is sent, and the API's own 422
 * sentence is shown if it still refuses. Saving the org default needs org:update, which asks
 * "Confirm it's you" through the one write door (obsWrite → apiWrite).
 */
import React, { useEffect, useState } from "react";
import { LockSimple } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import {
  DEV_LABEL, DEV_MODES, looserThan, savePolicy, type DevMode, type Policy, type PolicyScope, type PolicyView,
} from "../../lib/autonomy";
import "./autonomy.css";

const money = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(2));
const parseMoney = (t: string): number | null => {
  const v = Number(t.replace(/^\s*\$/, "").trim());
  return t.trim() !== "" && Number.isFinite(v) && v >= 0 ? v : null;
};

export function AutonomyControl({ projectId, view, error, onSaved }: {
  projectId: string; view: PolicyView | null; error?: string; onSaved: () => void;
}) {
  const [scope, setScope] = useState<PolicyScope>("org");
  const [dev, setDev] = useState<DevMode>("act");
  const [item, setItem] = useState("");
  const [day, setDay] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ tone: "red" | "green"; text: string } | null>(null);
  const [picked, setPicked] = useState(false);

  // The scope opens where the policy in force comes from; the person's pick then holds.
  useEffect(() => {
    if (view && !picked) setScope(view.policy.source === "project" ? "project" : "org");
  }, [view, picked]);
  const base: Policy | null = view ? (scope === "org" ? view.org : view.policy) : null;
  useEffect(() => {
    if (!base) return;
    setDev(base.dev); setItem(money(base.topup.per_item)); setDay(money(base.topup.per_day));
  }, [base?.dev, base?.topup.per_item, base?.topup.per_day, scope]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!view) {
    return (
      <div className="au-card" data-testid="au-control">
        <div className="au-head"><b>Autonomy</b></div>
        <div className="au-said" data-tone={error ? "red" : undefined} style={{ marginTop: 8 }}>{error ? `Could not read the policy: ${error}` : "…"}</div>
      </div>
    );
  }
  const org = view.org;
  const per = { per_item: parseMoney(item), per_day: parseMoney(day) };
  const dirty = !!base && (dev !== base.dev || per.per_item !== base.topup.per_item || per.per_day !== base.topup.per_day);

  const pick = (s: PolicyScope) => { setPicked(true); setScope(s); setSaid(null); };
  const save = async () => {
    if (per.per_item == null || per.per_day == null) { setSaid({ tone: "red", text: "Top-ups are dollar amounts." }); return; }
    if (scope === "project") {
      if (looserThan(dev, org.dev)) { setSaid({ tone: "red", text: `The org default is ${DEV_LABEL[org.dev]}; this project may only be stricter.` }); return; }
      if (per.per_item > org.topup.per_item || per.per_day > org.topup.per_day) {
        setSaid({ tone: "red", text: `The org's top-up is $${money(org.topup.per_item)} / item, $${money(org.topup.per_day)} / day; this project may only lower it.` });
        return;
      }
    }
    setBusy(true); setSaid(null);
    const r = await savePolicy(projectId, scope, dev, { per_item: per.per_item, per_day: per.per_day });
    setBusy(false);
    if (!r.ok) { setSaid({ tone: "red", text: r.error || "Not saved." }); return; }
    setSaid({ tone: "green", text: "Saved." });
    onSaved();
  };

  return (
    <div className="au-card" data-testid="au-control">
      <div className="au-head">
        <b>Autonomy</b><span className="au-spacer" />
        <span role="group" aria-label="Where it is set" style={{ display: "inline-flex", gap: 6, flexWrap: "wrap" }}>
          <button type="button" className="au-chip" aria-pressed={scope === "org"} onClick={() => pick("org")} data-testid="au-scope-org">Org default</button>
          <button type="button" className="au-chip" aria-pressed={scope === "project"} onClick={() => pick("project")} data-testid="au-scope-project">This project (stricter only)</button>
        </span>
      </div>
      <div className="au-grid">
        <span className="au-k" id="au-dev-k">Development</span>
        <div className="au-seg" role="radiogroup" aria-labelledby="au-dev-k" data-testid="au-dev">
          {DEV_MODES.map((m) => {
            const looser = scope === "project" && looserThan(m, org.dev);
            return (
              <button key={m} type="button" role="radio" aria-checked={dev === m} disabled={looser} data-mode={m} data-testid={`au-dev-${m}`}
                      title={looser ? `The org default is ${DEV_LABEL[org.dev]}: this project may only be stricter` : undefined}
                      onClick={() => { setDev(m); setSaid(null); }}>{DEV_LABEL[m]}</button>
            );
          })}
        </div>
        <span className="au-k">Production</span>
        <span className="au-lock" data-testid="au-lock-prod" title="Fixed: production always waits for a person"><LockSimple size={14} aria-hidden />You decide</span>
        <span className="au-k">Unknown remote</span>
        <span className="au-lock" data-testid="au-lock-unknown" title="Fixed: a place BOSS cannot classify is production"><LockSimple size={14} aria-hidden />Treated as production</span>
        <span className="au-k">Top-up</span>
        <div className="au-topup">
          <label className="au-money" title={scope === "project" ? `At most the org's $${money(org.topup.per_item)}` : "Spend past a cap without asking, per item"}>
            $<input inputMode="decimal" value={item} onChange={(e) => { setItem(e.target.value); setSaid(null); }} aria-label="Top-up per item, dollars" data-testid="au-topup-item" />
          </label><span>/ item</span>
          <label className="au-money" title={scope === "project" ? `At most the org's $${money(org.topup.per_day)}` : "Spend past a cap without asking, per day"}>
            $<input inputMode="decimal" value={day} onChange={(e) => { setDay(e.target.value); setSaid(null); }} aria-label="Top-up per day, dollars" data-testid="au-topup-day" />
          </label><span>/ day</span>
        </div>
        <span className="au-k">Explicit stops</span>
        <span className="au-lock" data-testid="au-lock-stops" title="Fixed: a stop a person set is always honoured"><LockSimple size={14} aria-hidden />Honoured</span>
      </div>
      <div className="au-foot">
        {said ? <span className="au-said" data-tone={said.tone} role={said.tone === "red" ? "alert" : "status"} data-testid="au-said">{said.text}</span> : null}
        <span className="au-spacer" />
        {/* The org default is org:update (it asks "Confirm it's you"); this project's row is autonomy:manage. */}
        {scope === "org" ? (
          <GatedButton key="org" permission="org:update" projectId={projectId} className="au-btn" data-kind="primary"
                       disabled={!dirty || busy} onClick={() => void save()} data-testid="au-save">{busy ? "Saving…" : "Save"}</GatedButton>
        ) : (
          <GatedButton key="project" permission="autonomy:manage" projectId={projectId} className="au-btn" data-kind="primary"
                       disabled={!dirty || busy} onClick={() => void save()} data-testid="au-save">{busy ? "Saving…" : "Save"}</GatedButton>
        )}
      </div>
    </div>
  );
}
