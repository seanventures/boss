"use client";

import React, { useCallback, useEffect, useState } from "react";

/**
 * The knobs, and what each is worth RIGHT NOW.
 *
 * How hard BOSS runs lived entirely in environment variables, so changing a number meant an
 * ops action and a container restart. In practice that meant nobody changed them and the
 * defaults became the product.
 *
 * Each row says where its value came from, because "this project runs three agents" and
 * "nobody has chosen, so it runs the deployment default" are different facts — and only one of
 * them tells you whether turning the dial here will do anything.
 */
type Knob = {
  key: string; label: string; help: string; kind: string;
  min?: number | null; max?: number | null;
  /** For kind "choice": the models THIS PROJECT has connected — not every harness BOSS can
   *  drive. A picker of eleven options on an install with one credential is a picker where
   *  ten choices fail at build time and nothing on screen says which. */
  choices?: string[] | null;
  /** Whether `choices` is a LIMIT or a SUGGESTION. `codex --model` takes any name OpenAI
   *  offers, so a closed dropdown makes a model that works unselectable the week after it
   *  ships. Claude's aliases are the complete set, so that picker stays a real dropdown. */
  open_choices?: boolean | null;
  value: number | string | null; default: number | string | null; source: string;
};

/**
 * `only` splits the knobs across panels.
 *
 * Which model fills which role and how hard BOSS runs are different questions, and rendering
 * them as one list put the model pickers under a heading that reads "How hard BOSS runs" —
 * findable only by somebody who already knew they were there.
 */
