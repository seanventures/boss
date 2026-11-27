"use client";

/**
 * Govern › Self-fix (docs/product/BOSS_FIXES_ITSELF.md §8.3, §8.4, §8.7, §10.5).
 *
 *   Self-fix                                                     [ On ] ⓘ
 *   Per fix $10, Per month $50, Time 60 min, Confidence medium ▾, Health 10 min, Tries 2
 *   Ready to fix itself   2 to do
 *     Installed from   ✓ /home/me/boss @ 1a2b3c4d5e6f (v1.0-rc10.37) ⓘ
 *     Source mirror    ! Not seeded ⓘ           boss self-fix seed      [ Copy ]
 *     Host installer   ! Not enrolled ⓘ         boss self-fix enable    [ Copy ]
 *     Mode             ✓ Installs on its own ⓘ  No ask                  Change ›
 *   Installed fixes   2 on the stack                       [ Remove all local fixes ]
 *
 * ENT-REC-54: everything a self-fix needs is in ONE place, each missing piece with ONE action (a
 * host command to copy, the switch, or the autonomy policy). The switch is the kill switch: off
 * cancels every fix still being built and installs nothing new; what is installed stays (Remove
 * all is the uninstall). `BOSS_SELF_FIX=off` beats it and is said. Whether an install asks a
 * person is the autonomy policy's (development + agents act: installs alone, listed in Decided
 * for you; production or Ask me: a person approves each one).
 */
import React, { useEffect, useState } from "react";
import { ArrowSquareOut, CheckCircle, Copy, Power, WarningCircle } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { usePolledRead } from "../../lib/log-stream";
import { showToast } from "../client-forms";
import {
  polledSelfFix, selfFixAreaHref, selfFixWrite, shortSha, usd, type ReadinessPiece, type Settings,
  type SettingsView,
} from "../../lib/self-fix";
import "../improve/improve.css";
import { localWith } from "../../lib/local-time";

type Field = { key: keyof Settings; label: string; tip: string; unit: "usd" | "min" | "n"; min: number; max: number };
const FIELDS: Field[] = [
  { key: "self_fix_budget_cents", label: "Per fix", tip: "Most one self-fix may spend", unit: "usd", min: 1, max: 100 },
  { key: "self_fix_month_cents", label: "Per month", tip: "Most all self-fixes may spend in a month", unit: "usd", min: 1, max: 1000 },
  { key: "self_fix_max_minutes", label: "Time", tip: "Longest one fix may take to reach your approval", unit: "min", min: 5, max: 240 },
  { key: "self_fix_health_minutes", label: "Health check", tip: "How long BOSS watches its health after an install", unit: "min", min: 1, max: 60 },
  { key: "self_fix_attempts_per_signature", label: "Tries", tip: "Self-fixes per bug before it stays with a person", unit: "n", min: 1, max: 5 },
];

const shown = (f: Field, v: number) => (f.unit === "usd" ? Math.round(v) / 100 : v);
const stored = (f: Field, v: number) => (f.unit === "usd" ? Math.round(v * 100) : Math.round(v));

/** The install refusal, trimmed. Blank is nothing to say. */
export function installWhySentence(why: string | null | undefined): string | null {
  const text = why?.trim() ?? "";
  return text ? text : null;
}

/** Beside "Can't install here". More than six words is a paragraph. */
export function InstallWhy({ sentence }: { sentence: string | null }) {
  if (!sentence) return null;
  return sentence.split(/\s+/).filter(Boolean).length > 6
    ? <p data-testid="gv-selffix-cannot-why" data-user-text>{sentence}</p>
    : <span data-testid="gv-selffix-cannot-why" data-user-text>{sentence}</span>;
}

async function copyCommand(command: string) {
  try {
    await navigator.clipboard.writeText(command);
    showToast("success", "Copied: run it on the machine BOSS is installed on");
  } catch {
    showToast("error", "Couldn't copy; select the command instead");
  }
}

