"use client";

/**
 * The query bar: the ONE place a Log filter lives (spec §4.2.2). Facet clicks, view chips and
 * the drawer's `+`/`−` all write tokens here; the URL mirrors it. `key:value` adds a token,
 * `-key:value` excludes (level, stream and agent only), a pasted `crun_…`/`wfr_…`/`act_…`
 * becomes its scope, and plain text is a search. Backspace on an empty bar removes the last
 * token; Esc clears what you typed; `/` focuses the bar from anywhere on the page.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { MagnifyingGlass, X } from "@phosphor-icons/react";

import type { ActivityFacets } from "../../lib/activity-log";
import { SCOPE_KEYS, parseToken, removeToken, tokenForId, type LogToken, type LogTokenKey } from "../../lib/log-url";
import { nameOf, RunFace } from "../agents/run-face";
import { LEVELS, SERVICE_LABEL, STATUSES, STREAMS, tokenText } from "./log-model";
import { useIdentityVersion } from "../../lib/use-agent-identities";

type Suggestion = { token: LogToken; label: string; count?: number };

function suggestions(key: LogTokenKey, negate: boolean, typed: string, facets: ActivityFacets | null,
                     found: { prefix: string; kinds: { kind: string; n: number }[] } | null = null): Suggestion[] {
  const all: Suggestion[] = (() => {
    switch (key) {
      case "level": return LEVELS.map((l) => ({ token: { key, value: l.key }, label: l.label, count: facets?.levels[l.key] }));
      case "src": return STREAMS.map((s) => ({ token: { key, value: s.key }, label: s.label, count: facets?.sources[s.key] }));
      case "agent": return (facets?.agents ?? []).map((a) => ({ token: { key, value: a.roleKey }, label: nameOf(a.roleKey).name, count: a.n }));
      case "status": return STATUSES.map((s) => ({ token: { key, value: s.key }, label: s.label, count: facets?.statuses[s.key] }));
      case "kind": {
        // The server's prefix answer first (any kind in the window), then the facets' top kinds.
        const fromServer = found && typed.toLowerCase().startsWith(found.prefix) ? found.kinds : [];
        const seen = new Set<string>();
        return [...fromServer, ...(facets?.kinds ?? [])].filter((k) => !seen.has(k.kind) && seen.add(k.kind))
          .map((k) => ({ token: { key, value: k.kind }, label: k.kind, count: k.n }));
      }
      case "svc": return Object.entries(facets?.services ?? {}).map(([k, n]) => ({ token: { key, value: k }, label: SERVICE_LABEL[k] ?? k, count: n }));
      case "run": return (facets?.runs ?? []).map((r) => ({
        token: { key: r.id.startsWith("crun_") ? "run" : "wfr", value: r.id } as LogToken, label: r.label || r.id, count: r.n }));
      default: return [];
    }
  })();
  const needle = typed.toLowerCase();
  return all
    .filter((s) => !needle || s.label.toLowerCase().includes(needle) || s.token.value.toLowerCase().includes(needle))
    .map((s) => (negate ? { ...s, token: { ...s.token, negate: true } } : s))
    .slice(0, 12);
}

export function QueryBar({ tokens, q, labels, names, facets, error, onTokens, onQuery, onAdd, findKinds }: {
  tokens: LogToken[];
  q: string;
  labels: Record<string, string>;
  names: { step?: string; run?: string };
  facets: ActivityFacets | null;
  /** The server's sentence for a query it refused (422), shown under the bar. */
  error: string;
  onTokens: (tokens: LogToken[], scopeChange: boolean) => void;
  onQuery: (q: string) => void;
  onAdd: (token: LogToken) => void;
  /** `kind:` completion by prefix from the server (beyond the facets' top kinds). */
  findKinds?: (prefix: string, signal: AbortSignal) => Promise<{ kind: string; n: number }[]>;
}) {
  const identities = useIdentityVersion(); // names land when the install's agents load
  const [text, setText] = useState(q);
  const [hint, setHint] = useState("");
  const [focused, setFocused] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const wfr = tokens.find((t) => t.key === "wfr")?.value ?? null;

  useEffect(() => { setText((cur) => (cur.includes(":") ? cur : q)); }, [q]);

  // `/` focuses the bar from anywhere that is not already a text box.
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey) return;
      const target = e.target as HTMLElement | null;
      if (target && (target.closest("input, textarea, select, [contenteditable=true]"))) return;
      e.preventDefault();
      input.current?.focus();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  // Plain text searches after a pause in typing; `key:` text is a token in progress, never a search.
  useEffect(() => {
    if (/^-?[a-z]+:/i.test(text)) return undefined;
    if (text === q) return undefined;
    const t = setTimeout(() => onQuery(text), 250);
    return () => clearTimeout(t);
  }, [text, q, onQuery]);

  const typing = text.match(/^(-?)([a-z]+):(.*)$/i);
  // kind:<prefix> asks the server after a short pause; a stale answer is dropped.
  const [found, setFound] = useState<{ prefix: string; kinds: { kind: string; n: number }[] } | null>(null);
  const kindPrefix = typing && typing[2].toLowerCase() === "kind" && focused ? typing[3].trim().toLowerCase() : null;
  useEffect(() => {
    if (kindPrefix === null || !kindPrefix || !findKinds) return undefined;
    const ctl = new AbortController();
    const t = setTimeout(() => {
      void findKinds(kindPrefix, ctl.signal).then((kinds) => { if (!ctl.signal.aborted) setFound({ prefix: kindPrefix, kinds }); })
        .catch(() => {});
    }, 200);
    return () => { clearTimeout(t); ctl.abort(); };
  }, [kindPrefix, findKinds]);
  const menu = useMemo(() => {
    const m = text.match(/^(-?)([a-z]+):(.*)$/i);
    return m && focused ? suggestions(m[2].toLowerCase() as LogTokenKey, m[1] === "-", m[3], facets, found) : [];
  }, [text, focused, facets, identities, found]);

  const commit = (raw: string): boolean => {
    const parsed = parseToken(raw);
    if (!parsed) return false;
    if ("error" in parsed) { setHint(parsed.error); return true; }
    setHint("");
    onAdd(parsed.token);
    setText("");
    if (q) onQuery("");
    return true;
  };

  return (
    <div className="lx-query-wrap">
      <div className="lx-query" data-focused={focused || undefined} data-testid="lx-query"
           title="Searches titles and agent text. Paste any id."
           onClick={() => input.current?.focus()}>
        <MagnifyingGlass size={14} aria-hidden className="lx-query-icon" />
        {tokens.map((t) => (
          <span key={`${t.negate ? "-" : ""}${t.key}:${t.value}`} className="lx-token" data-key={t.key}
                data-negate={t.negate || undefined} data-testid={`lx-token-${t.key}`}>
            <span data-user-text title={`${t.key}:${t.value}`}>{tokenText(t, labels, names, wfr)}</span>
            <button type="button" aria-label={`Remove ${t.key}`} data-testid={`lx-token-remove-${t.key}`}
                    onClick={(e) => { e.stopPropagation(); onTokens(removeToken(tokens, t), SCOPE_KEYS.has(t.key)); }}>
              <X size={10} weight="bold" aria-hidden />
            </button>
          </span>
        ))}
        {tokens.some((t) => t.key === "step") ? (
          <button type="button" className="lx-whole-run" data-testid="lx-whole-run" title="Every line of this run"
                  onClick={(e) => { e.stopPropagation(); onTokens(tokens.filter((t) => t.key !== "step"), true); }}>
            Whole run
          </button>
        ) : null}
        <input ref={input} className="lx-query-input" value={text} aria-label="Find lines"
               placeholder={tokens.length ? "" : "Find lines, ids…"} data-testid="lx-query-input"
               onFocus={() => setFocused(true)} onBlur={() => setTimeout(() => setFocused(false), 120)}
               onChange={(e) => { setText(e.target.value); setHint(""); }}
               onPaste={(e) => {
                 const pasted = e.clipboardData.getData("text").trim();
                 const token = tokenForId(pasted);
                 if (token) { e.preventDefault(); onAdd(token); setText(""); }
               }}
               onKeyDown={(e) => {
                 if (e.key === "Enter") {
                   if (menu.length && typing && !typing[3].includes(" ") && menu[0]) {
                     const exact = parseToken(text);
                     if (!exact || "error" in exact) { e.preventDefault(); onAdd(menu[0].token); setText(""); return; }
                   }
                   if (commit(text)) e.preventDefault();
                   else onQuery(text);
                 } else if (e.key === "Escape") {
                   setText(""); setHint(""); if (q) onQuery("");
                 } else if (e.key === "Backspace" && !text && tokens.length) {
                   const last = tokens[tokens.length - 1];
                   onTokens(removeToken(tokens, last), SCOPE_KEYS.has(last.key));
                 }
               }} />
      </div>
      {menu.length ? (
        <ul className="lx-suggest" role="listbox" data-testid="lx-suggest">
          {menu.map((s) => (
            <li key={`${s.token.key}:${s.token.value}`}>
              <button type="button" role="option" aria-selected="false"
                      onMouseDown={(e) => { e.preventDefault(); onAdd(s.token); setText(""); }}>
                {s.token.key === "agent" ? <RunFace roleKey={s.token.value} size={16} /> : null}
                <span data-user-text>{s.label}</span>
                {typeof s.count === "number" ? <span className="lx-count">{s.count.toLocaleString()}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {hint || error ? <div className="lx-query-error" role="alert" data-testid="lx-query-error">{hint || error}</div> : null}
    </div>
  );
}
