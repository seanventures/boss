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
import { askBossAction, runAgentActionAction, submitCommandAction } from "./actions";

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
type Idea = { tag: string; title: string; brief: string };

const IDEAS: Idea[] = [
  {
    tag: "DevTools",
    title: "Flaky-test detector",
    brief: `Build a flaky-test detector for CI.

Who it is for: a team whose test suite fails intermittently and who currently re-run jobs by hand.

What it must do:
- Ingest JUnit XML from CI runs and store each test's pass/fail history per commit.
- Flag a test as flaky when it both passed and failed on the SAME commit SHA.
- Show a ranked list: flakiest first, with failure rate and last-seen date.
- Let a user mute a known-flaky test and record who muted it and why.

Done when: seeding two runs of the same commit with opposite results produces exactly one flagged test, and muting it removes it from the default list but keeps it in history.`,
  },
  {
    tag: "Ops",
    title: "On-call rota with escalation",
    brief: `Build an on-call rota tool.

Who it is for: a small team that currently tracks who is on call in a spreadsheet.

What it must do:
- Define rotations (weekly/daily) over a list of people, with timezone-aware handover.
- Show who is on call right now and who is next, with a shareable read-only page.
- Support overrides for holidays without breaking the underlying rotation.
- Escalate: if a page is unacknowledged for N minutes, notify the next person.

Done when: a two-person weekly rotation shows the correct person across a handover boundary, and an unacknowledged page escalates exactly once.`,
  },
  {
    tag: "Data",
    title: "CSV-to-chart explorer",
    brief: `Build a CSV-to-chart explorer.

Who it is for: someone who receives CSV exports and wants a chart without opening a spreadsheet.

What it must do:
- Upload a CSV, infer column types (number, date, category), and show a preview.
- Pick x and y and a chart type; render it; handle 100k rows without freezing the page.
- Save a named view (file + columns + chart type) and reopen it later.
- Export the chart as PNG.

Done when: a 100k-row file renders in under two seconds and a saved view reopens with the same chart.`,
  },
  {
    tag: "Agents",
    title: "Weekly changelog writer",
    brief: `Build a changelog writer that summarises merged pull requests.

Who it is for: a maintainer who writes release notes by hand every week.

What it must do:
- Pull PRs merged in a date range from a GitHub repo.
- Group them by label (feature / fix / chore) and drop anything labelled "no-changelog".
- Write one plain sentence per PR, linking the PR number and crediting the author.
- Output markdown ready to paste into a release.

Done when: a week with mixed labels produces grouped markdown, and excluded PRs are absent.`,
  },
  {
    tag: "SaaS",
    title: "Link shortener with analytics",
    brief: `Build a link shortener with click analytics.

Who it is for: someone sharing links across channels who wants to know which channel worked.

What it must do:
- Create a short code for a URL; redirect with a 301 and record the click.
- Record referrer, coarse device type and country; never store raw IP addresses.
- Dashboard: clicks over time per link, and a breakdown by referrer.
- Support a custom alias, and reject one that is already taken.

Done when: two links show independent counts, a duplicate alias is rejected with a clear error, and no raw IP is persisted anywhere.`,
  },
  {
    tag: "Vertical",
    title: "Invoice chaser",
    brief: `Build an invoice chaser for a small consultancy.

Who it is for: a freelancer who forgets to chase unpaid invoices.

What it must do:
- Record invoices: client, amount, issue date, due date, paid/unpaid.
- Show an ageing view: due soon, overdue by 1-30 days, overdue by 30+.
- Draft a polite reminder email whose tone escalates with how overdue it is.
- Never send anything automatically — the user reviews and sends.

Done when: an invoice 45 days overdue appears in the right bucket with the firmest of the three drafts, and nothing is sent without an explicit click.`,
  },
  {
    tag: "Internal",
    title: "Access review helper",
    brief: `Build an access review helper.

Who it is for: a team that has to certify quarterly who has access to what.

What it must do:
- Import a list of users, systems and roles from CSV.
- Generate a review round: each owner sees only their systems and approves or revokes per user.
- Track progress and produce an export of every decision with a timestamp and reviewer.
- Flag users with access to a system who have not logged in for 90 days.

Done when: a round with two owners shows each only their own systems, and the export contains one row per decision.`,
  },
  {
    tag: "DevTools",
    title: "Env-var drift checker",
    brief: `Build an environment variable drift checker.

Who it is for: a team whose staging and production configs silently diverge.

What it must do:
- Read a .env.example as the schema and compare against each environment's actual key list.
- Report three things separately: missing keys, extra keys, and keys present but empty.
- Never read or display a value — key names only.
- Exit non-zero in CI when a required key is missing.

Done when: an environment missing one required key fails CI naming exactly that key, and no value is ever printed.`,
  },
];

const IDEAS_PER_PAGE = 4;

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
  const [ideaPage, setIdeaPage] = useState(0);
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
    const command = text.trim();
    if (!command || busy) return;
    const fd = new FormData();
    fd.set("command", command);
    fd.set("project_id", projectId);
    fd.set("actor", actor);
    setError(""); setReply(""); setProposed([]);
    startTransition(async () => { await submitCommandAction(fd); });
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

  const pageCount = Math.ceil(IDEAS.length / IDEAS_PER_PAGE);
  const shownIdeas = IDEAS.slice(ideaPage * IDEAS_PER_PAGE, ideaPage * IDEAS_PER_PAGE + IDEAS_PER_PAGE);

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
            <h3 className="mr-title">Ask BOSS</h3>
            <p className="askboss-sub">
              Ask anything about your work — or tell BOSS to do it. It reads live
              control-plane state and acts with your approval.
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
          <div className="askboss-box">
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
                data-testid="askboss-assign"
                title="Send this down the governed path — panel, PRD, then code"
                onClick={() => assign(input)}
              >
                Build →
              </button>
            </div>
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
              <div className="askboss-ideas-head">
                <span className="askboss-ideas-label">Or start something</span>
                <div className="askboss-ideas-nav">
                  <span className="askboss-ideas-count">
                    {ideaPage + 1} / {pageCount}
                  </span>
                  <button
                    type="button"
                    className="askboss-next"
                    onClick={() => setIdeaPage((n) => (n + 1) % pageCount)}
                    data-testid="askboss-ideas-next"
                  >
                    Next →
                  </button>
                </div>
              </div>
              <div className="askboss-idea-grid">
                {shownIdeas.map((i) => (
                  <button
                    key={i.title}
                    type="button"
                    className="askboss-idea"
                    onClick={() => draftIdea(i)}
                    title="Loads the full brief into the box — nothing starts until you press Build"
                    data-testid={`askboss-idea-${i.tag.toLowerCase()}`}
                  >
                    <span className="askboss-idea-tag">{i.tag}</span>
                    <span className="askboss-idea-title">{i.title}</span>
                    <span className="askboss-idea-hint">
                      {i.brief.split("\n")[0].replace(/^Build /, "")}
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
