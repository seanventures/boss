"use client";

/**
 * Ask BOSS — the first thing on the Command Center.
 *
 * What was here was a "Status Query" form that posted to Metatron: three inputs, a GET
 * submit, and a read-only paragraph back. It sat below the fold, it could only answer, and
 * two of its three fields asked the operator for ids they would have to go and look up.
 *
 * The agent can already do all of this and act as well, so the entry point is one line of
 * plain language at the top of the page. Scope is inferred, not typed — an operator who has
 * to find a task id before asking a question will go and read the table instead, which is
 * exactly the behaviour this is meant to replace.
 *
 * The animation earns its place by carrying state rather than decorating: the aura is calm
 * when idle, active while a turn runs, and amber once a turn passes what is normal. There is
 * no purely ornamental motion, and all of it collapses under prefers-reduced-motion.
 */

import { useCallback, useEffect, useRef, useState, useTransition } from "react";

import { Markdown } from "./markdown";
import { askBossAction, runAgentActionAction } from "./actions";

const NORMAL_SECONDS = 20;
const STALL_SECONDS = 120;

type ProposedAction = {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  label: string;
  why?: string;
};

/** Openers that demonstrate the range: read, diagnose, act. Each is a real request. */
const OPENERS = [
  { badge: "ST", tone: "accent", text: "What changed, what is blocked, and what should I do next?" },
  { badge: "RN", tone: "blue",   text: "Which runs failed today, and why?" },
  { badge: "QU", tone: "amber",  text: "Is anything starving or stuck in a queue?" },
  { badge: "$$", tone: "green",  text: "What is my spend so far this month?" },
];

/**
 * Starter outcomes, for the case the composer is worst at: an empty box and no idea.
 *
 * These are DIRECTIVES, not questions — each one goes down the governed path (panel, PRD,
 * code task), which is the same thing the separate "Assign an outcome" bar used to do. They
 * exist because a blank prompt is the hardest possible first run, and because the fastest way
 * to learn what BOSS does is to watch it do one.
 */
/**
 * Starter briefs.
 *
 * These were one-line prompts that fired a governed build the instant they were clicked —
 * which is both too little information to build from and too much commitment for a click. A
 * brief now DRAFTS into the composer: it says what the thing is, who it is for, what it must
 * do, and how you know it worked, and the operator edits it before pressing Build.
 *
 * Themes follow the categories YC and similar programmes keep asking for (developer tooling,
 * vertical SaaS, ops automation, data plumbing, agents). They are shipped in the bundle rather
 * than fetched, so they work offline and cost nothing to show; a live feed is a separate
 * capability and is noted in the UI rather than faked here.
 */
type Idea = {
  tag: string; title: string; brief: string;
  /** Hue for the card's thumbnail. A wall of identical grey cards is a list pretending to be a
   *  gallery — colour is what makes sixteen of them scannable rather than readable. */
  hue: number;
  /** Which glyph the thumbnail draws. Inline SVG, not an image URL: the console must render
   *  with no network beyond its own origin, and a broken <img> is worse than no picture. */
  glyph: GlyphId;
};

type GlyphId = keyof typeof GLYPHS;

/** Small, flat marks — a suggestion of the domain, not an illustration of it. Each is a single
 *  path on a 24-box so they sit at the same optical weight. */
const GLYPHS = {
  board: "M3 5h18v14H3zM9 5v14M15 5v14",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  clock: "M12 21a9 9 0 1 1 0-18 9 9 0 0 1 0 18zM12 7v5l3 2",
  inbox: "M3 13h5l2 3h4l2-3h5M4 13 6 5h12l2 8v6H4z",
  people: "M8 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM2 20a6 6 0 0 1 12 0M17 12a3 3 0 1 0 0-6M15 20h7a5 5 0 0 0-4-4.9",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  spark: "M12 3l2.2 6.2L20 11l-5.8 1.8L12 19l-2.2-6.2L4 11l5.8-1.8z",
  route: "M6 4v9a4 4 0 0 0 4 4h8M18 13l3 4-3 4",
  tag: "M3 12l9-9h8v8l-9 9zM16 7h.01",
  book: "M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 0-3 3zM5 4v19",
  map: "M9 4 3 6v14l6-2 6 2 6-2V4l-6 2z M9 4v14M15 6v14",
  bell: "M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6M10 20a2 2 0 0 0 4 0",
} as const;