/** One readiness piece: what it is, its state (the why in the tooltip), and its one action. */
export function ReadyPiece({ piece, projectId, busy, turnOn, children }: {
  piece: ReadinessPiece; projectId: string; busy: boolean; turnOn: () => void; children?: React.ReactNode;
}) {
  const a = piece.action;
  return (
    <div className="sfx-govern-row sfx-ready-row" data-testid={`gv-selffix-piece-${piece.key}`}>
      <span className="sfx-govern-label" title={piece.why || piece.label}>{piece.label}</span>
      <span className={`cap-badge ${piece.ok ? "tone-green" : "tone-amber"} sfx-ready-state`}
            data-testid={`gv-selffix-state-${piece.key}`} title={piece.why || piece.state}>
        {piece.ok ? <CheckCircle size={12} aria-hidden /> : <WarningCircle size={12} aria-hidden />}
        <span data-user-text>{piece.state}</span>
      </span>
      {children}
      {a?.kind === "command" ? (
        <span className="sfx-ready-do">
          <code className="sfx-ready-cmd" data-copy="data" title={`${a.label}: ${a.command}`}>{a.command}</code>
          <button type="button" className="ag-btn" data-testid={`gv-selffix-do-${piece.key}`}
                  title={`${a.label} (run on the machine BOSS is installed on)`}
                  onClick={() => void copyCommand(a.command)}>
            <Copy size={13} aria-hidden /> Copy
          </button>
        </span>
      ) : a?.kind === "switch" ? (
        <GatedButton permission="improvement:manage" projectId={projectId} type="button" className="ag-btn"
                     data-testid={`gv-selffix-do-${piece.key}`} disabled={busy} title="Let BOSS fix its own bugs"
                     onClick={turnOn}>
          <Power size={13} aria-hidden /> {a.label}
        </GatedButton>
      ) : a?.kind === "link" ? (
        <a className="ag-link" href={a.href} data-testid={`gv-selffix-do-${piece.key}`} title="Govern › Where people decide">
          {a.label} <ArrowSquareOut size={12} aria-hidden />
        </a>
      ) : null}
    </div>
  );
}

