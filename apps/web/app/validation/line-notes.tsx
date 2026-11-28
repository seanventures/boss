"use client";

/**
 * VALIDATION FINDINGS AS NOTES ON THEIR LINES (docs/design/workflow-library/DESIGN.md decision 4,
 * wireframe screen 2). Each finding sits in Files changed under its line, like a reviewer's
 * comment: severity · rule · "fixed by tool/agent/you" or "Needs you" with the proposed fix, and
 * the §3 override actions Accept · Revert line · Edit · Remove lines · Waive. The target file's
 * header carries "Revert to original".
 *
 * Data: GET /v1/changes/{p}/{n}/validation (boss.validation/v1 + overrides + re-run state). One
 * override at a time on the change (a press holds every note until the page has read the result
 * back), so a press after "Confirm it's you", or a second click, never sends the same override
 * twice. A BOSS without the override routes answers 404: every action is then disabled, saying so.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowCounterClockwise, Check, PencilSimple, Trash, X } from "@phosphor-icons/react";

import { idGet, IdentityError, refusalSentence } from "../../lib/identity/api";
import {
  isMissingRoute, isProposalGone, lineTitle, NOT_DEPLOYED, OVERRIDE_VERBS, PROPOSAL_GONE, STALE, overridePath, parseValidation,
  refTarget, removalBlock, removalLines, revertAllPath, sendOverride, sendRemove, shortSha,
  type Finding, type Fix, type Override, type OverrideAction, type OverrideResult, type Validation,
} from "../../lib/validation-review";
import { noteWords, placeNotes } from "../../lib/line-notes";
import { localWith } from "../../lib/hydration-state";
import type { DiffFile } from "../../lib/diff-view";
import type { Pins } from "../changes/[projectId]/[number]/pins";
import { ActorName, SevChip } from "./kit";

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
    idGet<unknown>(`/v1/changes/${encodeURIComponent(projectId)}/${encodeURIComponent(String(change))}/validation`)
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

/** What a press answers: ok, and the words shown beside the note (never a line at the top). */
export type Sent = { ok: boolean; text: string; code?: string };
export type Send = (f: Finding | null, action: OverrideAction, extra?: { text?: string; reason?: string; approver_id?: string; expires_at?: string }) => Promise<Sent>;

/** THE ONE SENDER for every note on the change: single-flight, stale-safe, re-reads after. */
export function useOverrides(v: Validation, projectId: string, change: number | string, onChanged: () => void): { send: Send; disabledWhy: string } {
  const [missing, setMissing] = useState(false);
  const flight = useRef(false);
  const [sentAt, setSentAt] = useState<Validation | null>(null);
  useEffect(() => { setSentAt((was) => (was && was !== v ? null : was)); }, [v]);
  const send: Send = async (f, action, extra = {}) => {
    if (flight.current) return { ok: false, text: "The last change is still being sent." };
    flight.current = true;
    try {
      const path = f ? overridePath(projectId, change, f.id) : revertAllPath(projectId, change);
      const r: OverrideResult = f && action === "remove"
        ? await sendRemove(path, { reason: extra.reason ?? "", head_sha: v.head_sha })
        : await sendOverride(path, { ...(f ? { action } : {}), ...extra, head_sha: v.head_sha });
      if (r.missing) { setMissing(true); return { ok: false, text: NOT_DEPLOYED }; }
      if (r.stale) { onChanged(); return { ok: false, text: STALE }; }
      if (!r.ok) return { ok: false, text: r.error ?? "That did not go through.", code: r.code };
      const waitsOn = Boolean(r.body && (r.body as { decision?: unknown }).decision) && action === "waive" && f?.severity === "P0";
      setSentAt(v);
      onChanged();
      return { ok: true, text: waitsOn ? `${f!.id}: waive sent for approval · it stays open until they say yes.`
        : `${f ? `${f.id} ${OVERRIDE_VERBS[action]}` : "Reverted to the original file"} · re-checking now.` };
    } finally {
      flight.current = false;
    }
  };
  const disabledWhy = missing ? NOT_DEPLOYED : v.rerun.state === "pending" || sentAt === v ? "Re-checking after the last change" : "";
  return { send, disabledWhy };
}

const splitLines = (x: string | null | undefined) => (x == null || x === "" ? [] : x.split("\n"));
/** A masked value ("[SECRET]", "sk_live_••••") written back would commit the mask itself. */
export const MASKED = /\[SECRET\]|••/;

