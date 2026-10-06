"use client";

/**
 * One tap, never blocking, never nagging (docs/product/RUN_SCORECARDS.md, "Feedback the person
 * gives"). Stored locally always — whether or not Improve BOSS is on — because it feeds this
 * install's own scorecards and experiments.
 *
 *   Would you ship this?  [Yes] [With changes] [No]          → then, optional: [Too slow] [Misread brief] …
 *   (after 3 open asks today)                    [Rate]       → a quiet link that opens the same row
 *
 * Every RateIt on a page shares ONE read: subjects are batched for a tick and asked together.
 */
import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import { ThumbsDown, ThumbsUp } from "@phosphor-icons/react";

import { CHIP_WORDS, QUESTION, RATING_WORDS, improveWrite, isPrompts, readImprove, type PromptsView } from "../../lib/improve";
import "./improve.css";

type Subject = "build" | "research" | "panel" | "answer" | "deploy";

// ── one shared read per tick ────────────────────────────────────────────────────────────────
type Pending = { key: string; resolve: (v: PromptsView | null) => void };
let queue: Pending[] = [];
let timer: ReturnType<typeof setTimeout> | null = null;
const shownThisPage = new Set<string>();
const POSITIVE_RATINGS = new Set(["yes", "useful", "right", "up", "working"]);
const POSITIVE_CHIPS = new Set(["great_sources", "changed_my_mind", "good_debate"]);

function askPrompts(projectId: string, actor: string, key: string): Promise<PromptsView | null> {
  return new Promise((resolve) => {
    queue.push({ key, resolve });
    if (timer) return;
    timer = setTimeout(async () => {
      const batch = queue;
      queue = [];
      timer = null;
      const subjects = Array.from(new Set(batch.map((b) => b.key))).slice(0, 20).join(",");
      const got = await readImprove<PromptsView>("prompts", { project_id: projectId, actor, subjects });
      const view = got.ok && !got.notModified && isPrompts(got.data) ? got.data : null;
      for (const b of batch) b.resolve(view);
    }, 0); // every RateIt mounted in one commit has asked by the next tick
  });
}

/** Where a rating is filed, for surfaces whose rows do not carry it (Talk to BOSS answers). */
export const RateScope = createContext<{ projectId: string; actor: string } | null>(null);

/** A RateIt that takes project and person from RateScope; renders nothing outside one. */
export function ScopedRate({ subject, id, testId }: { subject: Subject; id: string; testId?: string }) {
  const scope = useContext(RateScope);
  if (!scope?.projectId || !id) return null;
  return <RateIt projectId={scope.projectId} actor={scope.actor} subject={subject} id={id} testId={testId} />;
}

