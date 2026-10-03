"use client";

import React, {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type Dispatch,
} from "react";
import { CheckCircle, Copy, GitDiff, MagicWand, WarningCircle } from "@phosphor-icons/react";

import { FlowLane, deepEqual, lineOfStep, stepAtLine } from "../../../../lib/workflow-editor-types";
import type { EditorAction, EditorState, Palette, Problem } from "../../../../lib/workflow-editor-types";
import { foldDiff, lineDiff } from "./line-diff";

/** Whether broken JSON blocks Visual and Save, and the one tooltip both show. */
export function jsonGate(state: Pick<EditorState, "jsonError">): { blocked: boolean; tip: string } {
  return state.jsonError
    ? { blocked: true, tip: `Fix the JSON first (line ${state.jsonError.line})` }
    : { blocked: false, tip: "" };
}

const LINE_PX = 20;
const pretty = (value: unknown) => JSON.stringify(value, null, 2);

/** The 1-based line holding character `offset`. */
const lineAt = (text: string, offset: number) => text.slice(0, offset).split("\n").length;
/** The character offset where 1-based `line` starts. */
function offsetOf(text: string, line: number): number {
  let at = 0;
  for (let l = 1; l < line; l += 1) {
    const nl = text.indexOf("\n", at);
    if (nl < 0) return text.length;
    at = nl + 1;
  }
  return at;
}

/** The line a problem points at: the field inside its step when the server named one, else the
 *  step's `"id"` line. */
export function lineOfProblem(text: string, p: Problem): number | null {
  if (!p.step_id) return null;
  const start = lineOfStep(text, p.step_id);
  if (start === null || !p.field) return start;
  const lines = text.split("\n");
  const needle = `"${p.field}"`;
  const indent = (l: string) => l.length - l.trimStart().length;
  const own = indent(lines[start - 1] ?? "");
  // Forward through the step's own object: it ends at the first line indented less than its id.
  for (let i = start; i < lines.length; i += 1) {
    if (lines[i].trim() && indent(lines[i]) < own) break;
    if (lines[i].includes(needle)) return i + 1;
  }
  return start;
}

/**
 * The workflow as JSON — editable, for every workflow, standards included.
 *
 * Top: the whole flow, compact, as a live preview whose nodes select steps. Below: the text,
 * with a line-number gutter and a red dot on each problem's line. The text is the store's
 * (`SET_JSON_TEXT` parses it; broken JSON never touches the spec); when the spec changes from
 * anywhere else — the inspector, undo — the text is re-serialised and the caret stays on the
 * same step.
 */