const IDEAS: Idea[] = [
  {
    tag: "Ops", title: "Incident timeline builder", hue: 4, glyph: "clock",
    brief: `Build an incident timeline builder.
Who it is for: an on-call engineer writing the post-mortem the morning after.
Paste or upload the alert log and the chat transcript; it lays them on one timeline, marks
detection, escalation, mitigation and all-clear, and computes time-to-detect and time-to-resolve.
Editable — the tool proposes the milestones, the human corrects them.
Exports a markdown post-mortem with the timeline embedded.`,
  },
  {
    tag: "Money", title: "Subscription audit", hue: 152, glyph: "chart",
    brief: `Build a subscription audit tool.
Who it is for: a founder who suspects they are paying for tools nobody opens.
Upload a card statement CSV; it groups recurring charges by vendor, shows monthly and annual
cost, flags anything that rose in price, and lets you mark each one keep / cancel / renegotiate.
Shows the running total of what cancelling the marked ones would save.`,
  },
  {
    tag: "Team", title: "Interview scorecard", hue: 268, glyph: "people",
    brief: `Build an interview scorecard app.
Who it is for: a hiring manager tired of "seemed good?" as feedback.
Define a role with its competencies; each interviewer scores against them independently and
cannot see other scores until they submit theirs. Shows the spread, flags where interviewers
disagree most, and produces one summary per candidate.
Anti-anchoring is the point: hide the others until you have committed.`,
  },
  {
    tag: "Content", title: "Docs freshness board", hue: 196, glyph: "book",
    brief: `Build a documentation freshness board.
Who it is for: whoever owns docs nobody trusts any more.
Point it at a docs folder in a repo. For each page it shows last edited, the code paths it
mentions, and whether those paths have changed since. Sorts by "most likely to be wrong".
Mark a page reviewed to reset its clock.`,
  },
  {
    tag: "Sales", title: "Lead triage inbox", hue: 28, glyph: "inbox",
    brief: `Build a lead triage inbox.
Who it is for: a small team drowning in inbound that is 80% noise.
Paste a batch of enquiries; it scores each on fit against a profile you define, drafts a first
reply for the good ones and a polite decline for the rest, and queues them for one-click send.
Nothing sends without a human pressing send.`,
  },
  {
    tag: "Ops", title: "Change calendar", hue: 340, glyph: "map",
    brief: `Build a change calendar.
Who it is for: a team that keeps deploying into someone else's launch.
A shared calendar of planned changes with owner, blast radius and rollback plan. Warns when two
changes to the same system land the same day, and supports a freeze window nobody can book over
without stating a reason.`,
  },
  {
    tag: "Quality", title: "Bug triage board", hue: 12, glyph: "board",
    brief: `Build a bug triage board.
Who it is for: whoever runs triage and keeps re-litigating the same tickets.
Import issues; it clusters near-duplicates, proposes severity from the words used, and shows
what has been sitting untriaged longest. Every decision is one keystroke, and the board records
who decided what and when.`,
  },
  {
    tag: "Learning", title: "Onboarding path", hue: 120, glyph: "route",
    brief: `Build an onboarding path builder.
Who it is for: the person who explains the same system to every new joiner.
Compose a path of steps — read this, run that, ship a tiny change — with a checkpoint on each.
New joiners see progress; the author sees where people get stuck, which is the signal that the
docs are wrong.`,
  },
  {
    tag: "Money", title: "Pricing experiment log", hue: 44, glyph: "tag",
    brief: `Build a pricing experiment log.
Who it is for: a team changing prices and forgetting what they already tried.
Record each experiment — what changed, for whom, when, expected effect — and the outcome when
it lands. Prevents re-running a test you already lost, and shows the price history of every plan
on one line.`,
  },
  {
    tag: "Ops", title: "Vendor status watch", hue: 210, glyph: "bell",
    brief: `Build a vendor status watcher.
Who it is for: the engineer who finds out from a customer that Stripe is down.
Track the status pages of the services you depend on, show current state on one board, and keep
a history so "was it us or them?" is answerable a week later.
Notify on state changes only — a watcher that shouts every poll is one people mute.`,
  },
  {
    tag: "Team", title: "Decision log", hue: 288, glyph: "shield",
    brief: `Build a decision log.
Who it is for: a team that keeps rediscovering why something is the way it is.
One page per decision: the question, the options considered, what was chosen, who decided, and
what would make us revisit it. Searchable, and linkable from code comments.
Short by design — a template nobody fills in is worse than none.`,
  },
  {
    tag: "Growth", title: "Changelog to email", hue: 172, glyph: "spark",
    brief: `Build a changelog-to-email composer.
Who it is for: whoever owes customers a "what's new" and never sends it.
Reads merged pull requests since the last send, groups them into themes, drafts a short customer
-facing email in plain language, and holds it for approval.
Technical detail goes in a collapsible section; the top is what a customer cares about.`,
  },
  {
    tag: "Quality", title: "Test coverage trend", hue: 96, glyph: "chart",
    brief: `Build a coverage trend board.
Who it is for: a team whose coverage number moves and nobody knows why.
Store coverage per commit, chart it, and attribute each drop to the change that caused it.
Highlights files that lost coverage while being modified — the honest signal, as opposed to the
project-wide percentage.`,
  },
  {
    tag: "Ops", title: "Access expiry tracker", hue: 356, glyph: "shield",
    brief: `Build an access expiry tracker.
Who it is for: whoever grants "temporary" access that is still live a year later.
Record each grant with an expiry and an owner. Warns before it lapses, lists everything overdue,
and produces the evidence an auditor asks for.
Expiry is required — a grant with no end date is the thing this exists to prevent.`,
  },
  {
    tag: "Content", title: "Style guide linter", hue: 232, glyph: "book",
    brief: `Build a style guide linter for prose.
Who it is for: a team whose docs sound like four different companies.
Define the rules — terms to prefer, phrases to avoid, sentence length, voice — and check pages
against them. Every finding quotes the sentence and proposes a rewrite; nothing is changed
without a human accepting it.`,
  },
  {
    tag: "Team", title: "Rotation fairness board", hue: 64, glyph: "people",
    brief: `Build a rotation fairness board.
Who it is for: a team where the same two people always take the weekend.
Import the on-call history and show who covered what, weighted by how unsociable the slot was.
Proposes the next rotation to even it out, and shows the arithmetic — a fairness tool nobody can
check is just another opinion.`,
  },
];

