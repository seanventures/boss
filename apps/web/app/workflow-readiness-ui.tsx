"use client";

/**
 * CAN THIS WORKFLOW RUN HERE — shown before Start, with the fix for each thing missing.
 *
 * Every catalogue template but one ends in a deploy, and a project with no deployment host
 * started them anyway: research, a panel, a build and a launch-readiness repair were paid for,
 * a person approved the release, and the run failed at its last step (boss-1, 2026-10-02). The
 * email examples did the same at an approved send on an install with no mail server. The
 * engine now answers "what does this need and is it here" (`workflow_readiness.py`) and Start
 * refuses until it is answered; this draws that answer in the start form, so the operator sees
 * the list — and the place to fix each item — before pressing anything.
 *
 * Kept in its own module so the start form, and anything else that starts work, draws the same
 * checklist rather than each wording it differently.
 */

import { useEffect, useState } from "react";

import { AreaLink } from "./area-link";

export type ReadinessNeed = {
  key: string;
  label: string;
  fix: string;
  area: string | null;
  ok: boolean;
  detail: string;
  needed_by: { step_id: string; label: string }[];
  first_step_label: string;
  /** True when the run stops for a person before the first step that needs this, so it can be
   *  added while the run is under way. */
  after_a_person: boolean;
};

export type Readiness = {
  ready: boolean;
  checked: ReadinessNeed[];
  missing: ReadinessNeed[];
  summary?: string;
};

/** The fields the New project wizard collects on its own steps — the brief, its name, the
 *  repository, the criteria and the links. A catalogue template declares them too (so an agent
 *  or an API caller can see what it reads), and drawing them again as declared inputs would ask
 *  the operator for the same thing twice. */
export const WIZARD_COLLECTS: ReadonlySet<string> = new Set([
  "title", "directive", "repository", "acceptance_criteria", "reference_links",
]);

/** Missing needs the operator has not chosen to start without. Start stays blocked while this
 *  is non-empty — the API would refuse anyway, and the form is where to find out. */
export function unaccepted(readiness: Readiness | null, accepted: readonly string[]) {
  return (readiness?.missing ?? []).filter((m) => !accepted.includes(m.key));
}

/** The readiness of one workflow for this project, refreshed when what is typed changes the
 *  answer (a notify channel). `null` while unknown — the form does not block on a fetch. */
export function useWorkflowReadiness(
  projectId: string, workflowKey: string, inputs: Record<string, string>,
): Readiness | null {
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const channel = inputs.notify_channel ?? "";
  useEffect(() => {
    if (!projectId || !workflowKey) { setReadiness(null); return; }
    let alive = true;
    const params = new URLSearchParams({ view: "readiness", project_id: projectId,
                                         key: workflowKey });
    if (channel.trim()) params.set("input", JSON.stringify({ notify_channel: channel.trim() }));
    (async () => {
      try {
        const r = await fetch(`/api/delivery-workflows?${params}`, { cache: "no-store" });
        if (!r.ok) { if (alive) setReadiness(null); return; }
        const body = await r.json() as Readiness;
        if (alive) setReadiness(body);
      } catch { if (alive) setReadiness(null); }
    })();
    return () => { alive = false; };
  }, [projectId, workflowKey, channel]);
  return readiness;
}

export function ReadinessPanel({ readiness, accepted, onAccept }: {
  readiness: Readiness | null;
  accepted: readonly string[];
  onAccept: (keys: string[]) => void;
}) {
  if (!readiness || !readiness.checked?.length) return null;
  const missing = readiness.missing ?? [];
  if (!missing.length) {
    return (
      <p className="dw-ready dw-ready-ok" data-testid="dw-readiness-ok">
        Ready to run here: {readiness.checked.map((c) => c.label.toLowerCase()).join(", ")}.
      </p>
    );
  }
  const open = unaccepted(readiness, accepted);
  return (
    <section className="dw-ready" data-testid="dw-readiness" aria-label="What this needs first">
      <h4>This cannot run here yet</h4>
      <ul className="dw-ready-list">
        {missing.map((need) => (
          <li key={need.key} data-testid={`dw-readiness-missing-${need.key}`}
              data-accepted={accepted.includes(need.key) || undefined}>
            <strong>{need.label}</strong>
            <span className="detail">
              {" "}— needed by {need.needed_by.map((n) => n.label).join(", ")}. {need.detail}.
            </span>
            <span className="dw-ready-fix">
              {need.area ? (
                <AreaLink area={need.area}>{need.fix}</AreaLink>
              ) : need.fix}
            </span>
            {need.after_a_person ? (
              <span className="detail dw-ready-later">
                The run stops for you before {need.first_step_label}, so this can be added
                while it runs.
              </span>
            ) : null}
          </li>
        ))}
      </ul>
      {/* STARTING ANYWAY IS A CHOICE MADE OUT LOUD. The keys go with the start request and the
          run records that it began without them; anything missing that was not on this list
          still refuses. */}
      <label className="dw-ready-anyway">
        <input type="checkbox" data-testid="dw-readiness-start-anyway"
               checked={open.length === 0}
               onChange={(e) => onAccept(e.target.checked ? missing.map((m) => m.key) : [])} />
        {" "}Start anyway — I will add {missing.length === 1 ? "this" : "these"} before the
        step that needs {missing.length === 1 ? "it" : "them"}.
      </label>
    </section>
  );
}
