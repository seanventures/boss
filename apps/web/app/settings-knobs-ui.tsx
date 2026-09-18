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
  /** For kind "choice": what this installation can actually drive, from the harness registry. */
  choices?: string[] | null;
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
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/settings?project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (!r.ok) { setKnobs([]); return; }
      const body = await r.json();
      setKnobs(body.knobs ?? []);
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
      {shown.map((k) => (
        <div className="settings-knob" key={k.key}>
          <div className="settings-knob-head">
            <label htmlFor={`knob-${k.key}`}>{k.label}</label>
            <span className="settings-source" data-source={k.source}>{k.source}</span>
          </div>
          <p className="detail">{k.help}</p>
          <div className="settings-knob-row">
            {k.kind === "choice" ? (
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