export function SelfFixTab({ projectId, actor, paused }: { projectId: string; actor: string; paused: boolean }) {
  const read = usePolledRead<SettingsView>({
    key: projectId, pollMs: 30_000, paused,
    read: polledSelfFix<SettingsView>("settings", { project_id: projectId }),
  });
  const v = read.data;
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [confirmAll, setConfirmAll] = useState(false);
  useEffect(() => { setDraft({}); }, [v?.settings]);

  if (read.error && !v) {
    return <div className="ag-empty" role="alert"><span title={read.error}>Can&apos;t reach BOSS</span></div>;
  }
  if (!v) return <div className="gv-skel" aria-busy="true" />;

  const on = !!v.switch.on && !v.env_off;
  const write = async (what: string, body: Record<string, unknown>, ok: string) => {
    setBusy(what);
    const got = await selfFixWrite(what === "remove_all" ? "remove_all" : "settings",
                                   { project_id: projectId, actor, ...body });
    setBusy("");
    if (got.ok) { showToast("success", ok); read.reload(); }
    else showToast("error", got.error);
    return got.ok;
  };
  const changed = FIELDS.filter((f) => draft[f.key] !== undefined && draft[f.key] !== String(shown(f, Number(v.settings[f.key]))));
  const confidenceChanged = draft.self_fix_min_confidence !== undefined && draft.self_fix_min_confidence !== v.settings.self_fix_min_confidence;
  const save = () => write("settings", { settings: {
    ...Object.fromEntries(changed.map((f) => [f.key, stored(f, Number(draft[f.key]))])),
    ...(confidenceChanged ? { self_fix_min_confidence: draft.self_fix_min_confidence } : {}),
  } }, "Saved");

  return (
    <div className="sfx-govern" data-testid="gv-selffix">
      <div className="sfx-govern-row">
        <span className="sfx-govern-label" title="BOSS fixes bugs in its own code, and installs a fix only after you approve it">
          Self-fix
        </span>
        <GatedButton permission="improvement:manage" projectId={projectId} type="button" role="switch"
                     aria-checked={on} aria-label="Self-fix" className="im-switch" data-testid="gv-selffix-switch"
                     disabled={v.env_off || !!busy}
                     title={v.env_off ? "BOSS_SELF_FIX=off on this install beats this switch"
                       : on ? "Off cancels fixes being built; installed fixes stay" : "Let BOSS fix its own bugs"}
                     onClick={() => write("switch", { on: !on, why: on ? "switched off in Govern" : "switched on in Govern" },
                                          on ? "Self-fix off" : "Self-fix on")}>
          {on ? "On" : "Off"}
        </GatedButton>
        {v.env_off ? <span className="sfx-sub" data-testid="gv-selffix-env-off" title="Set in the install's environment">BOSS_SELF_FIX=off</span>
          : !v.switch.on && v.switch.why ? <span className="sfx-sub" data-testid="gv-selffix-why" title={v.switch.why}>Off ⓘ</span> : null}
        {!v.can_install ? (
          <>
            <span className="sfx-sub tone-amber" data-testid="gv-selffix-cannot" title={v.why_not}>Can&apos;t install here ⓘ</span>
            <InstallWhy sentence={installWhySentence(v.why_not)} />
          </>
        ) : null}
      </div>
      <form className="sfx-govern-caps" data-testid="gv-selffix-caps" noValidate
            onSubmit={(e) => { e.preventDefault(); void save(); }}>
        {FIELDS.map((f) => (
          <label key={f.key} className="sfx-cap" title={f.tip}>
            <span>{f.label}</span>
            {f.unit === "usd" ? <span aria-hidden>$</span> : null}
            <input className="input sfx-cap-input" type="number" min={f.min} max={f.max}
                   step={f.unit === "usd" ? "any" : 1} aria-label={f.label}
                   data-testid={`gv-selffix-${f.key}`}
                   value={draft[f.key] ?? String(shown(f, Number(v.settings[f.key])))}
                   onChange={(e) => setDraft((d) => ({ ...d, [f.key]: e.target.value }))} />
            {f.unit === "min" ? <span aria-hidden>min</span> : null}
          </label>
        ))}
        <label className="sfx-cap" title="How sure BOSS must be that a bug is its own before it changes itself">
          <span>Confidence</span>
          <select className="input" aria-label="Confidence" data-testid="gv-selffix-confidence"
                  value={draft.self_fix_min_confidence ?? v.settings.self_fix_min_confidence}
                  onChange={(e) => setDraft((d) => ({ ...d, self_fix_min_confidence: e.target.value }))}>
            <option value="low">low</option><option value="medium">medium</option><option value="high">high</option>
          </select>
        </label>
        <GatedButton permission="improvement:manage" projectId={projectId} type="submit" className="ag-btn"
                     data-testid="gv-selffix-save" disabled={!!busy || (!changed.length && !confidenceChanged)}
                     title="Save the caps">Save</GatedButton>
        <span className="sfx-sub" data-testid="gv-selffix-spent" title="Spent on self-fixes this calendar month">
          {usd(v.month_spent_usd)} this month
        </span>
      </form>
      {v.readiness ? (
        <section className="sfx-ready" data-testid="gv-selffix-ready" aria-label="Ready to fix itself">
          <div className="sfx-govern-row">
            <span className="sfx-govern-label" title={v.readiness.install.sentence}>Ready to fix itself</span>
            {v.readiness.ready
              ? <span className="cap-badge tone-green" data-testid="gv-selffix-ready-state">Ready</span>
              : <span className="cap-badge tone-amber" data-testid="gv-selffix-ready-state">
                  {v.readiness.pieces.filter((p) => !p.ok).length} to do
                </span>}
          </div>
          {v.readiness.pieces.map((p) => (
            <ReadyPiece key={p.key} piece={p} projectId={projectId} busy={!!busy}
                        turnOn={() => void write("switch", { on: true, why: "switched on in Govern" }, "Self-fix on")}>
              {p.key === "mode" ? (
                <span className="cap-badge tone-muted" data-testid="gv-selffix-auto" title={v.auto_install_why}>
                  {v.auto_install === "on" ? "No ask" : "Asks"}
                </span>
              ) : p.key === "installer" && v.installer.last_seen_at ? (
                <span className="sfx-sub" data-testid="gv-selffix-installer" title="When the host installer last asked BOSS for approved installs">
                  {v.installer.watching ? "watching" : "not watching"}, last seen {localWith(v.installer.last_seen_at, { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" })}
                </span>
              ) : null}
            </ReadyPiece>
          ))}
        </section>
      ) : null}
      <div className="sfx-govern-row" data-testid="gv-selffix-stack">
        <span className="sfx-govern-label" title="Fixes installed on top of this release, in order">Installed fixes</span>
        <span>{v.stack.length} on the stack</span>
        {v.stack.map((s) => (
          <a key={s.self_fix_id} className="ag-link" href={selfFixAreaHref(s.self_fix_id)}
             title={`${s.title ?? s.class_signature}, ${shortSha(s.commit_sha)}`}>#{s.position}</a>
        ))}
        {v.stack.length ? (
          confirmAll ? (
            <>
              <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                           data-testid="gv-selffix-remove-all-yes" disabled={!!busy}
                           title="Back to the plain release, after approval"
                           onClick={async () => { if (await write("remove_all", {}, "Removal needs approval")) setConfirmAll(false); }}>
                Remove all
              </GatedButton>
              <button type="button" className="ag-link" onClick={() => setConfirmAll(false)}>Keep</button>
            </>
          ) : (
            <GatedButton permission="improvement:manage" projectId={projectId} className="ag-btn"
                         data-testid="gv-selffix-remove-all" onClick={() => setConfirmAll(true)}
                         title="Rebuild BOSS as the plain release, without any local fix">Remove all local fixes</GatedButton>
          )
        ) : null}
      </div>
    </div>
  );
}
