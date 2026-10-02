/**
 * THE ONE WAY INTO THE SIDE DOCK: a typed subject, the operator's literal question, and who
 * answers.
 *
 * THE INCIDENT (boss-1, 2026-10-02). A Preview step failed with an ImportError. "Ask the Qa"
 * opened the dock with a sentence built from the step — its error cut to 400 characters,
 * mid-word, and "Check the app's code and the host before answering." stapled on — scoped to
 * the BUILD (`agent=crun_…`), because a run id was the only thing a door could hand over. The
 * workflow run, the step, the preview and its log never travelled. The server framed the turn as
 * a question about a completed build, the word "host" sent it down the deployment chain, and
 * 276 seconds later it answered that the app "has never been deployed".
 *
 * Eleven doors each built their own URL, their own prose and their own idea of what to send.
 * Now there is this function. A door says WHAT the question is about (`subject`, a kind and an
 * id) and WHAT the operator is asking (`question`, in their words, prefilled and editable); the
 * server resolves the subject into its full record and answers from it. Facts do not go in the
 * question, and neither do instructions to go exploring.
 *
 * Enforced by tests/every-ask-door-attaches-a-subject.test.ts: no file under app/ may set
 * `dock_ask` itself, and every door that is about a record must pass that record.
 */

/** Every kind of thing a door can ask about. MIRRORED by SUBJECT_KINDS in
 *  engine/boss_engine/services/turn_subject.py — an engine test reads this list and fails if
 *  any kind here has no resolver there. */
export const SUBJECT_KINDS = [
  "workflow_step",
  "workflow_run",
  "code_task",
  "preview",
  "discovery",
  "deployment",
  "decision",
] as const;

export type SubjectKind = (typeof SUBJECT_KINDS)[number];

export type DockSubject = { kind: SubjectKind; id: string; step?: string };

/** The thread key for a conversation about this subject — what the turn sends as
 *  `external_ref`, and what the server parses back. A build keeps its bare run id, which every
 *  build conversation has always been keyed on. */
export function subjectRef(subject: DockSubject): string {
  if (subject.kind === "code_task") return subject.id;
  return `${subject.kind}:${subject.id}${subject.step ? `:${subject.step}` : ""}`;
}

export function parseSubjectRef(ref: string | null | undefined): DockSubject | null {
  const value = String(ref ?? "").trim();
  if (!value) return null;
  const [head, id, step] = value.split(":");
  if ((SUBJECT_KINDS as readonly string[]).includes(head) && id) {
    return { kind: head as SubjectKind, id, ...(step ? { step } : {}) };
  }
  if (value.startsWith("crun_")) return { kind: "code_task", id: value };
  return null;
}

export type DockOpening = {
  /** What the question is about. `null` only for a door with no record behind it — designing
   *  a workflow, connecting a repository, an idea — and then the turn is a general one. */
  subject: DockSubject | null;
  /** The operator's question, as they would say it. Prefilled, so they can see and edit it. */
  question: string;
  /** Who answers. `null` asks BOSS in general. Never omitted: `dock_role` survives in the URL,
   *  so silence would inherit whichever agent was addressed last. */
  role: string | null;
  /** Start a new conversation rather than resuming the last one with this agent. */
  fresh?: boolean;
  /** "discuss" for thinking about an idea. Cleared otherwise, for the same reason as role. */
  mode?: "discuss";
  /** pushState (a navigation the Back button should undo) or replaceState (default). */
  history?: "push" | "replace";
  /** Other params the page needs in the SAME history entry — e.g. code tasks' `run`. */
  also?: Record<string, string>;
};

export function dockParams(o: DockOpening): Record<string, string | null> {
  return {
    agent: o.subject ? subjectRef(o.subject) : "general",
    dock_ask: o.question || null,
    dock_role: o.role,
    dock_mode: o.mode ?? null,
    dock_fresh: o.fresh ? "1" : null,
    ...(o.also ?? {}),
  };
}

export function openDockAbout(o: DockOpening): void {
  try {
    const url = new URL(window.location.href);
    for (const [key, value] of Object.entries(dockParams(o))) {
      if (value === null) url.searchParams.delete(key);
      else url.searchParams.set(key, value);
    }
    if (o.history === "push") window.history.pushState({}, "", url.toString());
    else window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}
