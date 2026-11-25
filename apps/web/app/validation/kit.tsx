"use client";

/** The small pieces every validation surface shares: severity chip, fix-kind badge, status dot +
 *  word, and the ref chip that scrolls to what it names (SPEC §1.5). */
import React from "react";

import { KIND_WORDS, refTarget, STATUS_WORDS, type FindingStatus, type FixKind, type Sev } from "../../lib/validation-review";
import { actorWords } from "../../lib/actor-words";
import { useWho } from "../who";
import "./validation-review.css";

/** Who did it, by name: the name the record carries, else the directory's (useWho), said through
 *  actorWords — "Paramaguru", never "a person" when BOSS knows who it was, never an id. */
export function ActorName({ id, name }: { id: string; name?: string }) {
  const given = name && name !== id ? name : "";
  const w = useWho(given ? undefined : id);
  return <span data-testid="vr-actor" title={id || undefined}>{actorWords(id, given || w.name)}</span>;
}

export function SevChip({ sev, title }: { sev: Sev; title?: string }) {
  return <span className="vr-sev" data-sev={sev} title={title}>{sev}</span>;
}

export function KindBadge({ kind, label }: { kind: FixKind; label?: string }) {
  if (kind === "none" && !label) return null;
  return <span className="vr-kind" data-kind={kind}>{label ?? KIND_WORDS[kind]}</span>;
}

export function Status({ st, label }: { st: FindingStatus | "deferred" | "pending"; label?: string }) {
  return <span className="vr-st" data-st={st}>{label ?? (st === "deferred" || st === "pending" ? st : STATUS_WORDS[st])}</span>;
}

/** Scroll to the row a ref names and flash it; nothing happens for an id not on the page. */
export function jumpToRef(id: string): void {
  if (typeof document === "undefined") return;
  const el = document.getElementById(refTarget(id));
  if (!el) return;
  el.scrollIntoView?.({ block: "center", behavior: "smooth" });
  el.setAttribute("data-flash", "");
  window.setTimeout(() => el.removeAttribute("data-flash"), 1700);
}

export function RefChip({ id, label }: { id: string; label?: string }) {
  return (
    <button type="button" className="vr-ref" onClick={() => jumpToRef(id)} title={`Go to ${id}`} data-testid="vr-ref" data-ref={id}>
      {label ?? id}
    </button>
  );
}