export function RateIt({ projectId, actor, subject, id, testId }: {
  projectId: string; actor: string; subject: Subject; id: string; testId?: string;
}) {
  const key = `${subject}:${id}`;
  const [view, setView] = useState<PromptsView | null>(null);
  const [rating, setRating] = useState<string | null>(null);
  const [chips, setChips] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const marked = useRef(false);

  useEffect(() => {
    if (!projectId || !id) return undefined;
    let live = true;
    void askPrompts(projectId, actor, key).then((v) => {
      if (!live || !v) return;
      setView(v);
      const mine = v.mine[key];
      if (mine) { setRating(mine.rating); setChips(mine.chips); }
      setOpen(v.ask_open || subject === "answer");
    });
    return () => { live = false; };
  }, [projectId, actor, key, id]);

  // An OPEN ask counts toward the person's three a day — once per subject per page.
  useEffect(() => {
    if (!view || !open || rating || marked.current || shownThisPage.has(key) || subject === "answer") return;
    marked.current = true;
    shownThisPage.add(key);
    void improveWrite("prompt_shown", { project_id: projectId, actor, prompt: "rate" });
  }, [view, open, rating, key, projectId, actor, subject]);

  if (!view) return null;
  const ratings = view.ratings[subject] ?? [];
  const chipList = view.chips[subject] ?? [];

  const send = async (r: string, c: string[]) => {
    setBusy(true);
    setError("");
    const got = await improveWrite("feedback", { project_id: projectId, actor, subject_kind: subject, subject_id: id,
                                                 rating: r, chips: c });
    setBusy(false);
    if (!got.ok) { setError(got.status === 403 ? "Can't rate this" : "Not saved"); return false; }
    return true;
  };
  const pick = async (r: string) => {
    const prev = rating;
    setRating(r);
    if (!(await send(r, chips))) setRating(prev);
  };
  const toggle = async (c: string) => {
    if (!rating) return;
    const next = chips.includes(c) ? chips.filter((x) => x !== c) : [...chips, c];
    setChips(next);
    if (!(await send(rating, next))) setChips(chips);
  };

  if (!open && !rating) {
    return (
      <span className="im-rate" data-testid={testId ?? `rate-${subject}`}>
        <button type="button" className="im-rate-btn" data-quiet="" onClick={() => setOpen(true)}
                title="Tell BOSS how this went. One tap.">Rate</button>
      </span>
    );
  }
  const thumbs = subject === "answer";
  // After a good verdict, only the good reasons; after any other, only the others.
  const shownChips = rating
    ? chipList.filter((c) => (POSITIVE_RATINGS.has(rating) ? POSITIVE_CHIPS.has(c) : !POSITIVE_CHIPS.has(c)))
    : [];
  return (
    <span className="im-rate" data-testid={testId ?? `rate-${subject}`} role="group" aria-label={QUESTION[subject]}>
      {thumbs ? null : <span className="im-rate-q">{QUESTION[subject]}</span>}
      {ratings.map((r) => (
        <button key={r} type="button" className="im-rate-btn" aria-pressed={rating === r} disabled={busy}
                data-testid={`rate-${subject}-${r}`} onClick={() => void pick(r)}
                title={thumbs ? RATING_WORDS[r] : undefined} aria-label={thumbs ? RATING_WORDS[r] : undefined}>
          {thumbs ? (r === "up" ? <ThumbsUp size={14} aria-hidden /> : <ThumbsDown size={14} aria-hidden />) : RATING_WORDS[r] ?? r}
        </button>
      ))}
      {shownChips.length ? (
        <span className="im-chips" role="group" aria-label="Why? Optional">
          {shownChips.map((c) => (
            <button key={c} type="button" className="im-chip" aria-pressed={chips.includes(c)} disabled={busy}
                    data-testid={`rate-chip-${c}`} onClick={() => void toggle(c)}>{CHIP_WORDS[c] ?? c}</button>
          ))}
        </span>
      ) : null}
      {error ? <span className="im-err" role="status">{error}</span> : null}
    </span>
  );
}

/**
 * The reasons on a rejection or a steer: the build chips, optional, sent with the action that
 * already exists (the reject press, the steer press) — never a separate question.
 */
export function ReasonChips({ value, onChange, testId, label = "Why? Optional" }: {
  value: string[]; onChange: (next: string[]) => void; testId?: string; label?: string;
}) {
  const all = ["wrong_scope", "poor_quality", "broke_something", "too_slow", "too_expensive", "misread_brief"];
  return (
    <span className="im-chips" role="group" aria-label={label} data-testid={testId ?? "reason-chips"}>
      {all.map((c) => (
        <button key={c} type="button" className="im-chip" aria-pressed={value.includes(c)}
                onClick={() => onChange(value.includes(c) ? value.filter((x) => x !== c) : [...value, c])}>
          {CHIP_WORDS[c]}
        </button>
      ))}
    </span>
  );
}

/** Send reasons for a reject or a steer. Best effort and quiet: the action itself already went. */
export async function sendReasons(projectId: string, actor: string, kind: "reject" | "steer", id: string,
                                  chips: string[], decisionKind?: string | null): Promise<void> {
  if (!chips.length) return;
  await improveWrite("feedback", { project_id: projectId, actor, subject_kind: kind, subject_id: id, chips,
                                   decision_kind: decisionKind ?? null });
}

/** Which kind of no this was, for the reasons: plan, spec (PRD), merge, deploy, or other. */
export function rejectKind(decisionKind?: string | null, step?: string | null, where?: string | null,
                           rec?: Record<string, unknown> | null): "plan" | "spec" | "merge" | "deploy" | "other" {
  const action = String(rec?.action ?? "");
  const tool = String(rec?.tool ?? "");
  if (decisionKind === "pull_request" || action === "review_pull_request") return "merge";
  if (decisionKind === "deploy_gate" || /deploy/.test(action)) return "deploy";
  if (tool === "publish_document" || /spec|prd/i.test(step ?? "")) return "spec";
  if (action === "review_plan_draft" || /plan/i.test(step ?? "") || /plan/i.test(where ?? "")) return "plan";
  return "other";
}