/**
 * Research seeds — the step BEFORE a build.
 *
 * The composer could ask and it could build, so the only way in was to already know what you
 * wanted made. Discovery is the stage for when you do not: it researches a seed on the web and
 * produces a cited dossier plus candidate outcomes, which is where a directive should come
 * from when the idea is not yet formed.
 *
 * Phrased as questions, not products, because that is what separates research from a build.
 */
const RESEARCH: Array<{ label: string; seed: string }> = [
  { label: "On-call tooling",
    seed: "What are teams currently using to manage on-call rotas, and what do they complain about?" },
  { label: "Why internal tools die",
    seed: "Why do small teams abandon their internal tools within a year?" },
  { label: "Crowded categories",
    seed: "Which developer-tooling categories have had the most new entrants in the last year?" },
  { label: "Pricing for small teams",
    seed: "How do incident-management products differentiate on pricing for teams under 20 people?" },
];

const IDEAS_PER_PAGE = 4;

/**
 * POST to one of our own route handlers, with the same stall bound `ask` already had.
 *
 * Research and Build were Server Actions, which re-render the whole route to reply — on a
 * 20-27s console that meant pressing Research showed a spinner for 163 seconds for a call that
 * enqueues in about one. They were also the two paths with NO timeout, so nothing ever ended
 * the spin. Both are fixed here: a route handler returns JSON only, and the wait is bounded.
 */
async function postBounded(
  url: string,
  body: Record<string, unknown>,
): Promise<{ ok: boolean; data?: any; error?: string }> {
  const stalled = Symbol("stalled");
  try {
    const r = await Promise.race([
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        cache: "no-store",
      }),
      new Promise<typeof stalled>((res) =>
        setTimeout(() => res(stalled), STALL_SECONDS * 1000)),
    ]);
    if (r === stalled) {
      return { ok: false,
               error: `No answer after ${STALL_SECONDS}s. It may still be running — nothing was `
                      + "cancelled. Check the area before trying again." };
    }
    const data = await (r as Response).json().catch(() => ({}));
    if (!(r as Response).ok) return { ok: false, error: data?.error ?? `HTTP ${(r as Response).status}` };
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

/**
 * The workflow every new project in this project follows.
 *
 * "Build →" does not create a project — it sends the directive down panel → PRD → then code.
 * Threading a per-press workflow choice through a meeting and an approval before it reaches
 * the eventual code task would be real plumbing for a choice nobody makes twice. Setting the
 * project's DEFAULT is one control that governs everything that follows, including whatever
 * the panel produces, and it is what workflow mappings are for.
 *
 * Writes through the `apply` action that already exists — the same one the Workflows area
 * uses — so there is one way a mapping gets created, not two that can disagree.
 */