/** The whole hunk a finding's fix sits in: the original lines and the branch's lines now.
 *  Several findings can share one hunk (h-04 → V-04, V-05); Edit replaces the whole hunk. */
export function hunkLines(f: Finding, all: Fix[]): { original: string[]; now: string[] } {
  const inHunk = f.hunk_id ? all.filter((x) => x.hunk_id === f.hunk_id) : [];
  const own = inHunk.length ? inHunk : all.filter((x) => x.finding === f.id);
  if (!own.length) {
    const was = f.original && f.original !== "(none)" ? f.original : "";
    return { original: splitLines(was), now: splitLines(f.fixed ?? was) };
  }
  return { original: own.flatMap((x) => splitLines(x.before)), now: own.flatMap((x) => splitLines(x.after)) };
}

const recordOf = (v: Validation, f: Finding): Override | null =>
  f.override ?? [...v.overrides].reverse().find((r) => r.finding_id === f.id) ?? null;

/**
 * The finding's own change: its fixes; else the hunk it points at; else its line. A line still on
 * the branch (an open finding, or one the fix left as it was) is context, never a "-".
 */
function FindingDiff({ f, all }: { f: Finding; all: Fix[] }) {
  const own = all.filter((x) => x.finding === f.id);
  const inHunk = own.length ? own : f.hunk_id ? all.filter((x) => x.hunk_id === f.hunk_id) : [];
  // A note shows ITS OWN lines: a finding that went with a removed block shows that block's
  // removal, never the other findings' lines sharing the hunk (dry run 4, V-08 showed the CORS fix).
  const atLine = own.length ? [] : inHunk.filter((x) => x.line != null && x.line === f.location.partner_line);
  const hunk = atLine.length ? atLine : inHunk;
  const was = f.original && f.original !== "(none)" ? f.original : "";
  const closed = f.status === "fixed" || f.status === "overridden";
  if (!hunk.length && was && (f.fixed == null ? !closed : f.fixed.trim() === was.trim())) {
    return <pre className="ln-diff" data-testid="vr-diff-lines" data-kind="unchanged">{splitLines(was).map((l, i) => <span key={i} className="c">  {l}</span>)}</pre>;
  }
  const minus = hunk.length ? hunk.flatMap((x) => splitLines(x.before)) : splitLines(was);
  const plus = hunk.length ? hunk.flatMap((x) => splitLines(x.after)) : splitLines(f.fixed ?? "");
  if (!minus.length && !plus.length) return null;
  return (
    <pre className="ln-diff" data-testid="vr-diff-lines">
      {minus.map((l, i) => <span key={`d${i}`} className="d">- {l}</span>)}
      {plus.map((l, i) => <span key={`a${i}`} className="a">+ {l}</span>)}
    </pre>
  );
}

function OverrideLine({ r }: { r: Override }) {
  const verb: Record<OverrideAction, string> = { accept: "accepted the fix", reject: "reverted the line", edit: "edited the lines",
    waive: "waived it", reopen: "reopened it", revert_all: "reverted the file to the original", remove: "removed the lines" };
  const at = r.at ? new Date(r.at) : null;
  return (
    <div className="ln-rec" id={refTarget(r.id)} data-testid="vr-override-record" data-action={r.action}>
      <span className="vr-mono">{r.id} · </span><b><ActorName id={r.actor} name={r.actor_name} /></b> {verb[r.action] ?? OVERRIDE_VERBS[r.action]}
      {at && !Number.isNaN(at.getTime()) ? ` · ${localWith(at.getTime(), { hour: "2-digit", minute: "2-digit" })}` : ""}
      {r.reason ? ` · “${r.reason}”` : ""}{r.commit_sha ? <span className="vr-mono vr-dim"> · commit {shortSha(r.commit_sha)}</span> : null}
      {r.rerun_id ? <span className="vr-mono vr-dim"> · re-run{/^[A-Z]-\d+$/.test(r.rerun_id) ? ` ${r.rerun_id}` : ""}</span> : null}
      {r.approver && !r.approved_at ? <span className="vr-unexplained"> · waiting on <ActorName id={r.approver} /> to approve</span> : null}
    </div>
  );
}

