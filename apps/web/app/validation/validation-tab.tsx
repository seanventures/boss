"use client";

/**
 * THE VALIDATION TAB (SPEC §2–§3; wireframe B): partner file → findings → fixes → re-run.
 * Four columns at 1440, two at a tablet, four stacked steps on a phone.
 *
 * Data: GET /v1/changes/{p}/{n}/validation (boss.validation/v1 + overrides + re-run state).
 * Each fix carries the §3 override controls (accept · reject (revert hunk) · edit line · waive ·
 * reopen) and the file carries "Revert all to partner's original". A BOSS without the override
 * routes answers 404: every control then shows disabled with a tooltip saying so.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowCounterClockwise, Check, FileText, MagnifyingGlass, PencilSimple, ShieldCheck, ShieldWarning, X,
} from "@phosphor-icons/react";

import { idGet, IdentityError, refusalSentence } from "../../lib/identity/api";
import {
  groupBySeverity, isMissingRoute, lineTitle, lineWords, NOT_DEPLOYED, STALE, overridePath, parseValidation, refTarget, revertAllPath,
  SEV_WORDS, sendOverride, shortSha, sortFindings, validationPath,
  type Finding, type Fix, type Override, type OverrideAction, type Validation,
} from "../../lib/validation-review";
import { actorWords } from "../../lib/actor-words";
import { KindBadge, RefChip, SevChip, Status, jumpToRef } from "./kit";

type ReadState = "loading" | "ready" | "missing" | "error";

/** Read the change's validation; poll every 3 s while a re-run is pending. */
export function useValidation(projectId: string, change: number | string | null, fixture?: unknown) {
  const [data, setData] = useState<Validation | null>(() => (fixture ? parseValidation(fixture) : null));
  const [state, setState] = useState<ReadState>(fixture ? "ready" : change == null ? "missing" : "loading");
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  const refresh = useCallback(() => setN((k) => k + 1), []);
  useEffect(() => {
    if (fixture || change == null) return undefined;
    let live = true;
    idGet<unknown>(validationPath(projectId, change))
      .then((v) => { if (!live) return; setData(parseValidation(v)); setState("ready"); setError(null); })
      .catch((e: unknown) => {
        if (!live) return;
        if (e instanceof IdentityError && isMissingRoute(e.status, e.body)) { setState("missing"); return; }
        setError(refusalSentence(e)); setState("error");
      });
    return () => { live = false; };
  }, [projectId, change, n, fixture]);
  const pending = data?.rerun.state === "pending";
  useEffect(() => {
    if (!pending || fixture) return undefined;
    const t = window.setInterval(refresh, 3000);
    return () => window.clearInterval(t);
  }, [pending, refresh, fixture]);
  return { data, state, error, refresh, setData };
}

// ── 1 partner file ──────────────────────────────────────────────────────────────────────────

/** The lines worth seeing: each finding's line with one line of context; the rest folded. */
function visibleLines(total: number, hits: Set<number>): (number | { gap: [number, number] })[] {
  if (!hits.size) return Array.from({ length: Math.min(total, 12) }, (_, i) => i + 1);
  const keep = new Set<number>();
  for (let i = 1; i <= Math.min(total, 6); i += 1) keep.add(i);
  for (const h of hits) for (const k of [h - 1, h, h + 1]) if (k >= 1 && k <= total) keep.add(k);
  const out: (number | { gap: [number, number] })[] = [];
  let i = 1;
  while (i <= total) {
    if (keep.has(i)) { out.push(i); i += 1; continue; }
    const start = i;
    while (i <= total && !keep.has(i)) i += 1;
    out.push({ gap: [start, i - 1] });
  }
  return out;
}

/** Only the quoted lines, the rest folded (no partner bytes recorded). */
function visibleOnly(total: number, keep: Set<number>): (number | { gap: [number, number] })[] {
  const out: (number | { gap: [number, number] })[] = [];
  let i = 1;
  while (i <= total) {
    if (keep.has(i)) { out.push(i); i += 1; continue; }
    const start = i;
    while (i <= total && !keep.has(i)) i += 1;
    out.push({ gap: [start, i - 1] });
  }
  return out;
}

