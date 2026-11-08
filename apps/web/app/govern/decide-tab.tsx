"use client";

/**
 * GOVERN › WHERE PEOPLE DECIDE (docs/product/wireframes/agentic-factory/1-where-people-decide.html).
 *
 *   ✋ Where people decide                                  ● Agents on   [■ Stop all]
 *   142 handled by agents · 6 asked a person · 1 waiting on you: Merge PR #3 ›   What agents did ›
 *   [🚀 Production deploy][⑂ Merge][$ Spend][🔑 Secrets][📣 Outside][🗑 Delete][🪪 Who][🔒 This list]
 *   └ a card's popover: who decides + backup · scope · quiet window · agents may · on my behalf ·
 *     Full auto (locked) · trail with diagram links · full audit trail · Save · passkey
 *   Change a rule in plain words  [ multi-line … ]  → Rule change card → Edit · Apply N changes
 *
 * The cards are the ONE list in code (services/decide_list.py) — the same list the decision flow
 * enforces. A card may only tighten an entry; the API refuses looser (422), and the card says why.
 * Agents' own activity lives on Agents at work ("What agents did ›"); the A0–A3 grid lives under
 * More › Per-area autonomy.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowSquareOut, CaretDown, CheckCircle, CurrencyDollar, FlowArrow, GitMerge, HandPalm, IdentificationCard, Key,
  LockSimple, Megaphone, PencilSimple, Plus, RocketLaunch, Scales, ShieldCheck, StopCircle, Trash, WarningCircle, X,
  type Icon,
} from "@phosphor-icons/react";

import { useConsoleLive } from "../../lib/console-live";
import { decisionHref } from "../../lib/command-center";
import { AREA, parseSettings, polled, settingsPath } from "../../lib/ambient";
import {
  DAYS, STEP_WORDS, ago, applyEdits, boardPath, browserTz, parseBoard, replayEdits, saveRule, understandWords, when, whoWords,
  windowWords, type Board, type Card, type Edit, type Person, type ReplayRow, type Rule, type Understood, type Who, type Window,
} from "../../lib/decide";
import { logHref } from "../../lib/log-url";
import { usePolledRead } from "../../lib/log-stream";
import { AgentAvatar } from "../agent-avatar";
import { KillSwitch } from "../agent-ops/autonomy-panel";
import { showToast } from "../client-forms";
import "./decide.css";

const ICONS: Record<string, Icon> = {
  "rocket-launch": RocketLaunch, "git-merge": GitMerge, "currency-dollar": CurrencyDollar, key: Key,
  megaphone: Megaphone, trash: Trash, "identification-card": IdentificationCard, "lock-simple": LockSimple,
};

function orgParam(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("org");
}
function areaLink(area: string, extra: Record<string, string> = {}): string {
  const out = new URLSearchParams();
  const org = orgParam();
  if (org) out.set("org", org);
  out.set("area", area);
  for (const [k, v] of Object.entries(extra)) out.set(k, v);
  return `/?${out.toString()}`;
}

function Face({ who, size = 18 }: { who: { name?: string } | null; size?: number }) {
  return <AgentAvatar variant={who ? "person" : "you"} size={size} tip={who?.name || "You"} />;
}

function Mini({ bars }: { bars: number[] }) {
  const max = Math.max(1, ...bars);
  return (
    <span className="wd-mini" aria-hidden>
      {bars.map((b, i) => <i key={i} style={{ height: `${Math.max(2, Math.round((b / max) * 16))}px` }} data-zero={b === 0 || undefined} />)}
    </span>
  );
}

// ── one card ────────────────────────────────────────────────────────────────────────────────

function ListCard({ card, open, onOpen }: { card: Card; open: boolean; onOpen: () => void }) {
  const I = ICONS[card.icon] ?? HandPalm;
  if (card.fixed) {
    return (
      <button type="button" className="wd-b" data-lock data-open={open || undefined} onClick={onOpen} data-testid="wd-card"
              data-kind={card.key} aria-expanded={open} title="Fixed: changing the list is always a person, with a passkey">
        <span className="wd-b-h"><span className="wd-bi"><I size={16} weight="bold" /></span><b>{card.title}</b><LockSimple size={13} className="wd-caret" /></span>
        <span className="wd-def">{card.line}</span>
        <span className="wd-meta"><span className="wd-tag">fixed</span><span className="wd-spacer" />
          {card.last_change ? `last change ${ago(card.last_change.at)}` : "never changed"}</span>
        <span className="wd-why">{card.why}</span>
      </button>
    );
  }
  const rule = card.rule;
  const windows = rule?.quiet ?? [];
  const step = STEP_WORDS[card.step_up];
  return (
    <button type="button" className="wd-b" data-open={open || undefined} onClick={onOpen} data-testid="wd-card" data-kind={card.key}
            aria-expanded={open} title={`${card.title} · click to change`}>
      <span className="wd-b-h"><span className="wd-bi"><I size={16} weight="bold" /></span><b>{card.title}</b><CaretDown size={12} className="wd-caret" /></span>
      <span className="wd-def" data-testid="wd-card-line">{card.line}</span>
      <span className="wd-meta">
        <span className="wd-scope" title="Where this rule is set">
          <span aria-selected="true">Install</span>
          {card.scope.per_project ? <span>{card.scope.projects.length ? `${card.scope.projects.length} project${card.scope.projects.length > 1 ? "s" : ""}` : "project"}</span> : null}
        </span>
        <Face who={card.decider.kind === "person" ? { name: card.decider.label } : card.decider.people[0] ?? null} />
        <span className="wd-who" data-testid="wd-card-who">{card.decider.label}{step ? ` · ${step}` : ""}</span>
        <span className="wd-spacer" />
        <Mini bars={card.bars} />
        <span className="wd-count" data-testid="wd-card-count" title="Decisions of this kind in the last 24 hours">{card.count_24h} · 24h</span>
      </span>
      {windows.length ? <span className="wd-quiet-chip">quiet {windowWords(windows[0])}{windows.length > 1 ? ` +${windows.length - 1}` : ""}</span> : null}
      <span className="wd-why">{card.why}</span>
    </button>
  );
}

// ── the popover ─────────────────────────────────────────────────────────────────────────────

type Draft = { decider: Who; backup: Who; quiet: Window[]; cap_usd?: number; scope: "install" | "project" };

function draftOf(card: Card): Draft {
  const r = card.rule;
  return { decider: r?.decider ?? null, backup: r?.backup ?? null, quiet: [...(r?.quiet ?? [])], cap_usd: r?.cap_usd, scope: "install" };
}

function PersonPick({ value, people, seat, label, onChange, testid, allowNone }: {
  value: Who; people: Person[]; seat?: { id: string; label: string }; label: string; onChange: (w: Who) => void; testid: string; allowNone?: boolean;
}) {
  const now = value?.person ? `p:${value.person}` : value?.seat ? `s:${value.seat}` : "";
  return (
    <select className="wd-pick" aria-label={label} value={now} data-testid={testid}
            onChange={(e) => {
              const v = e.target.value;
              if (!v) return onChange(null);
              if (v.startsWith("s:")) return onChange({ seat: v.slice(2) });
              const p = people.find((x) => x.id === v.slice(2));
              onChange(p ? { person: p.id, name: p.name } : null);
            }}>
      {allowNone ? <option value="">+ backup person</option> : null}
      {seat ? <option value={`s:${seat.id}`}>{seat.label}</option> : null}
      {people.map((p) => <option key={p.id} value={`p:${p.id}`}>{p.name}</option>)}
    </select>
  );
}

function WindowForm({ onAdd, onCancel }: { onAdd: (w: Window) => void; onCancel: () => void }) {
  const [w, setW] = useState<Window>({ from_day: "fri", from_time: "15:00", to_day: "mon", to_time: "08:00", tz: browserTz() });
  const day = (k: "from_day" | "to_day") => (
    <select aria-label={k === "from_day" ? "From day" : "To day"} value={w[k]} onChange={(e) => setW({ ...w, [k]: e.target.value })}>
      {DAYS.map((d) => <option key={d} value={d}>{d[0].toUpperCase() + d.slice(1)}</option>)}
    </select>
  );
  const time = (k: "from_time" | "to_time") => (
    <input type="time" aria-label={k === "from_time" ? "From time" : "To time"} value={w[k]} onChange={(e) => setW({ ...w, [k]: e.target.value })} />
  );
  return (
    <span className="wd-winform" data-testid="wd-window-form">
      {day("from_day")}{time("from_time")}<span aria-hidden>→</span>{day("to_day")}{time("to_time")}
      <span className="wd-faint">{w.tz}</span>
      <button type="button" className="wd-btn" data-kind="primary" onClick={() => onAdd(w)} data-testid="wd-window-add">Add</button>
      <button type="button" className="wd-btn" data-kind="ghost" onClick={onCancel}>Cancel</button>
    </span>
  );
}

function Popover({ card, board, projectId, onClose, onSaved }: {
  card: Card; board: Board; projectId: string; onClose: () => void; onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(card));
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState("");
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => { setDraft(draftOf(card)); setRefusal(""); setAdding(false); }, [card.key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { ref.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" }); }, [card.key]);
  const I = ICONS[card.icon] ?? HandPalm;
  const seat = card.rule?.decider?.seat ?? (card.decider.kind === "seat" ? card.decider.id : undefined);
  const seatOption = seat ? { id: seat, label: `${card.decider.kind === "seat" ? card.decider.label : "Seat"}${card.decider.kind === "seat" && card.decider.people.length ? ` (${card.decider.people.map((p) => p.name).join(", ")})` : ""}` } : undefined;
  const audit = logHref({ kind: "decide.rule_changed", range: "30d" });

  const save = async () => {
    setBusy(true);
    setRefusal("");
    const rule: Partial<Rule> = { decider: draft.decider, backup: draft.backup, quiet: draft.quiet,
                                  ...(card.key === "spend" && draft.cap_usd !== undefined ? { cap_usd: draft.cap_usd } : {}) };
    const r = await saveRule(projectId, card.key, rule, draft.scope === "project" ? projectId : "install");
    setBusy(false);
    if (!r.ok) { setRefusal(r.error ?? "Not saved"); showToast("error", `Not saved: ${r.error}`); return; }
    showToast("success", `${card.title}: saved`);
    onSaved();
    onClose();
  };

  return (
    <div className="wd-pop" ref={ref} role="dialog" aria-label={card.title} data-testid="wd-pop" data-kind={card.key}>
      <div className="wd-pop-h">
        <span className="wd-bi"><I size={16} weight="bold" /></span><b>{card.title}</b>
        <span className="wd-spacer" />
        <button type="button" className="wd-icon-btn" onClick={onClose} aria-label="Close"><X size={14} /></button>
      </div>
      {card.fixed ? (
        <div className="wd-kv">
          <span>Who decides</span><span className="wd-row"><Face who={card.decider.people[0] ?? null} />Owner · passkey · always</span>
          <span>Why fixed</span><span>{card.why}</span>
          <span>Last change</span><span>{card.last_change ? `${card.last_change.who ?? "someone"} · ${ago(card.last_change.at)}` : "never"}</span>
        </div>
      ) : (
        <div className="wd-kv">
          <span>Who decides</span>
          <span className="wd-row">
            <PersonPick value={draft.decider} people={board.people} seat={seatOption} label="Who decides"
                        onChange={(w) => setDraft({ ...draft, decider: w })} testid="wd-pick-decider" />
            <PersonPick value={draft.backup} people={board.people.filter((p) => p.id !== draft.decider?.person)} label="Backup person"
                        onChange={(w) => setDraft({ ...draft, backup: w })} testid="wd-pick-backup" allowNone />
            {draft.backup ? <span className="wd-faint">answers after {card.rule?.backup_after_minutes ?? 120} min</span> : null}
            <a className="wd-link" href="/identity/seats" title="Identity › Approval seats: the full table">Seats ›</a>
          </span>
          <span>Scope</span>
          <span className="wd-row">
            <span className="wd-seg" role="radiogroup" aria-label="Scope">
              <button type="button" role="radio" aria-checked={draft.scope === "install"} onClick={() => setDraft({ ...draft, scope: "install" })}>Whole install</button>
              <button type="button" role="radio" aria-checked={draft.scope === "project"} disabled={!card.scope.per_project}
                      title={card.scope.per_project ? "This project adds to the install's rule" : "Set for the whole install"}
                      onClick={() => setDraft({ ...draft, scope: "project" })} data-testid="wd-scope-project">This project</button>
            </span>
            {card.scope.projects.length ? <span className="wd-faint">{card.scope.projects.join(", ")} add{card.scope.projects.length === 1 ? "s" : ""} to it</span> : null}
          </span>
          {card.key === "spend" ? (
            <>
              <span>Cap</span>
              <span className="wd-row">
                <span className="wd-money">$<input type="number" min={1} max={board.backstop.spend_usd} step={1} aria-label="Cap in dollars" data-testid="wd-cap"
                                                   value={draft.cap_usd ?? board.backstop.spend_usd}
                                                   onChange={(e) => setDraft({ ...draft, cap_usd: Number(e.target.value) })} /></span>
                <span className="wd-faint">a run or a day · at most ${board.backstop.spend_usd}</span>
                <a className="wd-link" href={areaLink("budget", { tab: "limits" })}>Limits ›</a>
              </span>
            </>
          ) : null}
          <span>Quiet window</span>
          <span className="wd-row">
            {draft.quiet.map((w, i) => (
              <span key={i} className="wd-chip" data-testid="wd-window">{windowWords(w)}<span className="wd-faint">{w.tz}</span>
                <button type="button" className="wd-icon-btn" aria-label={`Remove ${windowWords(w)}`}
                        onClick={() => setDraft({ ...draft, quiet: draft.quiet.filter((_, j) => j !== i) })}><X size={11} /></button>
              </span>
            ))}
            {adding ? <WindowForm onAdd={(w) => { setDraft({ ...draft, quiet: [...draft.quiet, w] }); setAdding(false); }} onCancel={() => setAdding(false)} />
              : <button type="button" className="wd-link" onClick={() => setAdding(true)} data-testid="wd-window-new"><Plus size={11} /> window</button>}
          </span>
          <span>Agents may</span><span>{card.agents_may}</span>
          <span>On my behalf</span>
          <span className="wd-row">
            {card.delegate.allowed === "never" ? <span className="wd-faint">Never: a delegate cannot approve this</span>
              : <span>{card.delegate.mine ? `${card.delegate.mine} delegate${card.delegate.mine > 1 ? "s" : ""} on` : "Off"} · release-branch merges only</span>}
            <a className="wd-link" href="/account/agents" title="Account › Approve on my behalf: a delegate key with a daily cap, at most 90 days">let a delegate approve…</a>
          </span>
          <span>Full auto</span>
          <span className="wd-faint wd-row"><LockSimple size={12} /> Never: irreversible actions are never auto-approved, at any cost</span>
        </div>
      )}
      <div className="wd-trail" data-testid="wd-trail">
        {(card.trail ?? []).length ? card.trail.map((t) => (
          <div key={`${t.type}-${t.id}`}>
            <span className="wd-t">{when(t.at)}</span>
            {t.who_name ? <Face who={{ name: t.who_name }} size={16} /> : <AgentAvatar variant="boss" size={16} tip="BOSS" />}
            <span>{t.type === "change" ? `${t.who_name ?? "Someone"} changed it: ${t.what}${t.via === "words" ? " · in words" : ""}`
                  : `${t.status === "pending" || t.status === "delegated" ? "Waiting" : `${t.who_name ?? "A person"} ${t.status ?? ""}`} · ${t.what}`}</span>
            {t.type === "decision" ? <a className="wd-chip" href={decisionHref(t.id, orgParam())}>diagram</a>
              : <a className="wd-chip" href={audit}>audit</a>}
          </div>
        )) : <div><span className="wd-t" /><span /><span className="wd-faint">Nothing of this kind in 30 days.</span><span /></div>}
      </div>
      {refusal ? <div className="wd-refusal" role="alert" data-testid="wd-refusal"><WarningCircle size={14} /> {refusal}</div> : null}
      <div className="wd-pop-f">
        <a className="wd-link" href={audit} data-testid="wd-audit">Full audit trail <ArrowSquareOut size={11} /></a>
        <span className="wd-spacer" />
        <button type="button" className="wd-btn" data-kind="ghost" onClick={onClose}>Cancel</button>
        {card.fixed ? null : (
          <button type="button" className="wd-btn" data-kind="primary" disabled={busy} onClick={() => void save()} data-testid="wd-save"
                  title="Changing this list is itself on the list: it asks for your passkey">
            <ShieldCheck size={14} />{busy ? "Saving…" : "Save · passkey"}
          </button>
        )}
      </div>
    </div>
  );
}

// ── change a rule in plain words ────────────────────────────────────────────────────────────

function editValueWords(e: Edit): string {
  const v = (e.value ?? {}) as Record<string, unknown>;
  if (e.field === "quiet") return `quiet ${windowWords(v as unknown as Window)}`;
  if (e.field === "cap_usd") return `cap $${String(e.value)}`;
  if (e.field === "decider" || e.field === "backup") return `${e.field === "backup" ? "backup " : ""}${whoWords(v as Who)}`;
  if (e.field === "approvers") return `${String(e.value)} approvers`;
  return e.field;
}

function RuleChange({ projectId, onApplied }: { projectId: string; onApplied: () => void }) {
  const [words, setWords] = useState("");
  const [reading, setReading] = useState(false);
  const [got, setGot] = useState<Understood | null>(null);
  const [failed, setFailed] = useState("");
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState<Edit[]>([]);
  const [replay, setReplay] = useState<ReplayRow[]>([]);
  const [applying, setApplying] = useState(false);

  const read = async () => {
    if (!words.trim() || reading) return;
    setReading(true); setFailed(""); setGot(null); setEditing(false);
    const r = await understandWords(projectId, words.trim(), browserTz());
    setReading(false);
    if ("error" in r) { setFailed(r.error); return; }
    setGot(r); setEdits(r.edits); setReplay(r.replay);
  };
  const drop = async (i: number) => {
    const next = edits.filter((_, j) => j !== i);
    setEdits(next);
    const again = next.length ? await replayEdits(projectId, next) : [];
    if (again) setReplay(again);
  };
  const setCap = async (i: number, value: number) => {
    const next = edits.map((e, j) => (j === i ? { ...e, value } : e));
    setEdits(next);
    const again = await replayEdits(projectId, next);
    if (again) setReplay(again);
  };
  const apply = async () => {
    setApplying(true);
    const r = await applyEdits(projectId, edits, words);
    setApplying(false);
    if (!r.ok) { showToast("error", `Not applied: ${r.error}`); return; }
    showToast("success", `${edits.length} change${edits.length === 1 ? "" : "s"} applied`);
    setGot(null); setWords(""); setEdits([]); setReplay([]);
    onApplied();
  };
  const allowed = got?.already_allowed ?? [];
  return (
    <>
      <div className="wd-cap">Change a rule in plain words</div>
      <div className="wd-in" data-testid="wd-words">
        <span className="wd-in-kind"><Scales size={18} weight="fill" /></span>
        <textarea rows={3} aria-label="Change a rule in plain words" value={words} maxLength={4000} data-testid="wd-words-input"
                  placeholder={"Nothing goes to prod on Fridays after 3pm. Anything over $200 a day needs Sarah, not me."}
                  onChange={(e) => setWords(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void read(); } }} />
        <div className="wd-in-foot">
          <span className="wd-faint">⇧↵ new line · ⌘↵ read it</span>
          <span className="wd-spacer" />
          <button type="button" className="wd-btn" data-kind="primary" disabled={!words.trim() || reading} onClick={() => void read()} data-testid="wd-words-read">
            <AgentAvatar agent="engineering_captain" size={18} aiBadge={false} tip="Helm" />{reading ? "Helm is reading…" : "Read it"}
          </button>
        </div>
      </div>
      {reading ? <div className="wd-ub" data-state="reading" aria-busy="true" data-testid="wd-rule-change-reading"><span className="wd-skel" /><span className="wd-skel" data-w="60" /></div> : null}
      {failed ? <div className="wd-refusal" role="alert">{failed}</div> : null}
      {got && got.status !== "understood" ? (
        <div className="wd-ub" data-state="fallback" data-testid="wd-rule-change-fallback"><WarningCircle size={16} /> {got.reply || "Helm could not read it just now. Your words are kept; try again."}</div>
      ) : null}
      {got && got.status === "understood" ? (
        <div className="wd-ub" data-testid="wd-rule-change">
          <div className="wd-ub-h">
            <AgentAvatar agent="engineering_captain" size={30} tip="Helm" />
            <div><b>Rule change</b>
              <small>{edits.length} change{edits.length === 1 ? "" : "s"}{allowed.length ? ` · ${allowed.length} already allowed` : ""} · effect on the last 30 days</small></div>
          </div>
          {got.reply ? <p className="wd-ub-reply">{got.reply}</p> : null}
          <div className="wd-ub-grid">
            {edits.map((e, i) => (
              <div className="wd-ub-f" key={`${e.kind}-${i}`} data-testid="wd-edit">
                <span className="wd-lbl">{e.title}{e.scope !== "install" ? ` · ${e.scope}` : ""}</span>
                <span>{e.says || editValueWords(e)} <mark>{editValueWords(e)}</mark>
                  {editing && e.field === "cap_usd" ? (
                    <span className="wd-money"> $<input type="number" min={1} max={500} step={1} aria-label="Cap" value={Number(e.value) || 0}
                                                       onChange={(ev) => void setCap(i, Number(ev.target.value))} /></span>) : null}
                </span>
                {editing ? <button type="button" className="wd-icon-btn" aria-label={`Drop ${e.title}`} onClick={() => void drop(i)} data-testid="wd-edit-drop"><X size={12} /></button>
                  : <PencilSimple size={12} className="wd-faint" />}
              </div>
            ))}
            {allowed.map((x, i) => (
              <div className="wd-ub-f" data-q key={`ok-${i}`} data-testid="wd-already">
                <span className="wd-lbl">{x.kind ? `${x.kind.replace(/_/g, " ")} · check` : "check"}</span>
                <span>{x.says} <mark>Nothing to change.</mark></span><span />
              </div>
            ))}
            {(got.cannot ?? []).map((x, i) => (
              <div className="wd-ub-f" data-no key={`no-${i}`} data-testid="wd-cannot">
                <span className="wd-lbl">not allowed</span><span>{x.says}</span><span />
              </div>
            ))}
            <div className="wd-ub-f" data-testid="wd-replay">
              <span className="wd-lbl">Last 30 days</span>
              <span className="wd-tags">{replay.length ? replay.map((r, i) => <span key={i} className="wd-tag" data-tone={r.n ? "amber" : undefined}>{r.words}</span>)
                : <span className="wd-faint">No changes to replay.</span>}</span><span />
            </div>
          </div>
          <div className="wd-ub-act">
            <button type="button" className="wd-btn" onClick={() => setEditing(!editing)} data-testid="wd-edit-toggle"><PencilSimple size={14} />{editing ? "Done" : "Edit"}</button>
            <span className="wd-faint wd-row"><ShieldCheck size={13} /> Changing this list is itself on the list: it asks for your passkey.</span>
            <span className="wd-spacer" />
            <button type="button" className="wd-btn" data-kind="primary" disabled={!edits.length || applying} onClick={() => void apply()} data-testid="wd-apply">
              <CheckCircle size={14} />{applying ? "Applying…" : `Apply ${edits.length} change${edits.length === 1 ? "" : "s"}`}
            </button>
          </div>
        </div>
      ) : null}
    </>
  );
}

// ── the page ────────────────────────────────────────────────────────────────────────────────

export function WherePeopleDecide({ projectId }: { projectId: string }) {
  const live = useConsoleLive();
  const board = usePolledRead({ key: boardPath(projectId), read: polled(boardPath(projectId), parseBoard), pollMs: 30_000, paused: !live, enabled: !!projectId });
  const settings = usePolledRead({ key: settingsPath(projectId), read: polled(settingsPath(projectId), parseSettings), pollMs: 15_000, paused: !live, enabled: !!projectId });
  const [open, setOpen] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [links, setLinks] = useState({ agents: "", oldest: "" });
  const data = board.data;
  const sum = data?.activity ?? null;
  useEffect(() => {
    setLinks({ agents: areaLink(AREA), oldest: sum?.oldest ? decisionHref(sum.oldest.id, orgParam()) : "" });
  }, [sum?.oldest?.id]);
  const card = useMemo(() => data?.cards.find((c) => c.key === open) ?? null, [data, open]);
  const st = settings.data;
  const stopped = !!st?.stop.stopped;
  const off = st?.mode.mode === "off";
  const reload = () => { board.reload(); settings.reload(); };
  const kinds = data ? data.cards.filter((c) => !c.fixed).length : 7;

  return (
    <section className="wd" data-testid="wd" aria-label="Where people decide">
      <header className="wd-head">
        <span className="wd-ico"><HandPalm size={22} weight="fill" /></span>
        <div><h1>Where people decide</h1><p>Agents act on their own. Only these {kinds} wait for a person.</p></div>
        <span className="wd-spacer" />
        <span className="wd-tag" data-tone={stopped ? "red" : off ? undefined : "green"} data-testid="wd-agents-state"
              title={stopped ? "Agent work is stopped" : off ? "Always-on agents are off: they start nothing by themselves" : "Captain authority is on: area agents act on their own below this line"}>
          <span className="wd-dot" />{stopped ? "Agents stopped" : off ? "Always-on off" : "Agents on"}
        </span>
        {!stopped ? (
          <button type="button" className="wd-btn" onClick={() => setStopping(true)} data-testid="wd-stop-all"
                  title="Stop all agent work now (the kill switch)"><StopCircle size={15} weight="bold" className="wd-red" />Stop all</button>
        ) : null}
      </header>
      {st && (stopping || stopped) ? (
        <div className="wd-kill"><KillSwitch projectId={projectId} stop={st.stop} mode={st.mode} startConfirming={stopping}
                                             onCancel={() => setStopping(false)} onChanged={() => { setStopping(false); reload(); }} /></div>
      ) : null}

      <div className="wd-sum" data-testid="wd-summary" title="Counts for the last 24 hours, every project">
        {sum ? (
          <>
            <span><b className="wd-teal" data-testid="wd-sum-agents">{sum.handled_by_agents}</b> handled by agents</span><span className="wd-sep">·</span>
            <span><b className="wd-amber" data-testid="wd-sum-asked">{sum.asked_a_person}</b> asked a person</span><span className="wd-sep">·</span>
            <span><b data-testid="wd-sum-waiting">{sum.waiting}</b> waiting on you{sum.oldest ? ": " : ""}
              {sum.oldest ? <a className="wd-chip" href={links.oldest || undefined} data-testid="wd-sum-oldest" title={sum.oldest.question}>{sum.oldest.question.length > 38 ? `${sum.oldest.question.slice(0, 37)}…` : sum.oldest.question} ›</a> : null}</span>
          </>
        ) : <span className="wd-skel" data-w="50" />}
        <span className="wd-spacer" />
        <a className="wd-chip" href={links.agents || undefined} data-testid="wd-what-agents-did" title="Each agent: what it did alone and what waited, as lanes and diagrams">
          <FlowArrow size={13} />What agents did ›</a>
      </div>

      <div className="wd-cap">The list <span className="wd-faint wd-cap-sub">· click a card to change who decides, scope or a quiet window</span></div>
      {board.error && !data ? <div className="wd-refusal" role="alert">The list could not be read: {board.error} <button type="button" className="wd-link" onClick={board.reload}>Retry</button></div> : null}
      <div className="wd-bounds" data-testid="wd-bounds">
        {data ? data.cards.map((c) => <ListCard key={c.key} card={c} open={open === c.key} onOpen={() => setOpen(open === c.key ? null : c.key)} />)
          : Array.from({ length: 8 }, (_, i) => <span key={i} className="wd-b wd-b-skel" aria-hidden />)}
      </div>
      {card && data ? <Popover card={card} board={data} projectId={projectId} onClose={() => setOpen(null)} onSaved={reload} /> : null}

      <RuleChange projectId={projectId} onApplied={reload} />
    </section>
  );
}