function DefaultWorkflowPicker({ projectId }: { projectId: string }) {
  const [items, setItems] = useState<Array<{
    id: string; name: string; workflow_key: string; is_catalogue?: boolean }>>([]);
  const [current, setCurrent] = useState("");
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/delivery-workflows?view=installed&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (res.ok && alive) setItems((await res.json()).items ?? []);
      } catch { /* the hero still works without it */ }
    })();
    return () => { alive = false; };
  }, [projectId]);

  const apply = async (key: string) => {
    if (!key) return;
    setSaving(true);
    setNote("");
    try {
      const res = await fetch("/api/delivery-workflows", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // `project.new` is the work class the catalogue gives Standard Product Delivery, and
        // the first one a code task resolves against.
        body: JSON.stringify({ action: "apply", project_id: projectId,
                               workflow_key: key, work_class: "project.new" }),
      });
      // SAY WHICH WAY IT WENT. A silent select is indistinguishable from one that did nothing,
      // and this control changes what every future project does.
      setNote(res.ok ? "saved" : `failed (${res.status})`);
      if (res.ok) setCurrent(key);
    } catch {
      setNote("could not save");
    } finally {
      setSaving(false);
    }
  };

  if (!items.length) return null;
  return (
    <div className="askboss-wf" data-testid="askboss-default-workflow">
      {/* Short. The full sentence lives in the tooltip — a label that is longer than the
          control it names is a label competing with the thing it describes. */}
      <label className="askboss-wf-label" htmlFor="askboss-wf-select"
             title="The workflow every new project in this project will follow">Workflow</label>
      <select id="askboss-wf-select" className="askboss-wf-select" value={current}
              disabled={saving} onChange={(e) => void apply(e.target.value)}>
        <option value="">Standard (default)</option>
        {items.map((w) => (
          <option key={w.id} value={w.workflow_key}>
            {w.name}{w.is_catalogue ? " (standard)" : ""}
          </option>
        ))}
      </select>
      {note ? <span className="askboss-wf-note" data-testid="askboss-wf-note">{note}</span> : null}
    </div>
  );
}