function PartnerFile({ v }: { v: Validation }) {
  // The partner's bytes when the engine sends them; else the lines each finding quotes (masked).
  const quoted = new Map<number, string>();
  for (const f of v.findings) if (f.location.partner_line != null && f.original != null) quoted.set(f.location.partner_line, f.original);
  const total = v.input.text ? v.input.text.split("\n").length : Math.max(0, ...quoted.keys());
  const lines = v.input.text ? v.input.text.split("\n") : Array.from({ length: total }, (_, i) => quoted.get(i + 1) ?? "");
  const bySev = new Map<number, Finding["severity"]>();
  for (const f of sortFindings(v.findings).reverse()) {
    const ln = f.location.partner_line ?? f.location.line;
    if (ln != null) bySev.set(ln, f.severity);
  }
  const rows = v.input.text ? visibleLines(lines.length, new Set(bySev.keys())) : visibleOnly(lines.length, new Set(quoted.keys()));
  return (
    <div className="vr-col" data-testid="vr-col-partner">
      <div className="vr-col-h"><span className="vr-step">1</span><h3>Partner file</h3><span className="vr-grow" />
        {v.input.sha256 ? <span className="vr-mono vr-dim vr-tiny">sha {v.input.sha256.slice(0, 4)}…</span> : null}</div>
      <div className="vr-col-b">
        <div className="vr-mono vr-muted vr-small" style={{ marginBottom: 8 }}>
          {[v.input.file || "the partner's file", v.input.from ? `from ${v.input.from}` : "", v.input.text ? `${lines.length} lines` : lines.length ? "the lines with findings" : ""].filter(Boolean).join(" · ")}</div>
        {lines.length ? (
          <div className="vr-file" data-testid="vr-partner-lines">
            {rows.map((r) => (typeof r === "number" ? (
              <div key={r} className="vr-ln" data-sev={bySev.get(r)} data-testid={bySev.has(r) ? "vr-partner-hit" : undefined}>
                {bySev.has(r) ? <span className="vr-ln-dot" data-sev={bySev.get(r)} aria-label={bySev.get(r)} /> : <span />}
                <span className="g">{r}</span><span>{lines[r - 1]}</span>
              </div>
            ) : (
              <div key={`g${r.gap[0]}`} className="vr-ln vr-dim"><span /><span className="g">⋮</span><span>{r.gap[1] - r.gap[0] + 1} lines</span></div>
            )))}
          </div>
        ) : <div className="vr-empty"><FileText size={16} aria-hidden />The partner&apos;s bytes were not recorded with this run.</div>}
        <p className="vr-mono vr-dim vr-tiny" style={{ margin: "8px 0 0" }}>Secrets are masked before storage.</p>
      </div>
    </div>
  );
}

// ── 2 findings ──────────────────────────────────────────────────────────────────────────────

function Findings({ v, picked, onPick }: { v: Validation; picked: string; onPick: (id: string) => void }) {
  const groups = groupBySeverity(v.findings);
  const sel = v.findings.find((f) => f.id === picked);
  return (
    <div className="vr-col" data-testid="vr-col-findings">
      <div className="vr-col-h"><span className="vr-step">2</span><h3>{v.validator.id || "Validator"} found {v.before.findings.length || v.findings.length}</h3><span className="vr-grow" />
        {v.before.exit != null ? <span className="vr-mono vr-tiny" style={{ color: v.before.exit ? "var(--v2-red)" : "var(--v2-green)" }}>exit {v.before.exit}</span> : null}</div>
      <div className="vr-col-b">
        {v.before.command ? <div className="vr-mono vr-dim vr-tiny" style={{ marginBottom: 10 }}>$ {v.before.command} {v.before.evidence ? <RefChip id={v.before.evidence} /> : null}</div> : null}
        {groups.length ? groups.map((g) => (
          <div key={g.sev} className="vr-grp" data-testid={`vr-grp-${g.sev}`}>
            <div className="vr-grp-h"><SevChip sev={g.sev} /><span className="vr-muted vr-small">{SEV_WORDS[g.sev]}</span></div>
            {g.items.map((f) => (
              <button key={f.id} type="button" className="vr-f" aria-pressed={picked === f.id} onClick={() => onPick(f.id)} data-testid="vr-finding" data-finding={f.id}
                      title={f.title || undefined}>
                <span className="line" title={lineTitle(f) || undefined}>{lineWords(f) || (f.severity === "P0" ? "file" : "—")}</span>
                <span style={{ minWidth: 0 }}><span className="rid">{f.source.rule_id || f.id}</span><br /><KindBadge kind={f.fix_kind} />
                  {f.introduced ? <> <span className="vr-kind" data-kind="human" title="Reported on the target only: the fix introduced it">new</span></> : null}</span>
                <Status st={f.status} />
              </button>
            ))}
          </div>
        )) : <div className="vr-empty"><ShieldCheck size={16} aria-hidden />No findings.</div>}
        {sel?.why || sel?.title ? (
          <details className="vr-small" open data-testid="vr-why">
            <summary className="vr-muted" style={{ cursor: "pointer" }}>Why: {sel.source.rule_id}{sel.source.rule_doc ? <span className="vr-dim"> ({sel.source.rule_doc.split("#")[0]})</span> : null}</summary>
            <p className="vr-small" style={{ margin: "6px 0 0" }}>{sel.why || sel.title}</p>
          </details>
        ) : null}
      </div>
    </div>
  );
}

