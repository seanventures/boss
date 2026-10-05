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

import React, { useEffect, useState } from "react";

import { AreaLink } from "./area-link";

/** THE FIX KEEPS THE ORG (re-check): the API's landing (`/?area=setup&focus=repository`)
 *  carries no org, and a full load without it lands on the org picker. The org the console is
 *  in is added here, at the source every refusal and start form draws. */
export function withOrg(href: string, org: string | null): string {
  if (!org || !href.startsWith("/?") || /[?&]org=/.test(href)) return href;
  return `/?org=${encodeURIComponent(org)}&${href.slice(2)}`;
}

function useOrg(): string | null {
  const [org, setOrg] = useState<string | null>(null);
  useEffect(() => {
    try { setOrg(new URL(window.location.href).searchParams.get("org")); } catch { /* none */ }
  }, []);
  return org;
}
import { isAreaId } from "../lib/area-ids";
import { EmailSetup } from "./email-setup-ui";

export type ReadinessNeed = {
  key: string;
  label: string;
  fix: string;
  area: string | null;
  /** Where in that area the fix lands, e.g. `/?area=setup&focus=repository`. */
  href?: string | null;
  ok: boolean;
  detail: string;
  needed_by: { step_id: string; label: string }[];
  first_step_label: string;
  /** True when the run stops for a person before the first step that needs this, so it can be
   *  added while the run is under way. */
  after_a_person: boolean;
};

/** Something worth knowing before Start that does not stop it — the budget at orange or
 *  yellow (workflow_readiness._budget_readiness). Red is a need, not a note. */
export type ReadinessNote = { key: string; level: string; text: string; area?: string | null };

export type Readiness = {
  ready: boolean;
  checked: ReadinessNeed[];
  missing: ReadinessNeed[];
  notes?: ReadinessNote[];
  summary?: string;
};

function ReadinessNotes({ notes }: { notes?: ReadinessNote[] }) {
  if (!notes?.length) return null;
  return (
    <>
      {notes.map((note) => (
        <p key={note.key} className="dw-ready dw-ready-note" data-testid={`dw-readiness-note-${note.key}`}>
          {isAreaId(note.area) ? <AreaLink area={note.area}>{note.text}</AreaLink> : note.text}
        </p>
      ))}
    </>
  );
}

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

