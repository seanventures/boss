"use client";

/**
 * CAN BOSS BUILD WHAT THIS ASKS FOR? Asked while the person types (GET /v1/stacks, debounced),
 * shown before Start: "This brief asks for Go, which BOSS cannot build. BOSS builds Python
 * (FastAPI, …) …". The API refuses the same start with `stack_unsupported` unless the person ticks
 * "Start anyway", which sends `allow_unsupported_stack`, the brief is then kept as a plan, and
 * the build refuses it again before anything is spent.
 */
import React, { useEffect, useState } from "react";

import {
  checkStack, levelWord, stackLabel, stackSupport, type StackAdapter, type StackCheck,
} from "../../../lib/templates";

export function useStackCheck(projectId: string, text: string, stack?: string) {
  const [check, setCheck] = useState<StackCheck | null>(null);
  const [allowed, setAllowed] = useState(false);
  useEffect(() => {
    let alive = true;
    const t = window.setTimeout(() => {
      void checkStack(projectId, text, stack).then((c) => { if (alive) setCheck(c); });
    }, 500);
    return () => { alive = false; window.clearTimeout(t); };
  }, [projectId, text, stack]);
  useEffect(() => { setAllowed(false); }, [check?.message]);
  const refused = !!check && !check.ok;
  return { check, refused, allowed, setAllowed, blocks: refused && !allowed };
}

export function StackWarning({ state, testId = "stack-warning" }: {
  state: ReturnType<typeof useStackCheck>; testId?: string;
}) {
  if (!state.refused || !state.check) return null;
  return (
    <div className="tpl-warn" role="status" data-testid={testId}>
      <p>{state.check.message}</p>
      <label title="It starts, and the panel and documents run; the build refuses it before anything is spent.">
        <input type="checkbox" checked={state.allowed} data-testid={`${testId}-anyway`}
               onChange={(e) => state.setAllowed(e.target.checked)} />{" "}
        Start anyway
      </label>
    </div>
  );
}

/** The language that builds the adapter (its backend's): what is said, and what is sent as `stack`. */
export function adapterLanguage(a: StackAdapter): string {
  return a.primary;
}

/** "Node, React + Vite, Express, PostgreSQL": the language first, unless the label says it. */
export function adapterWord(a: StackAdapter): string {
  const lang = stackLabel(adapterLanguage(a));
  return !lang || a.label.toLowerCase().startsWith(lang.toLowerCase()) ? a.label : `${lang}, ${a.label}`;
}

/** WHAT BOSS BUILDS, with how sure it is of each (services/stacks.py ADAPTERS), said in a tooltip. */
export function stackDetails(adapters: StackAdapter[]): string {
  return adapters.map((a) => `${a.label}: ${levelWord(a.level)}${a.ready ? "" : ", off on this install"}`).join(". ")
    + (adapters.length ? "." : "");
}

export function useStackAdapters(projectId: string): StackAdapter[] {
  const [adapters, setAdapters] = useState<StackAdapter[]>([]);
  useEffect(() => {
    let alive = true;
    void stackSupport(projectId).then((s) => { if (alive) setAdapters(s.adapters); });
    return () => { alive = false; };
  }, [projectId]);
  return adapters;
}

/**
 * A NEW APP'S STACK, one selector: "BOSS picks from the brief" or one adapter BOSS builds. The
 * details (each stack's support level, what is off here) are its tooltip, not a line of text.
 * An adapter off on this install is listed, disabled, with the reason.
 */
export function StackSelect({ adapters, value, onChange, id, testId = "np-stack" }: {
  adapters: StackAdapter[]; value: string; onChange: (id: string) => void; id: string; testId?: string;
}) {
  const shown = adapters.filter((a) => a.level !== "unsupported");
  return (
    <select id={id} className="input np-line np-stack" value={value} data-testid={testId}
            title={stackDetails(shown) || "The stack BOSS builds the app in"}
            onChange={(e) => onChange(e.target.value)}>
      <option value="">BOSS picks from the brief</option>
      {shown.map((a) => (
        <option key={a.id} value={a.id} disabled={!a.ready}>
          {adapterWord(a)}{a.level === "best_effort" ? ", best effort" : ""}{a.ready ? "" : ", off here"}
        </option>
      ))}
    </select>
  );
}