// ── 3 fixes, one per finding ────────────────────────────────────────────────────────────────

/** What a press answers: ok, and the words shown NEXT TO THE ROW (never a line at the top). */
export type Sent = { ok: boolean; text: string };
type Send = (f: Finding | null, action: OverrideAction, extra?: { text?: string; reason?: string; approver_id?: string; expires_at?: string }) => Promise<Sent>;

const splitLines = (x: string | null | undefined) => (x == null || x === "" ? [] : x.split("\n"));
/** A masked value ("[SECRET]", "sk_live_••••") written back would commit the mask itself. */
export const MASKED = /\[SECRET\]|••/;

/** The whole hunk a finding's fix sits in: the partner's lines and the branch's lines now.
 *  Several findings can share one hunk (h-04 → V-04, V-05); Edit replaces the whole hunk. */
export function hunkLines(f: Finding, all: Fix[]): { partner: string[]; now: string[] } {
  const inHunk = f.hunk_id ? all.filter((x) => x.hunk_id === f.hunk_id) : [];
  const own = inHunk.length ? inHunk : all.filter((x) => x.finding === f.id);
  if (!own.length) {
    const was = f.original && f.original !== "(none)" ? f.original : "";
    return { partner: splitLines(was), now: splitLines(f.fixed ?? was) };
  }
  return { partner: own.flatMap((x) => splitLines(x.before)), now: own.flatMap((x) => splitLines(x.after)) };
}

function DiffLines({ fixes, f }: { fixes: Fix[]; f: Finding }) {
  // One entry per changed line (engine), or one entry holding several lines (older records).
  const split = (x: string | null) => (x == null || x === "" ? [] : x.split("\n"));
  const minus = fixes.length ? fixes.flatMap((x) => split(x.before)) : split(f.original && f.original !== "(none)" ? f.original : "");
  const plus = fixes.length ? fixes.flatMap((x) => split(x.after)) : split(f.fixed ?? "");
  if (!minus.length && !plus.length) return null;
  return (
    <pre data-testid="vr-diff-lines">
      {minus.map((l, i) => <span key={`d${i}`} className="d">- {l}</span>)}
      {plus.map((l, i) => <span key={`a${i}`} className="a">+ {l}</span>)}
    </pre>
  );
}