export function SettingsKnobs(
  { projectId, actor, only }: { projectId: string; actor: string; only?: "choice" | "number" },
) {
  const [knobs, setKnobs] = useState<Knob[] | null>(null);
  const [connected, setConnected] = useState<string[]>([]);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/settings?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (!r.ok) { setKnobs([]); return; }
      const body = await r.json();
      setKnobs(body.knobs ?? []);
      setConnected((body.connected_engines ?? []) as string[]);
    } catch { setKnobs([]); }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const save = async (key: string, value: string) => {
    setBusy(key);
    setNote(null);
    try {
      const r = await fetch(`/api/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, project_id: projectId, actor,
                               value: value === "" ? null : value }),
      });
      const body = await r.json();
      // The API's own sentence — "Agents at once has to be between 1 and 16" is the whole
      // value of the bound; "could not save" throws it away.
      setNote(r.ok ? `Saved — ${key} is now ${body.value} (${body.source}).`
                   : (body.error ?? body.detail ?? "Could not save."));
      await load();
    } catch (e) {
      setNote(String((e as Error)?.message ?? e));
    } finally {
      setBusy(null);
    }
  };

  if (knobs === null) return <p className="detail">Loading settings…</p>;
  const shown = only
    ? knobs.filter((k) => (only === "choice" ? k.kind === "choice" : k.kind !== "choice"))
    : knobs;
  if (!shown.length) return <p className="detail" data-testid="settings-none">No adjustable settings.</p>;

  return (
    <div className="settings-knobs" data-testid="settings-knobs">
      {/* WHY THE LIST IS THIS SHORT. Without it a one-option picker reads as a limit of the
          product rather than as "you have connected one model", and the fix — connect
          another, above — is invisible. */}
      {only === "choice" ? (
        <p className="detail" data-testid="settings-connected-engines">
          {connected.length
            ? <>Choosing between <b>{connected.join(", ")}</b> — the models this project has
                connected. Connect another above and it appears here.</>
            : <>Nothing is connected yet, so every harness BOSS can drive is listed. Connect a
                model above and these narrow to the ones that will actually run.</>}
        </p>
      ) : null}
      {shown.map((k) => (
        <div className="settings-knob" key={k.key}>
          <div className="settings-knob-head">
            <label htmlFor={`knob-${k.key}`}>{k.label}</label>
            <span className="settings-source" data-source={k.source}>{k.source}</span>
          </div>
          <p className="detail">{k.help}</p>
          <div className="settings-knob-row">
            {k.kind === "choice" && k.open_choices ? (
              // A SUGGESTION LIST, NOT A LIMIT. Typed freely, with the known names offered as
              // completions — the operator can pick gpt-5.2-codex from the list or type the
              // model OpenAI shipped yesterday, and neither is second-class.
              <>
                <input
                  id={`knob-${k.key}`}
                  className="input"
                  list={`knob-${k.key}-options`}
                  placeholder="the engine's own default"
                  defaultValue={String(k.value ?? "")}
                  disabled={busy === k.key}
                  data-testid={`knob-${k.key}`}
                  onBlur={(e) => {
                    if (String(k.value ?? "") !== e.target.value) void save(k.key, e.target.value);
                  }}
                />
                <datalist id={`knob-${k.key}-options`}>
                  {(k.choices ?? []).filter(Boolean).map((choice) => (
                    <option key={choice} value={choice} />
                  ))}
                </datalist>
              </>
            ) : k.kind === "choice" ? (
              // A model is a choice among what this installation can actually drive, so the
              // options come from the API rather than being listed here — a list in the console
              // would drift from the harness registry, and the failure would be a provider the
              // operator has connected and cannot select.
              <select
                id={`knob-${k.key}`}
                className="input"
                defaultValue={String(k.value ?? "")}
                disabled={busy === k.key}
                data-testid={`knob-${k.key}`}
                onChange={(e) => {
                  if (String(k.value ?? "") !== e.target.value) void save(k.key, e.target.value);
                }}
              >
                {/* Empty is a real answer for these: it means "whoever implements also does
                    this", which is the default for the reviewer and both escalation lanes. */}
                <option value="">— use the implementer —</option>
                {(k.choices ?? []).map((choice) => (
                  <option key={choice} value={choice}>{choice}</option>
                ))}
              </select>
            ) : (
              <input
                id={`knob-${k.key}`}
                className="input"
                type="number"
                min={k.min ?? undefined}
                max={k.max ?? undefined}
                defaultValue={String(k.value ?? "")}
                disabled={busy === k.key}
                data-testid={`knob-${k.key}`}
                onBlur={(e) => {
                  if (String(k.value ?? "") !== e.target.value) void save(k.key, e.target.value);
                }}
              />
            )}
            {k.source === "project" ? (
              // Deleting is how a project says "use the default" — storing the current default
              // instead would freeze it against a later change to the deployment.
              <button type="button" className="cap-btn" disabled={busy === k.key}
                      data-testid={`knob-reset-${k.key}`}
                      onClick={() => void save(k.key, "")}>
                Use the default ({String(k.default ?? "—")})
              </button>
            ) : null}
          </div>
        </div>
      ))}
      {note ? <p className="settings-note" data-testid="settings-note">{note}</p> : null}
    </div>
  );
}


/**
 * WHO DOES WHAT, AS A TABLE.
 *
 * The model pickers were twelve stacked cards — label, paragraph, dropdown — so answering "what
 * reviews, and on which model?" meant reading two cards that sat five cards apart, and the
 * operator said so: very difficult to go through. Every one of those knobs is half of a pair,
 * an ENGINE and a MODEL for one job, so the honest shape is one row per job with the pair side
 * by side, and the paragraph shrunk to the one line that says what the job is.
 *
 * Each cell also says where its value comes from: chosen here, or inherited. An empty picker
 * that silently means "the implementer's" is the reason a reviewer ends up the same model as
 * the builder without anybody deciding it.
 */
const ROLE_ROWS: {
  role: string; does: string; engine: string | null; model: string | null;
  emptyEngine: string; emptyModel: string;
}[] = [
  { role: "Implementer", does: "Writes the code and its tests.",
    engine: "code_agent_engine", model: "code_agent_model",
    emptyEngine: "deployment default", emptyModel: "engine default" },
  { role: "Reviewer", does: "Reviews the code adversarially. Best on a different model.",
    engine: "code_review_engine", model: "code_review_model",
    emptyEngine: "same as implementer", emptyModel: "lane default" },
  { role: "Hard problems", does: "Escalations, repairs after failure, security checks.",
    engine: "code_agent_strong_engine", model: "code_agent_strong_model",
    emptyEngine: "same as implementer", emptyModel: "engine default" },
  { role: "Routine work", does: "Small, well-specified changes and design stages.",
    engine: "code_agent_low_cost_engine", model: "code_agent_low_cost_model",
    emptyEngine: "same as implementer", emptyModel: "engine default" },
  { role: "Chat, agents, meetings", does: "Answers in the console and speaks in panels.",
    engine: "chat_llm_engine", model: "claude_cli_model",
    emptyEngine: "same as implementer", emptyModel: "engine default" },
  { role: "Workflow decisions", does: "Picks a branch from fixed options. Jev needs a key, saved in the Model section above.",
    engine: "decision_backend", model: "decision_model",
    emptyEngine: "deployment default", emptyModel: "deployment default" },
  { role: "Codex CLI", does: "The model Codex runs whenever a role uses it.",
    engine: null, model: "codex_cli_model",
    emptyEngine: "", emptyModel: "CLI default" },
];

function RoleCell(
  { knob, empty, busy, onSave, origin }:
  { knob: Knob | undefined; empty: string; busy: boolean;
    onSave: (key: string, value: string) => void;
    /** What this value IS, where "inherited" would be wrong — the decisions row's model is
     *  Claude's second opinion when Jev is unsure, not a value handed down from elsewhere. */
    origin?: string },
) {
  if (!knob) return <td className="role-cell role-na">—</td>;
  // WHAT AN EMPTY PICKER REALLY FALLS BACK TO. "same as implementer (claude_cli)" beside an
  // implementer of claude_code contradicted itself: when the deployment sets a value, empty
  // means THAT value, not the implementer's.
  const fallback = knob.default !== null && knob.default !== ""
    ? `deployment default (${knob.default})` : empty;
  const control = knob.open_choices ? (
    <>
      <input className="input" list={`role-${knob.key}-options`} aria-label={knob.label}
             key={`${knob.key}:${String(knob.value ?? "")}`}
             placeholder={fallback} defaultValue={String(knob.value ?? "")}
             disabled={busy} data-testid={`knob-${knob.key}`}
             onBlur={(e) => {
               if (String(knob.value ?? "") !== e.target.value) onSave(knob.key, e.target.value);
             }} />
      <datalist id={`role-${knob.key}-options`}>
        {(knob.choices ?? []).filter(Boolean).map((c) => <option key={c} value={c} />)}
      </datalist>
    </>
  ) : (
    <select className="input" aria-label={knob.label} defaultValue={String(knob.value ?? "")}
            key={`${knob.key}:${String(knob.value ?? "")}:${(knob.choices ?? []).join(",")}`}
            disabled={busy} data-testid={`knob-${knob.key}`}
            onChange={(e) => {
              if (String(knob.value ?? "") !== e.target.value) onSave(knob.key, e.target.value);
            }}>
      <option value="">{fallback}</option>
      {(knob.choices ?? []).filter(Boolean).map((c) => <option key={c} value={c}>{c}</option>)}
    </select>
  );
  return (
    <td className="role-cell" title={knob.help}>
      {control}
      <span className="role-origin" data-source={knob.source}>
        {origin ?? (knob.source === "project" ? "set here" : "inherited")}
        {knob.source === "project" ? (
          <button type="button" className="role-reset" disabled={busy}
                  data-testid={`knob-reset-${knob.key}`} onClick={() => onSave(knob.key, "")}>
            reset
          </button>
        ) : null}
      </span>
    </td>
  );
}

export function RoleTable({ projectId, actor }: { projectId: string; actor: string }) {
  const [knobs, setKnobs] = useState<Knob[] | null>(null);
  const [connected, setConnected] = useState<string[]>([]);
  const [jevKey, setJevKey] = useState<boolean | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/settings?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (!r.ok) { setKnobs([]); return; }
      const body = await r.json();
      setKnobs(body.knobs ?? []);
      setConnected((body.connected_engines ?? []) as string[]);
    } catch { setKnobs([]); }
    try {
      const r = await fetch(
        `/api/setup?view=decision-model&project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (r.ok) setJevKey(Boolean((await r.json()).typesafe_key));
    } catch { setJevKey(null); }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const save = async (key: string, value: string) => {
    setBusy(key); setNote(null);
    try {
      const r = await fetch(`/api/settings`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, project_id: projectId, actor,
                               value: value === "" ? null : value }),
      });
      const body = await r.json();
      const label = knobs?.find((k) => k.key === key)?.label ?? key;
      const clearedLabel = body.cleared
        ? knobs?.find((k) => k.key === body.cleared)?.label ?? body.cleared : null;
      setNote(r.ok ? `Saved — ${label}: ${body.value || "inherited"}.`
                     + (clearedLabel ? ` ${clearedLabel} was cleared, because it was chosen for `
                                       + `the previous engine — pick one for this engine.` : "")
                     + " Applies on the next run."
                   : (body.error ?? body.detail ?? "Could not save."));
      await load();
    } catch (e) {
      setNote(String((e as Error)?.message ?? e));
    } finally { setBusy(null); }
  };

  if (knobs === null) return <p className="detail">Loading settings…</p>;
  const byKey = new Map(knobs.map((k) => [k.key, k]));
  const placed = new Set(ROLE_ROWS.flatMap((r) => [r.engine, r.model]).filter(Boolean));
  // A knob added later and not yet given a row still shows, rather than vanishing.
  const unplaced = knobs.filter((k) => k.kind === "choice" && !placed.has(k.key));

  return (
    <div className="role-table-wrap" data-testid="role-table">
      <p className="detail" data-testid="settings-connected-engines">
        {connected.length
          ? <>Choosing between <b>{connected.join(", ")}</b> — connect another model above and
              it appears here. Changes apply on the next run; nothing restarts.</>
          : <>Nothing is connected yet, so every engine BOSS can drive is listed.</>}
      </p>
      <table className="role-table">
        <thead>
          <tr><th scope="col">Job</th><th scope="col">Runs on</th><th scope="col">Model</th></tr>
        </thead>
        <tbody>
          {ROLE_ROWS.map((row) => {
            const engine = row.engine ? byKey.get(row.engine) : undefined;
            // THE CHAT ROW'S MODEL FOLLOWS ITS ENGINE. claude_cli_model is the Claude CLI's model;
            // with chat on codex that picker offered Claude models for a Codex conversation.
            const chatEngine = String(engine?.value || engine?.default || "");
            const modelKey = row.engine === "chat_llm_engine"
              ? (chatEngine.startsWith("codex") ? "codex_cli_model"
                 : chatEngine.startsWith("claude") ? "claude_cli_model" : null)
              : row.model;
            const model = modelKey ? byKey.get(modelKey) : undefined;
            if (!engine && !model) return null;
            const decisions = row.engine === "decision_backend";
            return (
              <tr key={row.role} data-testid={`role-row-${row.role}`}>
                <th scope="row" className="role-name">
                  <span>{row.role}</span>
                  <small>{row.does}</small>
                  {decisions && jevKey !== null ? (
                    <small className="role-flag" data-ok={jevKey ? "1" : "0"}>
                      {jevKey ? "Jev key saved" : "no Jev key — Claude decides"}
                    </small>
                  ) : null}
                </th>
                {row.engine
                  ? <RoleCell knob={engine} empty={row.emptyEngine} busy={busy === row.engine}
                              onSave={(k, v) => void save(k, v)} />
                  : <td className="role-cell role-na">codex</td>}
                <RoleCell knob={model} empty={row.emptyModel} busy={busy === modelKey}
                          onSave={(k, v) => void save(k, v)}
                          origin={decisions
                            ? (engine?.value === "typesafe"
                               ? "Claude's second opinion when Jev is unsure"
                               : "Claude decides")
                            : undefined} />
              </tr>
            );
          })}
          {unplaced.map((k) => (
            <tr key={k.key}>
              <th scope="row" className="role-name"><span>{k.label}</span><small>{k.help}</small></th>
              <RoleCell knob={k} empty="default" busy={busy === k.key}
                        onSave={(key, v) => void save(key, v)} />
              <td className="role-cell role-na">—</td>
            </tr>
          ))}
        </tbody>
      </table>
      {note ? <p className="settings-note" data-testid="settings-note">{note}</p> : null}
    </div>
  );
}