export function AskBossHero({
  projectId,
  actor,
  orgId,
}: {
  projectId: string;
  actor: string;
  orgId?: string;
}) {
  const [input, setInput] = useState("");
  const [reply, setReply] = useState("");
  const [proposed, setProposed] = useState<ProposedAction[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [ranAction, setRanAction] = useState<string | null>(null);
  const stripRef = useRef<HTMLDivElement>(null);
  const [busy, startTransition] = useTransition();
  const boxRef = useRef<HTMLTextAreaElement>(null);

  // SURVIVE A RE-RENDER OF THE PAGE UNDERNEATH.
  //
  // Server actions that revalidate ("/") re-render the whole console tree, and this panel's
  // answer went with it -- observed live: the reply arrived and vanished in the same frame.
  // askBossAction no longer revalidates, but runAgentActionAction still must (a button click
  // really does change the board), and any future action might. Rather than audit every call
  // site forever, the panel restores its own last answer. sessionStorage, not local: an answer
  // about live state should not outlive the tab that asked.
  const SESSION_KEY = `boss.ask.${projectId}`;
  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(SESSION_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as {
        reply?: string; proposed?: ProposedAction[]; threadId?: string | null;
      };
      if (saved.reply) setReply(saved.reply);
      if (Array.isArray(saved.proposed)) setProposed(saved.proposed);
      if (saved.threadId) setThreadId(saved.threadId);
    } catch {}
    // Restore once, on mount, for this project.
  }, [SESSION_KEY]);

  useEffect(() => {
    try {
      if (!reply && proposed.length === 0) window.sessionStorage.removeItem(SESSION_KEY);
      else window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({ reply, proposed, threadId }));
    } catch {}
  }, [SESSION_KEY, reply, proposed, threadId]);

  useEffect(() => {
    if (!busy) { setElapsed(0); return; }
    const t0 = Date.now();
    const id = window.setInterval(() => setElapsed(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [busy]);

  const ask = useCallback((text: string) => {
    const message = text.trim();
    if (!message || busy) return;
    setError("");
    setReply("");
    setProposed([]);
    const fd = new FormData();
    fd.set("message", message);
    fd.set("project_id", projectId);
    fd.set("actor", actor);
    if (threadId) fd.set("thread_id", threadId);
    startTransition(async () => {
      // Bound the WAIT, not the work: a turn that outlives STALL is reported, never presented
      // as cancelled, because it may still land server-side.
      const stalled = Symbol("stalled");
      const r = await Promise.race([
        askBossAction(null, fd),
        new Promise<typeof stalled>((res) =>
          window.setTimeout(() => res(stalled), STALL_SECONDS * 1000)),
      ]);
      if (r === stalled) {
        setError(
          `No answer after ${STALL_SECONDS}s. The turn may still be running — nothing was ` +
          "cancelled. Ask again in a moment, or open the full agent panel to see if it landed.",
        );
        return;
      }
      if (!r.ok || !r.data) { setError(r.error ?? "request failed"); return; }
      const d = r.data as Record<string, unknown>;
      if (typeof d.thread_id === "string") setThreadId(d.thread_id);
      setProposed(Array.isArray(d.proposed_actions) ? (d.proposed_actions as ProposedAction[]) : []);
      const msgs = Array.isArray(d.messages) ? (d.messages as Array<Record<string, unknown>>) : [];
      const last = [...msgs].reverse().find((m) => m.role === "assistant");
      setReply(String((d.reply as string) ?? (last?.content as string) ?? ""));
    });
  }, [busy, projectId, actor, threadId]);

  const accept = useCallback((a: ProposedAction) => {
    setRanAction(a.id);
    startTransition(async () => {
      const r = await runAgentActionAction(projectId, actor, a.tool, a.args, threadId ?? undefined);
      const data = r.data as Record<string, unknown> | undefined;
      const ok = r.ok && data?.ok !== false;
      setReply((prev) =>
        `${prev}\n\n${ok ? "✓" : "⚠"} **${a.label}**${ok ? " — done." : ` failed: ${data?.error ?? r.error ?? "unknown error"}`}`);
      // An offer is spent once taken: leaving it on screen invites a second, duplicate run.
      setProposed((p) => p.filter((x) => x.id !== a.id));
      setRanAction(null);
    });
  }, [projectId, actor, threadId]);

  /**
   * Send the text down the GOVERNED path instead of the chat one.
   *
   * This was a second composer sitting directly underneath ("Assign an outcome to your
   * team…"), so the page opened with two prompts side by side and no way to tell which took
   * what. They are one box now: the same words either get answered or get built, and the
   * operator picks which by pressing a different button rather than by choosing a lane first.
   */
  const assign = useCallback((text: string) => {
    const directive = text.trim();
    if (!directive || busy) return;
    setError(""); setReply(""); setProposed([]);
    startTransition(async () => {
      const r = await postBounded("/api/builds",
                                  { project_id: projectId, directive, actor });
      const d = r.data as Record<string, unknown> | undefined;
      if (!r.ok) {
        // Build used to swallow this entirely: the action returned void, so a failure and a
        // success looked identical — "Working", then nothing, forever.
        setError(r.error ?? "Could not start the build.");
        return;
      }
      // SAY WHAT STARTED. This announced a panel, because that is what used to happen; the
      // press now starts the workflow run, and the run is the thing to go and watch.
      const runId = String((d?.run as Record<string, unknown> | undefined)?.id ?? d?.id ?? "");
      setInput("");
      setReply(
        `**Started.** Taking you to the workflow…\n\n` +
        (runId ? `Run \`${runId}\`.` : ""),
      );
      // GO WHERE THE WORK IS. Telling somebody to "open Workflow" after they pressed Build is
      // asking them to do the navigation the press already implied — and they had no way to
      // know the run had appeared, because the page they were on does not show runs.
      const url = new URL(window.location.href);
      url.searchParams.set("area", "delivery_workflows");
      if (runId) url.searchParams.set("wfr", runId);
      window.history.pushState({}, "", url.toString());
      window.dispatchEvent(new PopStateEvent("popstate"));
      window.dispatchEvent(new CustomEvent("boss:navigate"));
    });
  }, [busy, projectId, actor]);

  /**
   * Put a brief in the composer. Deliberately does NOT build.
   *
   * Clicking a suggestion used to fire a governed build immediately — a click that spends money
   * on a one-line spec nobody had read. It drafts now: the operator edits the brief, then
   * presses Build when they mean it.
   */
  const draftIdea = useCallback((idea: Idea) => {
    setInput(idea.brief);
    setError(""); setReply(""); setProposed([]);
    // Let the textarea size to the brief before the caret lands at the end.
    window.requestAnimationFrame(() => {
      const box = boxRef.current;
      if (!box) return;
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
      box.style.height = "auto";
      box.style.height = `${Math.min(box.scrollHeight, 320)}px`;
    });
  }, []);

  // The nine-second auto-rotate is gone: a panel that changes while you are reading it is
  // worse than one that does not, and scrolling shows the whole set without waiting for it.
  /**
   * Start a Discovery run: research first, decide what to build after.
   *
   * Restored after a careless edit removed it along with the idea-paging state it happened to
   * sit next to — the build caught it, which is the argument for typechecking before deploying.
   */
  const research = useCallback((seedText: string) => {
    const seed = seedText.trim();
    if (!seed || busy) return;
    setError(""); setReply(""); setProposed([]);
    startTransition(async () => {
      const r = await postBounded("/api/discoveries", { project_id: projectId, seed, actor });
      if (!r.ok) { setError(r.error ?? "Could not start the research run."); return; }
      setInput("");
      setReply(
        `**Researching.** Discovery run \`${r.data?.run_id ?? ""}\` is gathering sources.\n\n` +
        `It produces a cited dossier and candidate outcomes — watch it under **Discovery**. ` +
        `When something looks worth building, bring it back here and press Build.`,
      );
    });
  }, [busy, projectId, actor]);

  // A SLOW DRIFT THE OPERATOR CAN OVERRIDE.
  //
  // Manual scrolling alone means the cards sit still and most people never learn there are
  // sixteen. The strip drifts continuously instead — slowly enough to read across, not in
  // page-sized jumps, so nothing you are looking at is ever yanked away.
  //
  // It stops the moment attention lands on it: pointer over the strip OR the composer, focus
  // inside either, a turn running, an answer on screen, or any text typed. Motion that
  // competes with what you are doing is worse than no motion.
  const [stripHeld, setStripHeld] = useState(false);
  // Defaults to OFF for anyone who asked the OS for less motion, but it is a VISIBLE toggle
  // rather than a silent gate: drifting was requested, and a requested feature that quietly
  // does nothing because of a system setting is indistinguishable from a bug.
  const [drift, setDrift] = useState<boolean | null>(null);
  useEffect(() => {
    if (drift !== null) return;
    setDrift(!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  }, [drift]);
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    if (stripHeld || drift === false || busy || reply || input.trim()) return;
    let raf = 0;
    let last = 0;
    // ACCUMULATE THE SUB-PIXEL REMAINDER.
    //
    // This is why the drift did nothing at all. 14px/s is about 0.23px per frame at 60fps, and
    // scrollLeft is rounded to whole pixels — so `scrollLeft = scrollLeft + 0.23` rounded
    // straight back to the same integer, every frame, forever. The motion has to be banked
    // until it is worth at least one pixel and then applied.
    let carry = 0;
    const PX_PER_SECOND = 16;          // about one card every fifteen seconds
    const step = (t: number) => {
      if (last) {
        const end = strip.scrollWidth - strip.clientWidth;
        if (end > 0) {
          carry += ((t - last) / 1000) * PX_PER_SECOND;
          const whole = Math.floor(carry);
          if (whole >= 1) {
            carry -= whole;
            // Wrap rather than stop: a strip parked at the end looks broken.
            const next = strip.scrollLeft + whole;
            strip.scrollLeft = next >= end ? 0 : next;
          }
        }
      }
      last = t;
      raf = window.requestAnimationFrame(step);
    };
    raf = window.requestAnimationFrame(step);
    return () => window.cancelAnimationFrame(raf);
  }, [stripHeld, drift, busy, reply, input]);

  /**
   * Step the strip by one card.
   *
   * The arrows used scrollBy with behavior:"smooth" while the strip had CSS scroll-snap on it.
   * The two fight: snap pulls the position back mid-animation, so a click appeared to do
   * nothing. Snap is gone from the CSS and this measures a real card instead of a magic 320px.
   */
  const nudge = useCallback((dir: 1 | -1) => {
    const strip = stripRef.current;
    if (!strip) return;
    const card = strip.querySelector<HTMLElement>(".askboss-idea");
    const step = (card?.offsetWidth ?? 240) + 10;
    strip.scrollLeft += dir * step;
  }, []);

  const slow = elapsed >= NORMAL_SECONDS;
  const state = busy ? (slow ? "slow" : "thinking") : reply || error ? "answered" : "idle";

  return (
    <section className="askboss" data-state={state} aria-label="Ask BOSS">
      <div className="askboss-aura" aria-hidden />
      <div className="askboss-inner">
        {/* The console's standard header shape — eyebrow, title, status pills on the right,
            the same one the Meeting room uses. This was a bespoke card with its own type
            scale, which is why it read as a different product bolted onto the page. */}
        <header className="mr-header askboss-head">
          <div className="mr-header-main">
            <span className="eyebrow">BOSS Agent</span>
            <h3 className="mr-title">Your autonomous software factory.</h3>
            {/* The first line on the first page described PLUMBING — "reads live control-plane
                state and acts with your approval" — which tells a visitor how it is wired, not
                what it is for.
                
                It then said "without the meetings", which sold the wrong thing: the governed
                panel IS the product. Agents convene it and it produces the PRD, so naming it as
                friction to avoid undersold the part that makes the output trustworthy. */}
            <p className="askboss-sub">
              Describe an outcome. BOSS agents research it, debate it in a governed panel,
              deliver the PRD, build it and ship it —{" "}
              <strong>with your approval at every gate.</strong>
            </p>
          </div>
          <div className="mr-header-meta">
            <span className={`cap-badge tone-${busy ? "amber" : "green"}`}>
              {busy ? (slow ? "working · slow" : "working") : "ready"}
            </span>
            <span className="mr-meta-item">reads live state</span>
            <span className="mr-meta-item">acts on approval</span>
            {busy ? <span className="mr-meta-item">⏱ {elapsed}s</span> : null}
          </div>
        </header>

        <form
          className="askboss-form"
          onSubmit={(e) => { e.preventDefault(); ask(input); }}
        >
          <div
            className="askboss-box"
            onMouseEnter={() => setStripHeld(true)}
            onMouseLeave={() => setStripHeld(false)}
          >
            <textarea
              ref={boxRef}
              className="askboss-input"
              value={input}
              rows={1}
              disabled={busy}
              placeholder="Ask a question, or describe something to build…"
              onChange={(e) => {
                setInput(e.target.value);
                // A brief is many lines; a fixed one-row box hid all but the first.
                e.target.style.height = "auto";
                e.target.style.height = `${Math.min(e.target.scrollHeight, 320)}px`;
              }}
              // Enter sends; Shift+Enter is a newline. A chat box that needs a mouse to send
              // is not a chat box.
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input); }
              }}
              data-testid="askboss-input"
            />
            <div className="askboss-actions-inline">
              <button
                className="askboss-send"
                type="submit"
                disabled={busy || !input.trim()}
                data-testid="askboss-send"
                title="Answer this from live control-plane state"
              >
                {busy ? <span className="askboss-spinner" aria-hidden /> : "Ask"}
              </button>
              <button
                className="askboss-assign"
                type="button"
                disabled={busy || !input.trim()}
                data-testid="askboss-research"
                title="Research this first — web sources, cited dossier, candidate outcomes"
                onClick={() => research(input)}
              >
                Research
              </button>
              <button
                className="askboss-assign"
                type="button"
                disabled={busy || !input.trim()}
                data-testid="askboss-assign"
                title="Send this down the governed path — panel, PRD, then code"
                onClick={() => assign(input)}
              >
                Build →
              </button>
            </div>
            <DefaultWorkflowPicker projectId={projectId} />
          </div>

          {!reply && !busy ? (
            <div className="askboss-openers">
              {OPENERS.map((o) => (
                <button
                  key={o.text}
                  type="button"
                  className="askboss-opener"
                  onClick={() => { setInput(o.text); ask(o.text); }}
                >
                  <span className={`mr-avatar tone-${o.tone}`} aria-hidden>{o.badge}</span>
                  {o.text}
                </button>
              ))}
            </div>
          ) : null}

          {!reply && !busy ? (
            <div className="askboss-ideas">
              {/* COMPACT ON PURPOSE. These were four full-width sentences, which is a lot of
                  height for a secondary path and pushed the composer — the thing that should
                  read first — down the panel. The chip carries a short label; the full question
                  goes into the composer on click, where it can be edited before it runs. */}
              <div className="askboss-research-row">
                <span className="askboss-research-label">Not sure yet?</span>
                {RESEARCH.map((r) => (
                  <button
                    key={r.seed}
                    type="button"
                    className="askboss-seed"
                    title={r.seed}
                    onClick={() => {
                      setInput(r.seed);
                      window.requestAnimationFrame(() => {
                        const box = boxRef.current;
                        if (!box) return;
                        box.focus();
                        box.setSelectionRange(box.value.length, box.value.length);
                        box.style.height = "auto";
                        box.style.height = `${Math.min(box.scrollHeight, 320)}px`;
                      });
                    }}
                  >
                    {r.label}
                  </button>
                ))}
                <span className="askboss-research-hint">→ then press Research</span>
              </div>
              {/* A SCROLL STRIP, NOT A SLIDESHOW.
                  These flipped pages on a nine-second timer, which means the one you were
                  reading could leave while you read it, and seeing the whole set required
                  waiting. All sixteen sit in one horizontal strip now: scroll or drag at
                  whatever pace you want, nothing moves on its own, and the arrows are there
                  for a trackpad that will not scroll sideways. */}
              <div className="askboss-ideas-head">
                <span className="askboss-ideas-label">Or start something</span>
                <div className="askboss-ideas-nav">
                  <button
                    type="button"
                    className="askboss-arrow"
                    aria-label={drift ? "Pause auto-scroll" : "Resume auto-scroll"}
                    title={drift ? "Pause auto-scroll" : "Resume auto-scroll"}
                    onClick={() => setDrift((d) => !d)}
                  >{drift ? "❙❙" : "▶"}</button>
                  <button type="button" className="askboss-arrow" aria-label="Scroll left"
                          onClick={() => nudge(-1)}>←</button>
                  <button type="button" className="askboss-arrow" aria-label="Scroll right"
                          onClick={() => nudge(1)}>→</button>
                </div>
              </div>
              <div
                className="askboss-idea-strip"
                ref={stripRef}
                onMouseEnter={() => setStripHeld(true)}
                onMouseLeave={() => setStripHeld(false)}
                onFocusCapture={() => setStripHeld(true)}
                onBlurCapture={() => setStripHeld(false)}
              >
                {IDEAS.map((i) => (
                  <button
                    key={i.title}
                    type="button"
                    className="askboss-idea"
                    style={{ ["--idea-hue" as string]: String(i.hue) }}
                    onClick={() => draftIdea(i)}
                    data-testid={`askboss-idea-${i.tag.toLowerCase()}`}
                  >
                    {/* A LOOK, NOT JUST A LABEL. Sixteen identical grey cards is a list
                        pretending to be a gallery; colour and a mark make them scannable. */}
                    <span className="askboss-idea-art" aria-hidden
                          style={{ ["--idea-hue" as string]: String(i.hue) }}>
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                           strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                        <path d={GLYPHS[i.glyph]} />
                      </svg>
                    </span>
                    <span className="askboss-idea-tag">{i.tag}</span>
                    <span className="askboss-idea-title">{i.title}</span>
                    {/* The first brief line just restates the title ("PR review-time tracker"
                        / "a pull-request review-time tracker"). WHO it is for is the line that
                        actually tells them apart. */}
                    <span className="askboss-idea-hint">
                      {(i.brief.split("Who it is for:")[1] ?? "").split("\n")[0].trim()
                        || i.brief.split("\n")[0].replace(/^Build /, "")}
                    </span>
                    {/* A small overlay, not an expansion. Rendering this inline grew the card
                        to about three times its height and shoved the row around, which is
                        worse than showing nothing. It floats above the card now, so the strip
                        never changes size, and it is capped to two lines. */}
                    <span className="askboss-idea-peek">
                      {(i.brief
                        .split("What it must do:")[1]?.split("Done when:")[0]
                        ?.split("\n").filter((l) => l.trim().startsWith("-"))
                        .slice(0, 2).map((l) => l.replace(/^\s*-\s*/, "").trim())
                        .join(" · ") ?? "").slice(0, 150)}
                    </span>
                  </button>
                ))}
              </div>
              <p className="askboss-ideas-foot">
                Clicking loads the full brief so you can edit it first — nothing builds until
                you press Build.
              </p>
            </div>
          ) : null}
        </form>

        {busy ? (
          <p className="askboss-status" data-slow={slow} aria-live="polite" data-testid="askboss-status">
            <span className="askboss-status-dot" aria-hidden />
            {slow
              ? "Still working — this is running long. It has not been dropped."
              : "Reading the system and choosing tools…"}
            <span className="askboss-elapsed">
              {elapsed}s{slow ? ` / ~${STALL_SECONDS}s` : ""}
            </span>
          </p>
        ) : null}

        {error ? (
          <p className="askboss-error" data-testid="askboss-error">⚠ {error}</p>
        ) : null}

        {reply ? (
          <div className="askboss-answer" data-testid="askboss-answer">
            <Markdown text={reply} />
          </div>
        ) : null}

        {proposed.length > 0 ? (
          <div className="askboss-actions" data-testid="askboss-actions">
            <p className="askboss-actions-head">BOSS can do this for you — nothing runs until you choose:</p>
            <div className="askboss-actions-row">
              {proposed.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="button agent-proposal-btn"
                  disabled={busy}
                  data-running={ranAction === a.id}
                  title={a.why}
                  onClick={() => accept(a)}
                  data-testid={`askboss-action-${a.tool}`}
                >
                  {ranAction === a.id ? "…" : a.label}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {reply || error ? (
          <div className="askboss-foot">
            <button type="button" className="askboss-reset" onClick={() => {
              setReply(""); setProposed([]); setError(""); setInput("");
              boxRef.current?.focus();
            }}>
              Ask something else
            </button>
            {/* `org`, NOT `org_id`. The page reads ?org=; a link carrying org_id drops the
                org, the picker appears mid-session, and the stored-org effect bounces you back
                a second later. That flicker was this link. */}
            <a className="askboss-reset"
               href={`/?${orgId ? `org=${encodeURIComponent(orgId)}&` : ""}area=agent`}>
              Open the full agent panel →
            </a>
          </div>
        ) : null}
      </div>
    </section>
  );
}