function OverrideRecord({ r }: { r: Override }) {
  const verb: Record<string, string> = { accept: "accepted the fix", reject: "rejected the fix (hunk reverted)", edit: "edited the line by hand",
    waive: "waived it", reopen: "reopened it", revert_all: "reverted the file to the partner's original" };
  const at = r.at ? new Date(r.at) : null;
  return (
    <div className="vr-ovrec" id={refTarget(r.id)} data-testid="vr-override-record">
      <div><b>{r.id} · {actorWords(r.actor)}</b> {verb[r.action]}{at && !Number.isNaN(at.getTime()) ? ` · ${at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : ""}</div>
      {r.reason || r.commit_sha || r.rerun_id ? (
        <div style={{ marginTop: 3 }}>{r.reason ? `Why: “${r.reason}”` : null} {r.commit_sha ? <span className="vr-ref">commit {shortSha(r.commit_sha)}</span> : null}{" "}
          {r.rerun_id ? (/^[A-Z]-\d+$/.test(r.rerun_id) ? <RefChip id={r.rerun_id} label={`re-run ${r.rerun_id}`} />
            : <span className="vr-muted vr-small">re-checked</span>) : null}</div>
      ) : null}
      {r.approver && !r.approved_at ? <div style={{ marginTop: 3 }} className="vr-unexplained">Waiting on {actorWords(r.approver)} to approve the P0 waive</div> : null}
    </div>
  );
}

function FixCard({ f, fixes, hunk, record, send, disabled, disabledWhy, guideVersion }: {
  f: Finding; fixes: Fix[]; hunk: { partner: string[]; now: string[] }; record: Override | null; send: Send; disabled: boolean; disabledWhy: string; guideVersion: string;
}) {
  const [mode, setMode] = useState<"" | "edit" | "waive" | "reject">("");
  const startText = (hunk.now.length ? hunk.now : hunk.partner).join("\n");
  const [text, setText] = useState(startText);
  const [reason, setReason] = useState("");
  const [approver, setApprover] = useState("");
  const [expires, setExpires] = useState("");
  const [busy, setBusy] = useState(false);
  // What the last press said, beside this row: an error in red, a success in muted words.
  const [said, setSaid] = useState<{ text: string; tone: string } | null>(null);
  // ONE PRESS AT A TIME (a ref, so a second click in the same tick is already refused).
  const flight = useRef(false);
  const run = async (action: OverrideAction, extra: Parameters<Send>[2] = {}) => {
    if (flight.current) return;
    flight.current = true;
    setBusy(true); setSaid(null);
    try {
      const r = await send(f, action, extra);
      setSaid({ text: r.text, tone: r.ok ? "" : "red" });
      if (r.ok) { setMode(""); setReason(""); }
    } finally {
      flight.current = false;
      setBusy(false);
    }
  };
  const open = (m: typeof mode) => {
    setSaid(null); setReason("");
    if (m === "edit") setText(startText);
    setMode(m);
  };
  const fix = fixes[0];
  const accepted = record?.action === "accept";
  const proposed = Boolean(fix || f.fixed != null) && f.status === "open";
  const waitingApproval = f.status === "open" && record?.action === "waive" && !record.approved_at;
  const settled = f.status === "overridden" || f.status === "waived" || f.status === "reverted";
  const off = disabled || busy;
  const tip = disabled ? disabledWhy : busy ? "Sending…" : undefined;
  const btn = (label: React.ReactNode, onClick: () => void, testid: string, kind?: string) => (
    <button type="button" className="vr-btn" data-kind={kind} disabled={off} title={tip} onClick={onClick} data-testid={testid}>{label}</button>
  );
  const p0 = f.severity === "P0";
  const masked = MASKED.test(text);
  const editWhy = !text.trim() ? "Write the lines first" : masked ? "Replace the masked value first" : !reason.trim() ? "Write why first" : undefined;
  const cancel = <button type="button" className="vr-btn" data-kind="ghost" onClick={() => { setMode(""); setSaid(null); }}>Cancel</button>;
  return (
    <div className="vr-diff" data-sev={f.severity} id={refTarget(f.id)} data-testid="vr-fix" data-finding={f.id} data-status={f.status}>
      <div className="vr-diff-h">
        <SevChip sev={f.severity} /><span className="vr-mono vr-small" title={lineTitle(f) || undefined}>{f.id} {f.source.rule_id}{lineWords(f) ? ` · ${lineWords(f)}` : ""}</span><span className="vr-grow" />
        {waitingApproval ? <Status st="pending" label="waive pending approval" />
          : f.status === "open" ? <KindBadge kind={f.fix_kind} label={f.fix_kind === "auto" && fix ? "auto · --fix" : undefined} /> : <Status st={f.status} />}
      </div>
      {f.title ? <div className="vr-guide">{f.title}</div> : null}
      <DiffLines fixes={fixes} f={f} />
      {f.guide && f.status === "open" ? <div className="vr-guide">guide {guideVersion}: {f.guide}</div> : null}
      {record ? <OverrideRecord r={record} /> : null}
      {mode === "edit" ? (
        <form className="vr-form" onSubmit={(e) => { e.preventDefault(); if (!editWhy && !off) void run("edit", { text, reason }); }} data-testid="vr-edit-form">
          {hunk.partner.length ? (
            <>
              <span className="vr-muted vr-small">Partner&apos;s lines{f.hunk_id ? ` (${f.hunk_id})` : ""} · for reference</span>
              <pre className="vr-ref-lines" data-testid="vr-edit-partner">{hunk.partner.join("\n")}</pre>
            </>
          ) : null}
          <label className="vr-muted vr-small" htmlFor={`vr-edit-${f.id}`}>The whole hunk as it should be <span className="vr-dim">(every line · one commit on the change)</span></label>
          <textarea id={`vr-edit-${f.id}`} className="vr-code" rows={Math.min(14, Math.max(3, text.split("\n").length + 1))} value={text} spellCheck={false}
                    onChange={(e) => setText(e.target.value)} data-testid="vr-edit-text" />
          {masked ? <span className="vr-said" data-tone="amber" data-testid="vr-edit-masked">A masked value is still in the lines. Type the real value or remove the line; the mask would be committed as-is.</span> : null}
          <input aria-label={`Why edit ${f.id}`} placeholder="Why (recorded on the change)" value={reason} onChange={(e) => setReason(e.target.value)} data-testid="vr-edit-reason" />
          <div className="vr-row"><span className="vr-grow" />{cancel}
            <button type="submit" className="vr-btn" data-kind="mint" disabled={off || !!editWhy} title={tip ?? editWhy} data-testid="vr-edit-save">Save lines</button></div>
        </form>
      ) : mode === "reject" ? (
        <form className="vr-form" onSubmit={(e) => { e.preventDefault(); if (reason.trim() && !off) void run("reject", { reason }); }} data-testid="vr-reject-form">
          <label className="vr-muted vr-small" htmlFor={`vr-reject-${f.id}`}>Why reject this fix? <span className="vr-dim">(required · the hunk goes back to the partner&apos;s lines)</span></label>
          <textarea id={`vr-reject-${f.id}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Recorded on the change" data-testid="vr-reject-reason" />
          <div className="vr-row"><span className="vr-grow" />{cancel}
            <button type="submit" className="vr-btn" data-kind="danger" disabled={off || !reason.trim()} title={tip ?? (!reason.trim() ? "Write why first" : undefined)}
                    data-testid="vr-reject-send">Reject and revert hunk</button></div>
        </form>
      ) : mode === "waive" ? (
        <form className="vr-form" onSubmit={(e) => { e.preventDefault(); if (!off && reason.trim() && (!p0 || approver.trim())) void run("waive", { reason, ...(p0 ? { approver_id: approver } : {}), ...(expires ? { expires_at: expires } : {}) }); }} data-testid="vr-waive-form">
          <label className="vr-muted vr-small" htmlFor={`vr-waive-${f.id}`}>Waive reason <span className="vr-dim">(required · recorded on the change)</span></label>
          <textarea id={`vr-waive-${f.id}`} value={reason} onChange={(e) => setReason(e.target.value)} data-testid="vr-waive-reason" />
          {p0 ? (
            <>
              <label className="vr-muted vr-small" htmlFor={`vr-appr-${f.id}`}>Approver <span className="vr-dim">(a second person with validation:waive_p0)</span></label>
              <input id={`vr-appr-${f.id}`} value={approver} onChange={(e) => setApprover(e.target.value)} placeholder="name or email" data-testid="vr-waive-approver" />
            </>
          ) : null}
          <label className="vr-muted vr-small" htmlFor={`vr-exp-${f.id}`}>Expires <span className="vr-dim">(optional · opens a follow-up ticket)</span></label>
          <input id={`vr-exp-${f.id}`} type="date" value={expires} onChange={(e) => setExpires(e.target.value)} data-testid="vr-waive-expires" />
          <div className="vr-row"><span className="vr-dim vr-tiny">{p0 ? "P0: stays open until the approver says yes" : f.severity === "P1" ? "P1: project owner, with a reason" : "P2: no approver needed"}</span><span className="vr-grow" />
            {cancel}
            <button type="submit" className="vr-btn" disabled={off || !reason.trim() || (p0 && !approver.trim())}
                    title={tip ?? (!reason.trim() ? "Write why first" : p0 && !approver.trim() ? "Name the approver first" : undefined)} data-testid="vr-waive-send">{p0 ? `Ask ${approver.trim() || "approver"}` : `Waive ${f.id}`}</button></div>
        </form>
      ) : f.status === "suppressed" ? null : (
        <div className="vr-ov" data-testid="vr-controls">
          {settled ? (
            <>
              {btn(<><PencilSimple size={12} aria-hidden />Edit again</>, () => open("edit"), "vr-edit")}
              {btn("Reopen", () => void run("reopen"), "vr-reopen", "ghost")}
            </>
          ) : (
            <>
              {accepted ? <Status st="fixed" label="accepted" />
                : proposed || f.status === "fixed" ? btn(<><Check size={12} aria-hidden />Accept</>, () => void run("accept"), "vr-accept", "mint") : null}
              {(fix || f.hunk_id) && f.fix_kind !== "none" ? btn(<><X size={12} aria-hidden />Reject (revert hunk)…</>, () => open("reject"), "vr-reject") : null}
              {btn(<><PencilSimple size={12} aria-hidden />Edit lines</>, () => open("edit"), "vr-edit")}
              {f.status === "open" ? btn("Waive…", () => open("waive"), "vr-waive", "ghost") : null}
            </>
          )}
          {busy ? <span className="vr-said">Sending…</span> : null}
        </div>
      )}
      {said ? <p className="vr-said vr-row-said" data-tone={said.tone || undefined} role={said.tone ? "alert" : "status"} data-testid="vr-row-said">{said.text}</p> : null}
    </div>
  );
}

