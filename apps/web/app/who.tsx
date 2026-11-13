"use client";

/**
 * <Who>: a principal id as a person reads it (BOSSAPP-1040). The wiki's history and byline, a
 * document's comments, the On-hold byline and the watch rows each printed the id they were given
 * ("usr_wsw5h4…"). Every one of those asks here instead: a party string ("person:ceo",
 * "agent:forge"), a principal id (usr_…) or an agent id (agt_…) becomes the name BOSS knows,
 * with the person's initial (the identity Face) and the raw id in the tooltip.
 *
 * People resolve through the directory the console already serves (GET /v1/identity/users),
 * fetched ONCE per page and shared by every <Who> on it; agents resolve through
 * lib/agent-identity, the same resolver every agent surface uses. A viewer who may not read the
 * directory, or an id it does not hold, sees the id itself — never a blank, never a guess.
 */
import React, { useEffect, useMemo, useState } from "react";

import { Face } from "../components/identity/kit";
import { actorWords, looksLikeAnId } from "../lib/actor-words";
import { agentIdentity, loadedIdentities } from "../lib/agent-identity";
import { idGet } from "../lib/identity/api";
import { avatarTone } from "../lib/identity/avatar";

// ── The one directory read, shared by every <Who> on the page ─────────────────────────────

type People = ReadonlyMap<string, string>;
let people: People | null = null;
let inflight: Promise<void> | null = null;
const waiters = new Set<() => void>();

function loadPeople(): void {
  if (people || inflight) return;
  inflight = idGet<{ users?: { id?: unknown; display_name?: unknown }[] }>("/v1/identity/users")
    .then((d) => {
      people = new Map((d.users ?? []).flatMap((u) =>
        (typeof u.id === "string" && u.id ? [[u.id, String(u.display_name ?? "").trim()] as const] : [])));
    })
    // May not read the directory (or it failed): ids stay ids, said honestly.
    .catch(() => { people = new Map(); })
    .then(() => { inflight = null; for (const w of [...waiters]) w(); });
}

/** Tests (and a project switch): a known directory without the fetch; null forgets it. */
export function primePeople(entries: Record<string, string> | null): void {
  people = entries ? new Map(Object.entries(entries)) : null;
  inflight = null;
}

// ── Resolution ─────────────────────────────────────────────────────────────────────────────

const PERSON_ID = /^(usr|prn|sa|svc)_/i;

export type WhoView = {
  /** The name to read, or null when nothing resolved (the id is then shown, never a blank). */
  name: string | null;
  kind: "person" | "agent" | null;
  /** The raw value, for the tooltip. */
  id: string;
};

/** Pure: a party string or id against the directory in hand (exported for tests). */
export function resolveWho(raw: string | null | undefined, dir: People | null): WhoView {
  const whole = String(raw ?? "").trim();
  const m = /^(person|agent|system):(.*)$/.exec(whole);
  const hint = m ? m[1] : null;
  const key = (m ? m[2] : whole).trim();
  if (!key) return { name: whole ? null : "someone", kind: null, id: whole };
  if (hint === "agent" || /^agt_/i.test(key)) {
    if (/^agt_/i.test(key)) {
      for (const i of loadedIdentities().values()) {
        if (i.agentId === key) return { name: i.name, kind: "agent", id: whole };
      }
      return { name: null, kind: "agent", id: whole };
    }
    return { name: agentIdentity(key).name, kind: "agent", id: whole };
  }
  if (PERSON_ID.test(key)) {
    const named = dir?.get(key)?.trim();
    return { name: named && !looksLikeAnId(named) ? named : null, kind: "person", id: whole };
  }
  // A word, not an id: "ceo" is the owner, a typed name passes through.
  const words = actorWords(key);
  const named = words === "a person" || words === "an agent" || looksLikeAnId(words) ? null : words;
  return { name: named, kind: hint === "person" ? "person" : null, id: whole };
}

/** The view for one raw value, loading the directory once when a person id needs it. */
export function useWho(raw: string | null | undefined): WhoView {
  const id = String(raw ?? "").trim();
  const bare = id.replace(/^person:/, "");
  const needsPeople = PERSON_ID.test(bare) && !people;
  const [, bump] = useState(0);
  useEffect(() => {
    if (!needsPeople) return;
    const wake = () => bump((n) => n + 1);
    waiters.add(wake);
    loadPeople();
    return () => { waiters.delete(wake); };
  }, [needsPeople]);
  // `people` is module state; the bump above re-renders when it lands.
  return useMemo(() => resolveWho(id, people), [id, needsPeople && !people]); // eslint-disable-line react-hooks/exhaustive-deps
}

// ── The component ──────────────────────────────────────────────────────────────────────────

/**
 * The name (bold when asked), a person's initial before it, the raw id in the tooltip. An
 * unresolved id is shown as itself — attribution is never blank and never invented.
 */
export function Who({ id, bold, face = true, testId }: {
  id: string | null | undefined; bold?: boolean;
  /** No room for the initial (a tight byline): face={false}. */
  face?: boolean; testId?: string;
}) {
  const w = useWho(id);
  const shown = w.name ?? (w.id || "someone");
  const text = bold ? <b>{shown}</b> : shown;
  return (
    <span className="who" title={w.id && w.id !== shown ? w.id : undefined} data-testid={testId} data-user-text>
      {face && w.name && w.kind === "person" ? <Face name={w.name} tone={avatarTone(w.id)} size={22} /> : null}
      {text}
    </span>
  );
}