/** ONE FINDING, PINNED ON ITS LINE. */
export function FindingNote({ f, v, send, disabled, disabledWhy }: {
  f: Finding; v: Validation; send: Send; disabled: boolean; disabledWhy: string;
}) {
  const [mode, setMode] = useState<"" | "edit" | "waive" | "reject" | "remove">("");
  // The engine said the agent's proposal cannot be applied any more (409 proposal_gone).
  const [gone, setGone] = useState(false);
  const hunk = useMemo(() => hunkLines(f, v.fixes), [f, v.fixes]);
  const startText = (hunk.now.length ? hunk.now : hunk.original).join("\n");
  const [text, setText] = useState(startText);
  const [reason, setReason] = useState("");
  const [approver, setApprover] = useState("");
  const [expires, setExpires] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; tone: string } | null>(null);
  // ONE PRESS AT A TIME (a ref, so a second click in the same tick is already refused).
  const flight = useRef(false);
  const run = async (action: OverrideAction, extra: Parameters<Send>[2] = {}) => {
    if (flight.current) return;
    flight.current = true;
    setBusy(true); setSaid(null);
    try {
      const r = await send(f, action, extra);
      if (!r.ok && action === "accept" && r.code && isProposalGone(r.code)) { setGone(true); return; }
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
  const record = recordOf(v, f);
  const fix = v.fixes.find((x) => x.finding === f.id);
  const accepted = record?.action === "accept";
  // The agent's refused change for a `human` / `never` rule: Accept commits it, while it applies.
  const refused = f.status === "open" ? f.proposed : null;
  const refusedOk = !!refused?.applicable && !gone;
  const proposed = f.status === "open" && (refused ? refusedOk : Boolean(fix || f.fixed != null));
  // "Remove lines": a "Needs you" finding still open (a route to drop, a secret to delete).
  const block = f.status === "open" && f.fix_kind === "human" ? removalBlock(v.input.text, f) : null;
  const removing = block ? removalLines(block, f) : [];
  const span = (b: { from: number; to: number }) => (b.to > b.from ? `lines ${b.from}–${b.to}` : `line ${b.from}`);
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
  const words = noteWords(f, record);
  return (
    <div className="ln-note vr" data-tone={words.tone} data-sev={f.severity} id={refTarget(f.id)} data-testid="vr-fix" data-finding={f.id} data-status={f.status}>
      <div className="ln-h">
        <SevChip sev={f.severity} />
        <b className="vr-mono ln-rule" title={[f.id, lineTitle(f)].filter(Boolean).join(" · ")}>{f.source.rule_id || f.source.name || f.id}</b>
        <span className="ln-by" data-tone={words.tone} data-testid="vr-note-by">{words.text}</span>
        {f.title ? <span className="ln-title" title={f.why || f.title}>{f.title}</span> : null}
      </div>
      {refused ? (
        <div className="ln-prop" data-testid="vr-proposed" data-applicable={refusedOk ? "" : undefined}>
          <span className="vr-muted vr-small">Proposed fix (not applied)</span>
          {refusedOk ? null : <span className="vr-said" data-tone="amber" role="note" data-testid="vr-proposal-gone"> · {PROPOSAL_GONE}</span>}
          <pre data-testid="vr-proposed-lines">
            {splitLines(refused.before).filter(Boolean).map((l, i) => <span key={`d${i}`} className="d">- {l}</span>)}
            {splitLines(refused.after).filter(Boolean).map((l, i) => <span key={`a${i}`} className="a">+ {l}</span>)}
          </pre>
        </div>
      ) : block && mode !== "remove" ? (
        <div className="ln-prop" data-testid="vr-proposed-remove">
          <span className="vr-muted vr-small">Proposed fix: remove {span(block)} of the original file</span>
          <pre>{block.lines.map((l, i) => <span key={i} className="d">- {l}</span>)}</pre>
        </div>
      ) : null}
      {!refused && !(block && mode !== "remove") ? <FindingDiff f={f} all={v.fixes} /> : null}
      {f.guide && f.status === "open" ? <div className="vr-guide" data-testid="vr-guide">guide{v.ruleset.version != null ? ` v${v.ruleset.version}` : ""}: {f.guide}</div> : null}
      {record ? <OverrideLine r={record} /> : null}
      {mode === "remove" && block ? (
        <form className="vr-form" onSubmit={(e) => { e.preventDefault(); if (reason.trim() && !off) void run("remove", { reason }); }} data-testid="vr-remove-form">
          <span className="vr-muted vr-small" data-testid="vr-remove-where"
                title={block.offset ? `On the branch: ${span({ from: block.from + block.offset, to: block.to + block.offset })} of ${v.target || "the target"}` : undefined}>
            Deletes {removing.length > block.lines.length ? `the block at line ${block.from}` : span(block)} of the original file{v.target ? ` from ${v.target}` : ""} · one commit on the change</span>
          <pre className="vr-ref-lines vr-remove-lines" data-testid="vr-remove-lines">
            {removing.map((l, i) => <span key={i} className="d">- {l}</span>)}
          </pre>
          <label className="vr-muted vr-small" htmlFor={`vr-remove-${f.id}`}>Why remove them? <span className="vr-dim">(required · recorded on the change)</span></label>
          <textarea id={`vr-remove-${f.id}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Recorded on the change" data-testid="vr-remove-reason" />
          <div className="vr-row"><span className="vr-grow" />{cancel}
            <button type="submit" className="vr-btn" data-kind="danger" disabled={off || !reason.trim()} title={tip ?? (!reason.trim() ? "Write why first" : undefined)}
                    data-testid="vr-remove-send"><Trash size={12} aria-hidden />Remove {removing.length} line{removing.length === 1 ? "" : "s"}</button></div>
        </form>
      ) : mode === "edit" ? (
        <form className="vr-form" onSubmit={(e) => { e.preventDefault(); if (!editWhy && !off) void run("edit", { text, reason }); }} data-testid="vr-edit-form">
          {hunk.original.length ? (
            <>
              <span className="vr-muted vr-small">Original lines · for reference</span>
              <pre className="vr-ref-lines" data-testid="vr-edit-original">{hunk.original.join("\n")}</pre>
            </>
          ) : null}
          <label className="vr-muted vr-small" htmlFor={`vr-edit-${f.id}`}>The lines as they should be <span className="vr-dim">(one commit on the change)</span></label>
          <textarea id={`vr-edit-${f.id}`} className="vr-code" rows={Math.min(14, Math.max(3, text.split("\n").length + 1))} value={text} spellCheck={false}
                    onChange={(e) => setText(e.target.value)} data-testid="vr-edit-text" />
          {masked ? <span className="vr-said" data-tone="amber" data-testid="vr-edit-masked">A masked value is still in the lines. Type the real value or remove the line; the mask would be committed as-is.</span> : null}
          <input aria-label={`Why edit ${f.id}`} placeholder="Why (recorded on the change)" value={reason} onChange={(e) => setReason(e.target.value)} data-testid="vr-edit-reason" />
          <div className="vr-row"><span className="vr-grow" />{cancel}
            <button type="submit" className="vr-btn" data-kind="mint" disabled={off || !!editWhy} title={tip ?? editWhy} data-testid="vr-edit-save">Save lines</button></div>
        </form>
      ) : mode === "reject" ? (
        <form className="vr-form" onSubmit={(e) => { e.preventDefault(); if (reason.trim() && !off) void run("reject", { reason }); }} data-testid="vr-reject-form">
          <label className="vr-muted vr-small" htmlFor={`vr-reject-${f.id}`}>Why revert this line? <span className="vr-dim">(required · it goes back to the original)</span></label>
          <textarea id={`vr-reject-${f.id}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Recorded on the change" data-testid="vr-reject-reason" />
          <div className="vr-row"><span className="vr-grow" />{cancel}
            <button type="submit" className="vr-btn" data-kind="danger" disabled={off || !reason.trim()} title={tip ?? (!reason.trim() ? "Write why first" : undefined)}
                    data-testid="vr-reject-send">Revert line</button></div>
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
        <div className="ln-acts" data-testid="vr-controls">
          {settled ? (
            <>
              {btn(<><PencilSimple size={12} aria-hidden />Edit</>, () => open("edit"), "vr-edit")}
              {btn("Reopen", () => void run("reopen"), "vr-reopen", "ghost")}
            </>
          ) : (
            <>
              {accepted ? <span className="vr-st" data-st="fixed">accepted</span>
                : proposed || f.status === "fixed" ? btn(<><Check size={12} aria-hidden />Accept</>, () => void run("accept"), "vr-accept", "mint") : null}
              {(fix || f.hunk_id) && f.fix_kind !== "none" ? btn(<><X size={12} aria-hidden />Revert line</>, () => open("reject"), "vr-reject") : null}
              {btn(<><PencilSimple size={12} aria-hidden />Edit</>, () => open("edit"), "vr-edit")}
              {block ? btn(<><Trash size={12} aria-hidden />Remove lines</>, () => open("remove"), "vr-remove", "danger") : null}
              {f.status === "open" ? btn("Waive", () => open("waive"), "vr-waive", "ghost") : null}
            </>
          )}
          {busy ? <span className="vr-said">Sending…</span> : null}
        </div>
      )}
      {said ? <p className="vr-said ln-said" data-tone={said.tone || undefined} role={said.tone ? "alert" : "status"} data-testid="vr-row-said">{said.text}</p> : null}
    </div>
  );
}

/** "Revert to original" on the target file's header: the original bytes back, as one commit. */
export function RevertToOriginal({ send, disabledWhy, commits = 0, masked = false }: { send: Send; disabledWhy: string; commits?: number; masked?: boolean }) {
  const [asking, setAsking] = useState(false);
  const [why, setWhy] = useState("");
  const [said, setSaid] = useState<{ text: string; tone: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const go = async () => {
    if (busy || !why.trim()) return;
    setBusy(true);
    try {
      const r = await send(null, "revert_all", { reason: why });
      setSaid({ text: r.text, tone: r.ok ? "" : "red" });
      if (r.ok) { setAsking(false); setWhy(""); }
    } finally { setBusy(false); }
  };
  return (
    <span className="vr ln-revert" data-testid="vr-revert-original">
      {commits ? <span className="vr-muted vr-small" data-testid="vr-original-first">{commits} commits: original as given{masked ? " (secrets masked)" : ""} → fixes</span> : null}
      {asking ? (
        <form className="vr-row" onSubmit={(e) => { e.preventDefault(); void go(); }}>
          <input className="vr-input" aria-label="Why revert to the original" placeholder="Why revert to the original?" value={why}
                 onChange={(e) => setWhy(e.target.value)} data-testid="vr-revert-all-reason" />
          <button type="button" className="vr-btn" data-kind="ghost" onClick={() => setAsking(false)}>Cancel</button>
          <button type="submit" className="vr-btn" data-kind="danger" disabled={busy || !why.trim() || !!disabledWhy}
                  title={disabledWhy || (!why.trim() ? "Say why first" : undefined)} data-testid="vr-revert-all-send">Revert</button>
        </form>
      ) : (
        <button type="button" className="vr-btn" data-kind="ghost" disabled={!!disabledWhy} title={disabledWhy || "Put the original file back, as one commit"}
                onClick={() => setAsking(true)} data-testid="vr-revert-all"><ArrowCounterClockwise size={12} aria-hidden />Revert to original</button>
      )}
      {said ? <span className="vr-said" data-tone={said.tone || undefined} role={said.tone ? "alert" : "status"} data-testid="vr-said">{said.text}</span> : null}
    </span>
  );
}

/** The pins Files changed draws: every finding under its line, Revert to original on the target. */
export function useValidationPins(v: Validation | null, projectId: string, change: number | string, onChanged: () => void,
                                  originalCommits = 0): Pins | null {
  const empty = useMemo(() => ({ ...EMPTY_V }), []);
  const { send, disabledWhy } = useOverrides(v ?? empty, projectId, change, onChanged);
  const sendRef = useRef(send);
  sendRef.current = send;
  return useMemo(() => {
    if (!v) return null;
    const stable: Send = (...a) => sendRef.current(...a);
    const note = (f: Finding) => <FindingNote key={f.id} f={f} v={v} send={stable} disabled={!!disabledWhy} disabledWhy={disabledWhy} />;
    return {
      forFile: (file: DiffFile) => {
        const { at, loose } = placeNotes(v.findings.filter((f) => f.status !== "suppressed"), file, v.target);
        return {
          row: (side, line) => { const list = at.get(`${side}:${line}`); return list ? list.map(note) : null; },
          top: loose.length ? <div className="ln-loose" data-testid="vr-loose-notes">{loose.map(note)}</div> : null,
        };
      },
      open: v.target ? [v.target] : [],
      head: (path: string) => (v.target && path === v.target
        ? <RevertToOriginal send={stable} disabledWhy={disabledWhy} commits={originalCommits} masked={v.input.from === "upload" || MASKED.test(v.input.text)} /> : null),
    };
  }, [v, disabledWhy, originalCommits]);
}

const EMPTY_V = parseValidation({ validator: { id: "-" } }) as Validation;