// ── 4 re-run ────────────────────────────────────────────────────────────────────────────────

function Rerun({ v }: { v: Validation }) {
  const after = v.after;
  const open = v.findings.filter((f) => f.status === "open").length;
  const waived = v.findings.filter((f) => f.status === "waived").length;
  const pending = v.rerun.state === "pending";
  const pass = v.contract.filter((c) => c.got != null && String(c.got) === String(c.expect)).length;
  return (
    <div className="vr-col" data-testid="vr-col-rerun">
      <div className="vr-col-h"><span className="vr-step">4</span><h3>Re-run</h3><span className="vr-grow" />
        {after?.exit != null ? <span className="vr-mono vr-tiny" style={{ color: after.exit ? "var(--v2-red)" : "var(--v2-green)" }}>exit {after.exit}</span> : null}</div>
      <div className="vr-col-b">
        {after ? (
          <div className="vr-result" data-open={open ? "" : undefined} data-testid="vr-rerun-result">
            <div className="vr-row"><span className="vr-big">{open}</span><span>open</span><span className="vr-grow" />{waived ? <span className="vr-mono vr-muted vr-small">{waived} waived</span> : null}</div>
            <div className="vr-mono vr-dim vr-tiny" style={{ marginTop: 6 }}>$ {after.command}<br />
              {[after.head_sha || v.head_sha ? `at head ${shortSha(after.head_sha || v.head_sha)}` : "", v.ruleset.version != null ? `ruleset v${v.ruleset.version}` : "",
                after.seconds != null ? `${after.seconds} s` : ""].filter(Boolean).join(" · ")} {after.evidence ? <RefChip id={after.evidence} /> : null}</div>
          </div>
        ) : <div className="vr-empty"><MagnifyingGlass size={16} aria-hidden />No re-run recorded yet.</div>}
        <div className="vr-row" style={{ margin: "10px 0 6px" }} data-testid="vr-rerun-state">
          <span className="vr-pulse" data-live={pending ? "" : undefined} aria-hidden />
          <span className={pending ? "vr-small" : "vr-muted vr-small"}>{pending
            ? `Re-checking after your change${v.rerun.eta_s ? ` · ${v.rerun.eta_s} s` : ""} · Merge is disabled until it is done`
            : "Re-runs automatically after each override; Merge waits while it runs."}</span>
        </div>
        {v.contract.length ? (
          <>
            <div className="vr-k" style={{ marginTop: 14 }}>Contract tests{v.contract_target ? ` · ${v.contract_target}` : ""}</div>
            <table data-testid="vr-contract">
              <thead><tr><th>Case</th><th>Expect</th><th>Got</th><th>Covers</th></tr></thead>
              <tbody>{v.contract.map((c) => (
                <tr key={c.id} id={refTarget(c.id)}>
                  <td className="vr-mono" style={{ wordBreak: "break-all" }}>{c.case}</td><td className="vr-mono">{c.expect ?? "—"}</td>
                  <td className="vr-mono" style={{ color: String(c.got) === String(c.expect) ? "var(--v2-green)" : "var(--v2-red)" }}>{c.got ?? "—"}</td>
                  <td>{c.covers.map((id) => (/^[A-Z]-\d+$/.test(id) ? <RefChip key={id} id={id} /> : <span key={id} className="vr-ref">{id}</span>))}</td>
                </tr>
              ))}</tbody>
            </table>
            <div className="vr-mono vr-small" style={{ color: pass === v.contract.length ? "var(--v2-green)" : "var(--v2-red)", marginTop: 8 }}>{pass} / {v.contract.length} pass</div>
            <p className="vr-note">Each case lists the finding it proves.</p>
          </>
        ) : null}
      </div>
    </div>
  );
}