export function JsonView({ state, palette, dispatch }: {
  state: EditorState;
  palette: Palette | null;
  dispatch: Dispatch<EditorAction>;
}) {
  const area = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const [showChanges, setShowChanges] = useState(false);
  const [copied, setCopied] = useState(false);

  const derived = useMemo(() => pretty(state.spec), [state.spec]);
  // The typed text while it is broken, or while it still says exactly what the spec says (so
  // the operator's own layout survives); otherwise the spec, freshly serialised.
  const text = useMemo(() => {
    if (state.jsonText === null) return derived;
    if (state.jsonError) return state.jsonText;
    try { return deepEqual(JSON.parse(state.jsonText), state.spec) ? state.jsonText : derived; }
    catch { return derived; }
  }, [state.jsonText, state.jsonError, state.spec, derived]);

  const lineCount = useMemo(() => text.split("\n").length, [text]);
  const problemLines = useMemo(() => {
    const out = new Map<number, string>();
    for (const p of state.problems) {
      const line = lineOfProblem(text, p);
      if (line !== null && !out.has(line)) out.set(line, p.message);
    }
    return out;
  }, [state.problems, text]);

  /* ── caret ↔ selection ─────────────────────────────────────────────────────────── */

  /** The selection the caret itself made, so the effect below does not move the caret back. */
  const caretPick = useRef<string | null>(null);
  /** Text this component wrote (typing, Tab, Format), as opposed to a re-serialisation. */
  const typed = useRef<string | null>(null);
  /** Where the caret was, as (step, lines below its id line), to restore after a re-serialise. */
  const anchor = useRef<{ step: string | null; delta: number; line: number }>({ step: null, delta: 0, line: 1 });
  const pendingRange = useRef<[number, number] | null>(null);

  const scrollToLine = useCallback((line: number) => {
    const el = area.current;
    if (!el) return;
    const top = (line - 1) * LINE_PX;
    if (top < el.scrollTop || top > el.scrollTop + el.clientHeight - LINE_PX * 2) {
      el.scrollTop = Math.max(0, top - el.clientHeight / 3);
    }
  }, []);

  const placeCaret = useCallback((line: number, focus: boolean) => {
    const el = area.current;
    if (!el) return;
    const at = offsetOf(el.value, Math.max(1, Math.min(line, el.value.split("\n").length)));
    if (focus) el.focus();
    el.setSelectionRange(at, at);
    scrollToLine(line);
  }, [scrollToLine]);

  const readCaret = useCallback(() => {
    const el = area.current;
    if (!el) return;
    const line = lineAt(el.value, el.selectionStart);
    const step = stepAtLine(el.value, line);
    const start = step ? lineOfStep(el.value, step) : null;
    anchor.current = { step, delta: start === null ? 0 : line - start, line };
    if (step && step !== state.selection) {
      caretPick.current = step;
      dispatch({ type: "SELECT", id: step });
    }
  }, [dispatch, state.selection]);

  // A re-serialised text (an inspector edit, undo): put the caret back on the same step.
  useLayoutEffect(() => {
    const el = area.current;
    if (!el) return;
    if (pendingRange.current) {
      el.setSelectionRange(...pendingRange.current);
      pendingRange.current = null;
      return;
    }
    if (typed.current === text) return;
    typed.current = null;
    const { step, delta, line } = anchor.current;
    const start = step ? lineOfStep(text, step) : null;
    const target = start !== null ? start + delta : line;
    if (document.activeElement === el) placeCaret(target, false);
  }, [text, placeCaret]);

  // A selection made elsewhere (a node in the lane above, the canvas): show that step's text.
  useEffect(() => {
    if (!state.selection || caretPick.current === state.selection) return;
    caretPick.current = null;
    const line = lineOfStep(text, state.selection);
    if (line !== null) {
      anchor.current = { step: state.selection, delta: 0, line };
      placeCaret(line, false);
    }
    // Only on a selection change, not on every keystroke.
  }, [state.selection]);

  const write = (next: string, range?: [number, number]) => {
    typed.current = next;
    if (range) pendingRange.current = range;
    dispatch({ type: "SET_JSON_TEXT", text: next });
  };

  /* ── editing keys ──────────────────────────────────────────────────────────────── */

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Tab" || e.altKey || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    const el = e.currentTarget;
    const { selectionStart: s, selectionEnd: end, value } = el;
    const firstLine = value.lastIndexOf("\n", s - 1) + 1;
    const multi = value.slice(s, end).includes("\n");
    if (!e.shiftKey && !multi) {
      write(value.slice(0, s) + "  " + value.slice(end), [s + 2, s + 2]);
      return;
    }
    // Indent or outdent every line the selection touches.
    const block = value.slice(firstLine, end);
    const lines = block.split("\n");
    let removedFirst = 0;
    let delta = 0;
    const changed = lines.map((l, i) => {
      if (!e.shiftKey) { delta += 2; return `  ${l}`; }
      const cut = l.startsWith("  ") ? 2 : l.startsWith(" ") ? 1 : 0;
      if (i === 0) removedFirst = cut;
      delta -= cut;
      return l.slice(cut);
    });
    const next = value.slice(0, firstLine) + changed.join("\n") + value.slice(end);
    const newStart = e.shiftKey ? Math.max(firstLine, s - removedFirst) : s + 2;
    write(next, multi ? [newStart, end + delta] : [newStart, newStart]);
  };

  /* ── toolbar ───────────────────────────────────────────────────────────────────── */

  const format = () => {
    if (state.jsonError) return;
    try { write(pretty(JSON.parse(text))); } catch { /* the store will report it */ }
  };
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard refused: nothing to say beyond the button not changing */ }
  };
  const diff = useMemo(() => (showChanges && state.loaded
    ? foldDiff(lineDiff(pretty(state.loaded), state.jsonError ? text : derived)) : null),
  [showChanges, state.loaded, state.jsonError, text, derived]);
  const changed = diff ? diff.filter((d) => "kind" in d && d.kind !== "same").length : 0;

  const error = state.jsonError;

  return (
    <div className="wf-json" data-testid="wf-json">
      <div className="wf-json-lane" data-dim={error ? true : undefined} aria-label="Flow preview">
        {error ? <span className="wf-json-badge" data-testid="wf-json-badge">JSON error</span> : null}
        <FlowLane spec={state.spec} palette={palette} mode="preview" density="compact"
                  selection={state.selection} problems={state.problems}
                  onSelect={(id) => dispatch({ type: "SELECT", id })} />
      </div>

      <div className="wf-json-bar" role="toolbar" aria-label="JSON">
        <button type="button" className="wf-btn wf-btn-sm" onClick={format} disabled={Boolean(error)}
                title={error ? jsonGate(state).tip : "Indent two spaces"} data-testid="wf-json-format">
          <MagicWand size={13} weight="bold" aria-hidden /> Format
        </button>
        <button type="button" className="wf-btn wf-btn-sm" onClick={() => void copy()} data-testid="wf-json-copy">
          <Copy size={13} weight="bold" aria-hidden /> {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" className="wf-btn wf-btn-sm" aria-pressed={showChanges}
                disabled={!state.loaded} title={state.loaded ? "Against the published version" : "Nothing published yet"}
                onClick={() => setShowChanges((v) => !v)} data-testid="wf-json-changes">
          <GitDiff size={13} weight="bold" aria-hidden /> Changes
        </button>
        <span className="wf-spacer" />
        {error ? (
          <span className="wf-json-state wf-red"><WarningCircle size={14} weight="bold" aria-hidden /> JSON error</span>
        ) : (
          <span className="wf-json-state wf-green"><CheckCircle size={14} weight="bold" aria-hidden /> Valid JSON</span>
        )}
      </div>

      {error ? (
        <button type="button" className="wf-json-error" role="alert" data-testid="wf-json-error"
                onClick={() => placeCaret(error.line, true)}>
          Line {error.line}: {error.message}
        </button>
      ) : null}

      {diff ? (
        <div className="wf-json-diff" data-testid="wf-json-diff" aria-label="Changes">
          {changed === 0 ? <div className="wf-faint wf-json-diff-none">No changes</div> : diff.map((d, i) => (
            "fold" in d
              ? <div key={i} className="wf-diff-fold">⋯ {d.fold} unchanged</div>
              : <div key={i} className="wf-diff-line" data-kind={d.kind}>
                  <span className="wf-diff-no">{d.newNo ?? d.oldNo}</span>
                  <span className="wf-diff-sign">{d.kind === "add" ? "+" : d.kind === "del" ? "−" : " "}</span>
                  <span className="wf-diff-text">{d.text}</span>
                </div>
          ))}
        </div>
      ) : null}

      <div className="wf-json-editor">
        <div className="wf-json-gutter" ref={gutter} aria-hidden>
          <div className="wf-json-gutter-inner">
            {Array.from({ length: lineCount }, (_, i) => {
              const msg = problemLines.get(i + 1);
              return (
                <div key={i} className="wf-json-ln" data-problem={msg ? true : undefined} title={msg}>
                  {msg ? <span className="wf-json-dot" /> : null}{i + 1}
                </div>
              );
            })}
          </div>
        </div>
        <textarea ref={area} className="wf-json-text" value={text} spellCheck={false}
                  autoCapitalize="off" autoComplete="off" autoCorrect="off" wrap="off"
                  aria-label="Workflow JSON" aria-invalid={error ? true : undefined}
                  readOnly={state.meta.readOnlyVersion != null}
                  data-testid="wf-json-text"
                  onChange={(e) => write(e.target.value)}
                  onKeyDown={onKeyDown}
                  onSelect={readCaret}
                  onClick={readCaret}
                  onKeyUp={readCaret}
                  onScroll={(e) => {
                    const inner = gutter.current?.firstElementChild as HTMLElement | null;
                    if (inner) inner.style.transform = `translateY(${-e.currentTarget.scrollTop}px)`;
                  }} />
      </div>

      {state.problems.length ? (
        <ul className="wf-json-problems" aria-label="Problems" data-testid="wf-json-problems">
          {state.problems.map((p, i) => {
            const line = lineOfProblem(text, p);
            return (
              <li key={i}>
                <button type="button" className="wf-json-problem" title={p.message}
                        disabled={line === null}
                        onClick={() => { if (line !== null) placeCaret(line, true); if (p.step_id) dispatch({ type: "SELECT", id: p.step_id }); }}>
                  {line !== null ? `Line ${line} · ` : ""}{p.step_id ? `${p.step_id} · ` : ""}{p.message}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