export function ReadinessPanel({ readiness, accepted, onAccept, projectId = "" }: {
  readiness: Readiness | null;
  accepted: readonly string[];
  onAccept: (keys: string[]) => void;
  /** Lets an item that can be fixed in place (email) draw its form. */
  projectId?: string;
}) {
  const org = useOrg();
  if (!readiness || !readiness.checked?.length) return null;
  const missing = readiness.missing ?? [];
  if (!missing.length) {
    return (
      <>
        <p className="dw-ready dw-ready-ok" data-testid="dw-readiness-ok">
          Ready to run here: {readiness.checked.map((c) => c.label.toLowerCase()).join(", ")}.
        </p>
        <ReadinessNotes notes={readiness.notes} />
      </>
    );
  }
  const open = unaccepted(readiness, accepted);
  return (
    <section className="dw-ready" data-testid="dw-readiness" aria-label="What this needs first">
      <h4>This cannot run here yet</h4>
      <ReadinessNotes notes={readiness.notes} />
      <ul className="dw-ready-list">
        {missing.map((need) => (
          <li key={need.key} data-testid={`dw-readiness-missing-${need.key}`}
              data-accepted={accepted.includes(need.key) || undefined}>
            <strong>{need.label}</strong>
            {/* SAID ONCE, SHORT (independent test D6): the refusal's line and this list both
                said every reason in full — ~80 words twice. The item is its name and its fix;
                why it is needed is one press away. */}
            <details className="dw-ready-why">
              <summary>Why</summary>
              <span className="detail">
                Needed by {need.needed_by.map((n) => n.label).join(", ")}. {need.detail}.
              </span>
              {need.after_a_person ? (
                <span className="detail dw-ready-later">
                  {" "}The run stops for you before {need.first_step_label}, so this can be added
                  while it runs.
                </span>
              ) : null}
            </details>
            <span className="dw-ready-fix">
              {/* Only an area the console has: the API names it as a string, and a link to an area
                  that is gone did nothing when clicked (console-b, lib/area-ids.ts). */}
              {/* The fix's own landing (`?area=setup&focus=repository`, `?area=skills&ability=email`)
                  when the API names one; the shell keeps the org and switches in place. */}
              {need.href && isAreaId(need.area) ? (
                <a href={withOrg(need.href, org)} data-testid={`dw-readiness-fix-${need.key}`}>{need.fix}</a>
              ) : isAreaId(need.area) ? (
                <AreaLink area={need.area}>{need.fix}</AreaLink>
              ) : need.fix}
            </span>
            {/* THE FIX WHERE THE PROBLEM IS. Email is the one need a short form settles, so
                it is drawn right here rather than sending the operator away from Start. */}
            {need.key === "email" && projectId ? (
              <EmailSetup projectId={projectId} compact />
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

/** The checklist a refusal carried, if it is one. A proxy hands it over as `unknown`. */
export function asReadiness(value: unknown): Readiness | null {
  if (!value || typeof value !== "object") return null;
  const r = value as Partial<Readiness>;
  return Array.isArray(r.missing) && Array.isArray(r.checked) ? r as Readiness : null;
}

/** A start that was refused for what the project lacks, waiting for the operator's answer. */
export type PendingRefusal = {
  /** What was being started — "Build", "Re-run" — said in the heading and the button. */
  label: string;
  message: string;
  readiness: Readiness;
  /** Send the same start again, with the keys the operator accepted. */
  retry: (startAnyway: string[]) => Promise<unknown> | void;
};

/**
 * EVERY DOOR THAT STARTS WORK DRAWS THE SAME REFUSAL.
 *
 * The start form had a checklist and "Start anyway"; Build, Re-run, Build on this research,
 * the run report's Fix and Agent Studio's Run had a sentence at best, and Build crashed (review
 * of the readiness change). This is the one drawing of "cannot run here yet" for all of them:
 * the list, each item's fix, and a Start anyway that sends exactly the keys ticked.
 */
export function StartRefusal({ refusal, projectId = "", busy = false, onDismiss }: {
  refusal: PendingRefusal;
  projectId?: string;
  busy?: boolean;
  onDismiss?: () => void;
}) {
  const [accepted, setAccepted] = useState<string[]>([]);
  const open = unaccepted(refusal.readiness, accepted);
  return (
    <div className="dw-refusal" data-testid="start-refusal" role="alert">
      {/* ONE SHORT LINE; the list below names each thing and its fix. The API's whole sentence
          stays one hover away. */}
      <p className="dw-new-error" title={refusal.message}>
        {refusal.label} — not started.{" "}
        {refusal.readiness.missing.length
          ? `${refusal.readiness.missing.length} ${refusal.readiness.missing.length === 1 ? "thing" : "things"} missing.`
          : refusal.message}
      </p>
      <ReadinessPanel readiness={refusal.readiness} accepted={accepted}
                      onAccept={setAccepted} projectId={projectId} />
      <div className="dw-refusal-foot">
        <button type="button" className="cap-btn primary" data-testid="start-refusal-anyway"
                disabled={busy || open.length > 0}
                title={open.length ? "Tick Start anyway above to accept what is missing" : ""}
                onClick={() => void refusal.retry(refusal.readiness.missing.map((m) => m.key))}>
          {busy ? "Starting…" : `${refusal.label} anyway`}
        </button>
        {onDismiss ? (
          <button type="button" className="linklike" data-testid="start-refusal-dismiss"
                  onClick={onDismiss}>Not now</button>
        ) : null}
      </div>
    </div>
  );
}