// ── the tab ─────────────────────────────────────────────────────────────────────────────────

export function ValidationFlow({ v, projectId, change, onChanged, testid = "vr-validation" }: {
  v: Validation; projectId: string; change: number | string; onChanged: () => void; testid?: string;
}) {
  const ordered = useMemo(() => sortFindings(v.findings), [v.findings]);
  const [picked, setPicked] = useState(ordered[0]?.id ?? "");
  const [missing, setMissing] = useState(false);
  const [said, setSaid] = useState<{ text: string; tone: string } | null>(null);
  const [reverting, setReverting] = useState(false);
  const [revertWhy, setRevertWhy] = useState("");
  const pending = v.rerun.state === "pending";
  const fixesBy = useMemo(() => {
    const m = new Map<string, Fix[]>();
    for (const x of v.fixes) if (x.finding) m.set(x.finding, [...(m.get(x.finding) ?? []), x]);
    return m;
  }, [v.fixes]);
  // Changed lines no finding explains (SPEC §1.4 rule 4): shown in amber, never hidden.
  const unexplained = v.fixes.filter((x) => !x.finding);
  const recordOf = (f: Finding): Override | null => f.override ?? [...v.overrides].reverse().find((r) => r.finding_id === f.id) ?? null;
  // ONE OVERRIDE AT A TIME on the change: a press holds every control until the page has read
  // the result back (the next `v`), so a press after "Confirm it's you" — or a second click —
  // never sends the same override twice.
  const flight = useRef(false);
  const [sentAt, setSentAt] = useState<Validation | null>(null);
  useEffect(() => { setSentAt((was) => (was && was !== v ? null : was)); }, [v]);
  const send: Send = async (f, action, extra = {}) => {
    if (flight.current) return { ok: false, text: "The last change is still being sent." };
    flight.current = true;
    try {
      const path = f ? overridePath(projectId, change, f.id) : revertAllPath(projectId, change);
      const r = await sendOverride(path, { ...(f ? { action } : {}), ...extra, head_sha: v.head_sha });
      if (r.missing) { setMissing(true); return { ok: false, text: NOT_DEPLOYED }; }
      if (r.stale) { onChanged(); return { ok: false, text: STALE }; }
      if (!r.ok) return { ok: false, text: r.error ?? "That did not go through." };
      const waitsOn = Boolean(r.body && (r.body as { decision?: unknown }).decision) && action === "waive" && f?.severity === "P0";
      setSentAt(v);
      onChanged();
      return { ok: true, text: waitsOn ? `${f!.id}: waive sent for approval · it stays open until they say yes.`
        : `${f ? `${f.id}: ${action}` : "Reverted to the partner's original"} recorded · re-checking now.` };
    } finally {
      flight.current = false;
    }
  };
  const pick = (id: string) => { setPicked(id); jumpToRef(id); };
  const disabledWhy = missing ? NOT_DEPLOYED : pending || sentAt === v ? "Re-checking after the last change" : "";
  const flowRef = useRef<HTMLDivElement>(null);
  const extra = ordered.filter((f) => !fixesBy.has(f.id) && f.status === "fixed" && f.fix_kind === "auto");
  return (
    <div className="vr" data-testid={testid}>
      <div className="vr-row" style={{ marginBottom: 12 }}>
        <span className="vr-pill">validator <b>{v.validator.id || "—"}</b>{v.validator.version ? ` ${v.validator.version}` : ""}</span>
        {v.validator.adapter ? <span className="vr-pill">adapter {v.validator.adapter}</span> : null}
        {v.ruleset.id ? <span className="vr-pill">ruleset {v.ruleset.id}{v.ruleset.version != null ? ` v${v.ruleset.version}` : ""}</span> : null}
        {v.target ? <span className="vr-pill">target {v.target}</span> : null}
        <span className="vr-grow" />
        {reverting ? (
          <form className="vr-row" onSubmit={(e) => { e.preventDefault(); if (revertWhy.trim()) void send(null, "revert_all", { reason: revertWhy }).then((r) => { setSaid({ text: r.text, tone: r.ok ? "" : "red" }); if (r.ok) setReverting(false); }); }}>
            <input className="vr-input" aria-label="Why revert the whole file" placeholder="Why revert the whole file?" value={revertWhy} onChange={(e) => setRevertWhy(e.target.value)} data-testid="vr-revert-all-reason" />
            <button type="button" className="vr-btn" data-kind="ghost" onClick={() => setReverting(false)}>Cancel</button>
            <button type="submit" className="vr-btn" data-kind="danger" disabled={!revertWhy.trim() || !!disabledWhy} title={disabledWhy || (!revertWhy.trim() ? "Say why first" : undefined)} data-testid="vr-revert-all-send">Revert all</button>
          </form>
        ) : (
          <button type="button" className="vr-btn" data-kind="danger" disabled={!!disabledWhy} title={disabledWhy || "Put the partner's exact bytes back, as one commit"}
                  onClick={() => setReverting(true)} data-testid="vr-revert-all"><ArrowCounterClockwise size={13} aria-hidden />Revert all to partner&apos;s original</button>
        )}
        {said ? <span className="vr-said" data-tone={said.tone || undefined} role={said.tone ? "alert" : "status"} data-testid="vr-said">{said.text}</span> : null}
      </div>
      {v.guard || v.error ? (
        <div className="vr-verdict" data-state="invalid" style={{ marginBottom: 12 }} data-testid="vr-guard">
          <b>{v.guard?.code || "validation error"}</b>
          <span className="vr-muted">{v.guard ? `The target equals ${v.guard.source || "another repo file"}, not the partner's file.` : v.error}</span>
        </div>
      ) : null}
      <div className="vr-flow" ref={flowRef}>
        <PartnerFile v={v} />
        <Findings v={v} picked={picked} onPick={pick} />
        <div className="vr-col" data-testid="vr-col-fixes">
          <div className="vr-col-h"><span className="vr-step">3</span><h3>Fixes, one per finding</h3><span className="vr-grow" />
            <span className="vr-mono vr-muted vr-tiny">{new Set(v.fixes.map((x) => x.hunk_id).filter(Boolean)).size} hunks</span></div>
          <div className="vr-col-b">
            {ordered.filter((f) => !extra.includes(f)).map((f) => (
              <FixCard key={f.id} f={f} fixes={fixesBy.get(f.id) ?? []} hunk={hunkLines(f, v.fixes)} record={recordOf(f)} send={send} disabled={!!disabledWhy} disabledWhy={disabledWhy}
                       guideVersion={v.ruleset.version != null ? `v${v.ruleset.version}` : ""} />
            ))}
            {unexplained.length ? (
              <div className="vr-diff" data-testid="vr-unexplained-fixes">
                <div className="vr-diff-h"><span className="vr-unexplained vr-small">Unexplained change · {unexplained.length} line{unexplained.length === 1 ? "" : "s"}</span><span className="vr-grow" />
                  <span className="vr-mono vr-dim vr-tiny">{[...new Set(unexplained.map((x) => x.hunk_id))].join(" ")}</span></div>
                <pre>{unexplained.flatMap((x, i) => [x.before != null ? <span key={`d${i}`} className="d">- {x.before}</span> : null, x.after != null ? <span key={`a${i}`} className="a">+ {x.after}</span> : null])}</pre>
              </div>
            ) : null}
            {extra.length ? <div className="vr-mono vr-dim vr-tiny">+ {extra.map((f) => `${f.id} ${f.source.rule_id}`).join(", ")} (auto, in the hunks above)</div> : null}
            {!ordered.length ? <div className="vr-empty"><ShieldCheck size={16} aria-hidden />Nothing to fix.</div> : null}
          </div>
        </div>
        <Rerun v={v} />
      </div>
    </div>
  );
}

/** The tab on the change page: reads the validation and draws the flow, or a clean empty state. */
export function ValidationTab({ projectId, change, fixture }: { projectId: string; change: number | string; fixture?: unknown }) {
  const { data, state, error, refresh } = useValidation(projectId, change, fixture);
  if (state === "loading") return <div className="vr"><div className="vr-empty" role="status">Reading the validation…</div></div>;
  if (state === "missing") {
    return <div className="vr"><div className="vr-empty" data-testid="vr-validation-missing"><ShieldWarning size={18} aria-hidden />
      Validation results are not on this BOSS yet. Once the engine records the Validate step, gwlint&apos;s findings, fixes and re-run show here.</div></div>;
  }
  if (state === "error") return <div className="vr"><div className="vr-empty" role="alert">{error}</div></div>;
  if (!data) {
    return <div className="vr"><div className="vr-empty" data-testid="vr-validation-empty"><ShieldCheck size={18} aria-hidden />
      No validator ran on this change. A workflow with a Validate step records its findings here.</div></div>;
  }
  return <ValidationFlow v={data} projectId={projectId} change={change} onChanged={refresh} />;
}
