"use client";

/**
 * PANELS, agent meetings that debate to a decision or a PRD (area `meetings`).
 *
 * Not a sidebar page: the usual way in is a workflow run's panel step, whose step panel shows
 * `MeetingCompact` and links here for the full room. A dossier's "Convene a panel", the
 * Factory's PRD links, Approvals and old links land here too, by `meeting=`, `run=`, `task=`
 * or `#id`. The look is the approved mockup (boss-research-panels-mockup: panels-home,
 * meeting); styles in research/research.css.
 *
 *   Panels home , convene hero (type, directive, website, task, the seats around the table),
 *                  recent panel cards, Manage seats, Maintenance (vote, sweep).
 *   Meeting room, hero with the stage stepper and the outcome card; the rounds as speaker
 *                  cards (each seat's gist, "Read all" for the full reply); the facilitator's
 *                  synthesis beside the PRD's sections (or the verdict / the actions for a
 *                  go/no-go or a stand-up); a drawer with the vote, what was asked and the
 *                  context BOSS added, the linked task, status and cost, and the timeline.
 *
 * NEVER THE PROMPT. Titles, heroes, cards and the drawer read the person's own words through
 * humanBrief (lib/research-panels), the scaffolding BOSS wraps around a panel's prompt is
 * shown only as the readable "Context BOSS added".
 */
import { changeHref, linkTarget } from "../lib/repo-links";
import React, { useActionState, useCallback, useEffect, useMemo, useState } from "react";
import { RateIt } from "./improve/rate-it";

import type { Meeting, MeetingRole } from "../lib/meeting-rooms";
import { Markdown } from "./markdown";
import { PrdRecordView, PrdRepoLink } from "./prd-record";
import { parsePrdRepo } from "../lib/prd-record";
import { meetingCardTitle } from "../lib/meeting-title";
import {
  conveneMeetingAction,
  upsertMeetingRoleAction,
  setMeetingRoleEnabledAction,
  deleteMeetingRoleAction,
  runSimulatedMeetingAction,
  loadMeetingEvidenceAction,
  tallyVotesAction,
  sweepStalledMeetingsAction,
} from "./actions";
import { MeetingConsensusCard } from "./phase3-ui";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";
import { consoleHref } from "./url-intent";
import { Ago, BossMark, CopyId, How, Stepper, Verdict, clip, pushParams, useSearch } from "./research/bits";

import { callAction } from "../lib/call-action";
import type { AvatarState } from "../lib/agent-world-types";
import { agentIdentity, seatAvatar } from "../lib/agent-identity";
import { useIdentityVersion } from "../lib/use-agent-identities";
import { fetchMeetingForTask, fetchMeetingPage, fetchMeetingRoom, sayLoadError } from "../lib/meeting-room-detail";
import {
  ago, briefLine, composerHref, decisionHref, dissentLine, humanBrief, voteSentence, kindOf, meetingFromUrl, meetingLive,
  meetingOutcome, orderedSeats, panelDoing, panelStages, prdDigest, prdSections, repoOf, roundNames,
  seatGist, seatsOf, shortTitle, took, usd, SEAT_ORDER, type StageState,
} from "../lib/research-panels";
import { AgentAvatar } from "./agent-avatar";
import { ReviewChecklist } from "./factory/security/review-checklist";
import { useOnScreen } from "../lib/area-shown";

export const ROLE_ORDER = SEAT_ORDER;

const PHASE_LABEL: Record<string, string> = {
  research: "Researching the site",
  round1: "Round 1, opening views",
  round2: "Round 2, debate",
  synthesis: "Synthesis",
  draft: "Drafting the PRD",
  finalize: "Finalizing",
  done: "Done",
};

const TYPE_LABEL: Record<string, string> = { prd_panel: "PRD panel", go_no_go: "Go / no-go", standup: "Stand-up" };

/** A status in words: the drawer never shows a raw status code. */
const STATUS_WORDS: Record<string, string> = {
  running: "In session", queued: "Waiting to start", completed: "Finished", simulated: "Finished (no-spend fixture)",
  failed: "Stopped", cancelled: "Cancelled",
};

/** A seat's face. The seat title resolves to its agent through the one resolver, so a panel's
 *  "QA / Test" is the same face as `qa` on a workflow step. Seats that share an agent (Growth and
 *  Data are both the PM) get their own emblem and visor, so no two seats look alike. The
 *  facilitator is BOSS itself. */
export function SeatAvatar({ role, size = 26, state }: { role: string; size?: number; state?: AvatarState }) {
  useIdentityVersion(); // the seat face is worked out here, so repaint when identities load
  if (/facilitator/i.test(role)) {
    return <AgentAvatar variant="boss" size={size} state={state} className="mr-avatar ag-face" />;
  }
  return <AgentAvatar agent={role} spec={seatAvatar(role)} size={size} state={state} aiBadge={false} className="mr-avatar ag-face" />;
}

/** The agent's callsign for a seat ("Juno" for Product Manager), or "" when it has none. */
function callsignOf(seat: string): string {
  return agentIdentity(seat).callsign ?? "";
}

/** The person's words for a meeting (never the prompt), and its title. */
function briefOf(m: Pick<Meeting, "directive" | "brief" | "title">): string {
  return humanBrief(m.directive ?? meetingCardTitle(m.title), m.brief);
}
function titleOf(m: Pick<Meeting, "directive" | "brief" | "title">): string {
  return shortTitle(meetingCardTitle(m.title));
}

/**
 * A meeting, kept fresh: the list's copy when it has one, else read by id (or, for `task:<id>`,
 * the panel held for that task). Re-read every 4s ONLY while the panel is in session; a meeting
 * that is not there is said once and not asked for again.
 */
function useMeeting(id: string, projectId: string, listed: Meeting | null): { meeting: Meeting | null; error: string | null } {
  const [loaded, setLoaded] = useState<Meeting | null>(null);
  const [error, setError] = useState<string | null>(null);
  const byTask = id.startsWith("task:");
  const mine = loaded && (byTask || loaded.id === id) ? loaded : null;
  const current = mine && listed && !meetingLive(listed) && meetingLive(mine) ? listed : mine ?? listed;
  const live = current ? meetingLive(current) : false;
  const have = Boolean(current);
  const onScreen = useOnScreen();   // off screen, nothing is asked (lib/area-shown.ts)
  useEffect(() => {
    let cancelled = false;
    const load = () => (byTask ? fetchMeetingForTask(id.slice(5), projectId) : fetchMeetingRoom(id, projectId))
      .then((res) => {
        if (cancelled) return;
        if (res.ok) { setLoaded(res.meeting); setError(null); } else setError(res.error);
      });
    if (!have) void load();
    if (!live || !onScreen) return () => { cancelled = true; };
    const t = setInterval(() => { void load(); }, 4000);
    return () => { cancelled = true; clearInterval(t); };
  }, [id, projectId, live, have, byTask, onScreen]);
  return { meeting: current ?? null, error: current ? null : error };
}

// ── Speaker cards ──────────────────────────────────────────────────────────────────────────

function TurnCard({ meeting, round, seat, live }: { meeting: Meeting; round: 1 | 2; seat: string; live: boolean }) {
  useIdentityVersion();
  const [open, setOpen] = useState(false);
  const text = (round === 1 ? meeting.round1 : meeting.round2)?.[seat];
  const gist = seatGist(meeting, round, seat);
  const name = callsignOf(seat) || seat;
  if (!text) {
    return (
      <article className="rp-turn" data-pending="true" data-testid="meeting-turn-pending">
        <div className="rp-t-h">
          <SeatAvatar role={seat} size={36} state={live ? "thinking" : "idle"} />
          <div><b>{name}</b><span>{seat}</span></div>
        </div>
        <p className="rp-thinking">{live ? "Thinking…" : "Did not speak this round"}</p>
      </article>
    );
  }
  return (
    <article className="rp-turn" data-open={open ? "true" : undefined} data-testid="meeting-turn">
      <div className="rp-t-h">
        <SeatAvatar role={seat} size={36} state="done" />
        <div><b>{name}</b><span>{seat}</span></div>
      </div>
      {gist?.headline ? <p className="rp-t-head">{gist.headline}</p> : null}
      {gist?.points.length ? <ul>{gist.points.map((p, i) => <li key={i}>{p}</li>)}</ul> : null}
      <button type="button" className="rp-link sm" aria-expanded={open} onClick={() => setOpen((x) => !x)}
              style={{ alignSelf: "flex-start" }}>
        {open ? "Show less ↑" : "Read all ↓"}
      </button>
      {open ? <div className="rp-full"><Markdown text={text} /></div> : null}
    </article>
  );
}

function Round({ meeting, round, seats, live }: { meeting: Meeting; round: 1 | 2; seats: string[]; live: boolean }) {
  const have = round === 1 ? meeting.round1 : meeting.round2;
  const started = Object.keys(have ?? {}).length > 0;
  const now = live && ((round === 1 && ["research", "round1"].includes(meeting.phase ?? ""))
                       || (round === 2 && meeting.phase === "round2"));
  if (!started && !now) return null;
  const names = roundNames(meeting);
  const [label, sub] = round === 1 ? [names[0], names[1]] : [names[2], names[3]];
  return (
    <section className="rp-round" aria-label={label} data-testid={`meeting-round-${round}`}>
      <div className="rp-r-h">
        <span className="rp-r-n" aria-hidden>{round}</span>
        <b>{label}</b>
        <span className="rp-sub">{sub}</span>
      </div>
      <div className="rp-turns" data-n={Math.min(seats.length, 4)}>
        {seats.map((s) => <TurnCard key={s} meeting={meeting} round={round} seat={s} live={now} />)}
      </div>
    </section>
  );
}

/** What a go/no-go decided and who disagreed, or what a stand-up committed to. */
function TypedResult({ meeting }: { meeting: Meeting }) {
  const k = kindOf(meeting);
  if (k === "prd_panel") return null;
  const dissent = meeting.dissent ?? [];
  const actions = meeting.action_items ?? [];
  return (
    <div className="rp-prd" data-testid="meeting-typed-result">
      {k === "go_no_go" ? (
        <div className="rp-verdict-big" data-verdict={meeting.verdict ?? "none"} data-testid="meeting-verdict">
          {meeting.verdict === "go" ? "GO" : meeting.verdict === "no_go" ? "NO-GO" : "No verdict"}
        </div>
      ) : null}
      {dissent.length ? (
        <>
          <small className="rp-cap">Dissent</small>
          <ul className="rp-dis">{dissent.map((d, i) => (
            <li key={i}><b>{d.role}</b>{d.position ? ` (${d.position})` : ""}: {d.reason}</li>
          ))}</ul>
        </>
      ) : k === "go_no_go" ? <span className="rp-sub">No dissent recorded.</span> : null}
      {actions.length ? (
        <>
          <small className="rp-cap">{k === "standup" ? "Commitments" : "Follow-ups"}</small>
          <ul className="rp-dis">{actions.map((a, i) => <li key={i}>{a.title}</li>)}</ul>
        </>
      ) : null}
    </div>
  );
}

function Synthesis({ meeting }: { meeting: Meeting }) {
  const [open, setOpen] = useState(false);
  const [prdOpen, setPrdOpen] = useState(false);
  const typed = kindOf(meeting) !== "prd_panel";
  const sections = prdSections(meeting.prd_markdown);
  const gist = meeting.synthesis_gist ?? null;
  if (!meeting.synthesis && !meeting.prd_markdown) return null;
  return (
    <div className="rp-synth-row">
      {meeting.synthesis ? (
        <div className="rp-synth" data-testid="meeting-synthesis">
          <div className="rp-t-h">
            <BossMark size={36} />
            <div><b>Facilitator</b><span>{typed ? "Calls it" : "Reconciles the panel"}</span></div>
          </div>
          {gist?.headline ? <p className="rp-t-head">{gist.headline}</p> : null}
          {gist?.points.length ? <ul>{gist.points.map((p, i) => <li key={i}>{p}</li>)}</ul> : null}
          <button type="button" className="rp-link sm" aria-expanded={open} onClick={() => setOpen((x) => !x)}
                  style={{ alignSelf: "flex-start" }}>
            {open ? "Show less ↑" : "Read all ↓"}
          </button>
          {open || !gist?.headline ? <div className="rp-full"><Markdown text={meeting.synthesis} /></div> : null}
        </div>
      ) : <div />}
      {meeting.security_review?.run_id ? (
        <div className="rp-prd" data-testid="meeting-security-checklist">
          <div className="rp-card-h"><b title="The panel's recommendations: tick what to fix">Recommendations</b></div>
          <ReviewChecklist runId={meeting.security_review.run_id} />
        </div>
      ) : typed ? <TypedResult meeting={meeting} /> : meeting.prd_markdown ? (
        <div className="rp-prd" data-testid="meeting-prd">
          <div className="rp-card-h">
            <b>The PRD</b><span className="rp-spacer" />
            {meeting.pull_request_url ? (
              <a className="rp-link" href={changeHref(meeting.pull_request_url)} {...linkTarget(meeting.pull_request_url)}>
                PR #{meeting.pull_request_number ?? ""} 
              </a>
            ) : null}
          </div>
          {sections.length ? <ol>{sections.map((h) => <li key={h}>{h}</li>)}</ol> : null}
          <button type="button" className="rp-link sm" aria-expanded={prdOpen} onClick={() => setPrdOpen((x) => !x)}
                  style={{ alignSelf: "flex-start" }}>
            {prdOpen ? "Hide the PRD ↑" : "Read the PRD ↓"}
          </button>
        </div>
      ) : null}
      {/* THE PRD READS AT THE PAGE'S WIDTH. Opened inside the right-hand column it was a long
          document in a narrow strip beside empty space; it opens as its own full-width row. */}
      {!typed && !meeting.security_review?.run_id && meeting.prd_markdown && prdOpen ? (
        <div className="rp-prd rp-prd-wide" data-testid="meeting-prd-full">
          <div className="rp-card-h">
            <b>PRD</b><span className="rp-spacer" />
            {/* Where it lives in the repository (BOSSAPP-929): the build commits it with its PR. */}
            {parsePrdRepo((meeting as { prd_repo?: unknown }).prd_repo) ? (
              <PrdRepoLink repo={parsePrdRepo((meeting as { prd_repo?: unknown }).prd_repo)!} testid="meeting-prd-repo" />
            ) : null}
            <button type="button" className="rp-link sm" onClick={() => setPrdOpen(false)}>Hide the PRD ↑</button>
          </div>
          <div className="rp-full"><Markdown text={meeting.prd_markdown} /></div>
        </div>
      ) : null}
    </div>
  );
}

// ── The outcome card ───────────────────────────────────────────────────────────────────────

/** "Build it": the composer, prefilled from the AGREED PRD, never the raw directive. */
function buildHref(meeting: Meeting, search: string): string {
  const title = titleOf(meeting);
  return composerHref(search, {
    title,
    brief: meeting.prd_markdown ? prdDigest(meeting.prd_markdown, title, meeting.pull_request_url) : briefOf(meeting),
  });
}

/** Convene the same panel again: a failed one's way forward. */
function ConveneAgain({ meeting, projectId }: { meeting: Meeting; projectId: string }) {
  return (
    <ActionForm permission="meeting:convene" action={conveneMeetingAction} successMessage="Convened again, the new panel is in session."
                onResult={(r) => {
                  // Into the new panel (or the one already in session on this ask).
                  const id = r.ok ? (r.data?.run_id as string | undefined) : undefined;
                  if (id && id !== meeting.id) {
                    pushParams({ meeting: id, run: null, task: null });
                    window.dispatchEvent(new PopStateEvent("popstate"));
                  }
                }}>
      <input type="hidden" name="project_id" value={projectId} />
      <input type="hidden" name="directive" value={meeting.directive ?? briefOf(meeting)} />
      <input type="hidden" name="meeting_type" value={kindOf(meeting)} />
      {meeting.url ? <input type="hidden" name="url" value={meeting.url} /> : null}
      {meeting.task_id ? <input type="hidden" name="task_id" value={meeting.task_id} /> : null}
      {(meeting.role_keys ?? []).map((r) => <input key={r} type="hidden" name="roles" value={r} />)}
      <SubmitButton type="submit" className="rp-btn primary" pendingLabel="Convening…" data-testid="meeting-convene-again">
        Convene again
      </SubmitButton>
    </ActionForm>
  );
}

export function OutcomeCard({ meeting, search, projectId, inWorkflow = false }: {
  meeting: Meeting; search: string; projectId: string; inWorkflow?: boolean;
}) {
  const o = meetingOutcome(meeting);
  const sections = prdSections(meeting.prd_markdown).length;
  const seats = seatsOf(meeting);
  const repo = repoOf(meeting.pull_request_url);
  const typed = kindOf(meeting) !== "prd_panel";
  const sub = o.stage === "live" ? `${seats.length} seats, updates live`
    : o.stage === "published" ? [repo, sections ? `${sections} sections` : ""].filter(Boolean).join(", ")
    : o.stage === "building" ? "The PRD is being built"
    : o.stage === "approved" ? "Approved, publishing it as a pull request"
    : o.stage === "awaiting" ? "Publishing waits for your approval"
    : o.stage === "simulated" ? "No model was called"
    : o.stage === "verdict" ? `${(meeting.dissent ?? []).length} dissent${(meeting.dissent ?? []).length === 1 ? "" : "s"} recorded`
    : o.stage === "failed" ? `Stopped at ${PHASE_LABEL[meeting.phase ?? ""] ?? "a phase"}`
    : "";
  return (
    <div className="rp-vcard" data-tone={o.tone} data-testid="meeting-outcome" data-stage={o.stage}>
      <Verdict tone={o.tone} big>{o.pill}</Verdict>
      <b>{o.stage === "live" ? panelDoing(meeting) : o.headline}</b>
      {sub ? <span className="rp-sub">{sub}</span> : null}
      {o.stage === "failed" && meeting.error ? (
        <div className="rp-attn" data-testid="meeting-error"><b>Why it stopped</b><span>{meeting.error}</span></div>
      ) : null}
      {o.stage === "live" ? (
        <span className="rp-faces" aria-label="Seats">
          {seats.map((s) => <SeatAvatar key={s} role={s} size={32} state="thinking" />)}
          <BossMark size={32} />
        </span>
      ) : null}
      <div className="rp-vact">
        {o.stage === "failed" ? <ConveneAgain meeting={meeting} projectId={projectId} /> : null}
        {o.stage === "building" || o.stage === "built" ? (
          <a className="rp-btn primary" href={consoleHref(search, { area: "code_tasks", run: meeting.build?.id })}>
            Open the build</a>
        ) : null}
        {!typed && !inWorkflow && (o.stage === "published" || o.stage === "approved") ? (
          <a className="rp-btn primary" data-testid="meeting-build-it" href={buildHref(meeting, search)}>Build it</a>
        ) : null}
        {meeting.pull_request_url ? (
          <a className={`rp-btn${inWorkflow ? " primary" : ""}`} href={changeHref(meeting.pull_request_url)} {...linkTarget(meeting.pull_request_url)}>
            Open the PR 
          </a>
        ) : null}
        {(o.stage === "awaiting" || o.stage === "declined" || (typed && meeting.decision_id)) ? (
          <a className="rp-btn primary" data-testid="meeting-review" href={decisionHref(search, meeting.decision_id)}>
            {typed ? "Answer it in Approvals →" : "Review in Approvals →"}</a>
        ) : null}
      </div>
    </div>
  );
}

// ── The drawer: vote, what was asked, task, status, timeline ───────────────────────────────

function MeetingEvidence({ meetingId, projectId }: { meetingId: string; projectId: string }) {
  const [evidence, setEvidence] = useState<{
    phase_timeline: Array<{ phase: string; status?: string; artifact_id?: string }>;
    artifact_links: Array<{ kind: string; artifact_id: string }>;
    receipt_id?: string | null;
    simulated?: boolean;
    meeting_type?: string;
    decision_id?: string | null;
  } | null>(null);
  useEffect(() => {
    let cancelled = false;
    callAction(loadMeetingEvidenceAction, undefined, "meeting:read")(meetingId, projectId).then((result) => {
      if (!cancelled && result.ok) setEvidence(result);
    });
    return () => { cancelled = true; };
  }, [meetingId, projectId]);
  if (!evidence || (evidence.phase_timeline.length === 0 && evidence.artifact_links.length === 0)) {
    return (
      <div className="rp-card" data-testid="meeting-evidence-none">
        <div className="rp-card-h"><b>Phase timeline</b></div>
        <span className="rp-sub">{evidence ? "No phase records were kept for this panel." : "Loading…"}</span>
      </div>
    );
  }
  const ids = [evidence.decision_id, evidence.receipt_id, ...evidence.artifact_links.map((l) => l.artifact_id)]
    .filter(Boolean).join(" ");
  return (
    <div className="rp-card" data-testid="meeting-evidence">
      <div className="rp-card-h"><b>Phase timeline</b></div>
      {evidence.simulated ? <p className="rp-sub">A no-spend fixture, no live model was called.</p> : null}
      {evidence.phase_timeline.length ? (
        <ol className="rp-timeline">
          {evidence.phase_timeline.map((step) => (
            <li key={step.phase}>
              <b>{PHASE_LABEL[step.phase] ?? step.phase.replace(/_/g, " ")}</b>
              {", "}{step.status === "completed" || !step.status ? "done" : step.status.replace(/_/g, " ")}
            </li>
          ))}
        </ol>
      ) : null}
      {evidence.artifact_links.length ? (
        <p className="rp-sub">{evidence.artifact_links.length} record{evidence.artifact_links.length === 1 ? "" : "s"} kept
          ({[...new Set(evidence.artifact_links.map((l) => l.kind.replace(/_/g, " ")))].join(", ")})</p>
      ) : null}
      {ids ? <CopyId id={ids} label="Copy record ids" /> : null}
    </div>
  );
}

function Drawer({ meeting, projectId, actor, search }: { meeting: Meeting; projectId: string; actor: string; search: string }) {
  const brief = briefOf(meeting);
  return (
    <details className="rp-more" data-testid="meeting-drawer">
      <summary>{[meeting.can_vote === false ? "The vote" : "Record the vote", "What was asked",
                 meeting.task_id ? "Linked task" : "", "Status"].filter(Boolean).join(", ")}</summary>
      <div className="rp-drawer">
        {meeting.can_vote === false ? (
          <div className="rp-card" data-testid="meeting-vote-closed">
            <div className="rp-card-h"><b>Meeting consensus</b></div>
            <span className="rp-sub">The vote opens when the panel finishes and records its minutes.</span>
          </div>
        ) : (
          <MeetingConsensusCard projectId={projectId} actor={actor} meetingId={meeting.id} />
        )}
        <div className="rp-card" data-testid="meeting-directive">
          <div className="rp-card-h"><b>What was asked</b></div>
          <div className="rp-directive">{brief || "—"}</div>
          {meeting.context ? (
            <details className="rp-fold" data-testid="meeting-context">
              <summary>Context BOSS added</summary>
              <div className="rp-full"><Markdown text={meeting.context} /></div>
            </details>
          ) : null}
          {meeting.url ? (
            <a className="rp-link sm" href={meeting.url} target="_blank" rel="noreferrer">Studied {meeting.url} </a>
          ) : null}
        </div>
        <div className="rp-card" data-testid="meeting-task">
          <div className="rp-card-h"><b>Linked task</b></div>
          {meeting.task_id ? (
            <>
              <p className="rp-hyp">{shortTitle(meeting.task_title ?? "", 200)}</p>
              {meeting.task_status ? <span className="rp-sub">{meeting.task_status.replace(/_/g, " ")}</span> : null}
              <a className="rp-link sm" href={consoleHref(search, { area: "work", task: meeting.task_id })}>View in Work</a>
            </>
          ) : <span className="rp-sub">No task, this panel stands on its own.</span>}
        </div>
        <div className="rp-card" data-testid="meeting-status">
          <div className="rp-card-h"><b>Status and cost</b></div>
          <dl className="rp-kv">
            <dt>Kind</dt><dd>{TYPE_LABEL[kindOf(meeting)]}</dd>
            <dt>Status</dt><dd>{STATUS_WORDS[meeting.status] ?? meeting.status}{meetingLive(meeting) ? `, ${PHASE_LABEL[meeting.phase ?? ""] ?? ""}` : ""}</dd>
            {meeting.prd_label && kindOf(meeting) === "prd_panel" ? <><dt>PRD</dt><dd>{meeting.prd_label}</dd></> : null}
            {took(meeting.created_at, meeting.completed_at) ? <><dt>Took</dt><dd>{took(meeting.created_at, meeting.completed_at)}</dd></> : null}
            {meeting.model ? <><dt>Model</dt><dd>{meeting.model}</dd></> : null}
            <dt>Cost</dt><dd>{meeting.cost_usd_micros ? usd(meeting.cost_usd_micros, 2) : "—"}</dd>
            {meeting.research_summary?.pages_studied ? <><dt>Studied</dt><dd>{meeting.research_summary.pages_studied} pages</dd></> : null}
            <dt>Seats</dt><dd>{(meeting.participants ?? []).join(", ")}</dd>
          </dl>
          <CopyId id={meeting.id} />
        </div>
        <MeetingEvidence meetingId={meeting.id} projectId={projectId} />
      </div>
    </details>
  );
}

// ── The meeting room ───────────────────────────────────────────────────────────────────────

function MeetingRoom({ id, listed, projectId, actor, onBack }: {
  id: string; listed: Meeting | null; projectId: string; actor: string; onBack: () => void;
}) {
  const search = useSearch();
  const { meeting, error } = useMeeting(id, projectId, listed);
  if (!meeting) {
    return (
      <div className="rp" data-testid="meeting-room">
        <button type="button" className="rp-back" onClick={onBack}>← All panels</button>
        <div className="rp-empty" role="status" data-testid={error ? "meeting-not-found" : undefined}>
          {error ? <><b>This panel isn&apos;t here any more.</b>{sayLoadError(error, "panel")}</>
            : <b>Opening the panel…</b>}
        </div>
      </div>
    );
  }
  const live = meetingLive(meeting);
  const seats = orderedSeats(Object.fromEntries(seatsOf(meeting).map((s) => [s, 1])));
  const line = briefLine(meeting.directive, 220, meeting.brief);
  const kind = kindOf(meeting);
  return (
    <div className="rp" data-testid="meeting-room" data-status={meeting.status} data-kind={kind}>
      <button type="button" className="rp-back" onClick={onBack}>← All panels</button>
      <section className="rp-dhero" data-kind="meeting">
        <div className="rp-aurora" aria-hidden />
        <div className="rp-dh-l">
          <div className="rp-area-name">
            <span className="rp-dot" data-tone={live ? undefined : meeting.status === "failed" ? "bad" : "idle"} aria-hidden />
            {TYPE_LABEL[kind]}{meeting.status === "simulated" ? ", fixture" : ""}
            <Ago iso={meeting.created_at} prefix=", " />
            {took(meeting.created_at, meeting.completed_at) ? `, took ${took(meeting.created_at, meeting.completed_at)}` : ""}
            {meeting.cost_usd_micros ? `, ${usd(meeting.cost_usd_micros)}` : ""}
          </div>
          <h2 className="rp-dh-t" title={briefOf(meeting)}>{titleOf(meeting)}</h2>
          {line ? <p className="rp-dh-s">{line}</p> : null}
          <Stepper stages={panelStages(meeting)} live={live} testId="meeting-stages" />
        </div>
        <div className="rp-dh-r">
          <OutcomeCard meeting={meeting} search={search} projectId={projectId} />
          {meeting.status === "completed" && meeting.prd_markdown ? (
            <RateIt projectId={projectId} actor={actor} subject="panel" id={meeting.id} testId="meeting-rate" />
          ) : null}
        </div>
      </section>

      <Round meeting={meeting} round={1} seats={seats} live={live} />
      <Round meeting={meeting} round={2} seats={seats} live={live} />
      <Synthesis meeting={meeting} />
      {!live && !Object.keys(meeting.round1 ?? {}).length && !meeting.synthesis ? (
        <div className="rp-empty"><b>No turns were recorded.</b>
          {meeting.status === "failed" ? "The panel stopped before anyone spoke." : "This meeting kept minutes only."}</div>
      ) : null}
      <Drawer meeting={meeting} projectId={projectId} actor={actor} search={search} />
    </div>
  );
}

// ── Panels home ────────────────────────────────────────────────────────────────────────────

function median(xs: number[]): number | null {
  const s = xs.filter((x) => Number.isFinite(x) && x > 0).sort((a, b) => a - b);
  return s.length ? s[Math.floor(s.length / 2)] : null;
}

function RoundTable({ seats }: { seats: MeetingRole[] }) {
  useIdentityVersion();
  const n = seats.length;
  return (
    <div className="rp-table-wrap" data-testid="meeting-table">
      <div className="rp-table">
        <span className="rp-tmark">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/boss-mark.png" alt="" />
          <small>Facilitator</small>
        </span>
      </div>
      {seats.map((r, i) => {
        const a = (-90 + (360 * i) / Math.max(n, 1)) * (Math.PI / 180);
        return (
          <span key={r.key} className="rp-tseat"
                style={{ left: `calc(50% + var(--rx, 128px) * ${Math.cos(a).toFixed(3)})`,
                         top: `calc(50% + var(--ry, 116px) * ${Math.sin(a).toFixed(3)})`,
                         animationDelay: `${-i * 1.3}s` }}>
            <SeatAvatar role={r.title} size={48} />
            <small>{callsignOf(r.title) || r.title}</small>
          </span>
        );
      })}
      {n === 0 ? <span className="rp-table-empty">Pick at least one seat</span> : null}
    </div>
  );
}

type MeetingType = "prd_panel" | "go_no_go" | "standup";
const TYPES: Array<{ key: MeetingType; title: string; sub: string }> = [
  { key: "prd_panel", title: "PRD panel", sub: "Debate to a spec in a PR" },
  { key: "go_no_go", title: "Go / no-go", sub: "A decision with dissent" },
  { key: "standup", title: "Stand-up", sub: "Status across the team" },
];

function ConveneHero({ projectId, roles, tasks, meetings }: {
  projectId: string; roles: MeetingRole[]; tasks: { id: string; title: string }[]; meetings: Meeting[];
}) {
  useIdentityVersion(); // seat callsigns are read here
  const [type, setType] = useState<MeetingType>("prd_panel");
  const [site, setSite] = useState(false);
  const enabled = useMemo(() => roles.filter((r) => r.enabled !== false), [roles]);
  const [chosen, setChosen] = useState<Set<string>>(
    () => new Set(enabled.filter((r) => r.default ?? r.is_default).map((r) => r.key)));
  // Roles arrive after the first render on some loads; seed the defaults once they do.
  useEffect(() => {
    setChosen((cur) => (cur.size || !enabled.length ? cur
      : new Set(enabled.filter((r) => r.default ?? r.is_default).map((r) => r.key))));
  }, [enabled]);
  const toggle = (key: string) => setChosen((cur) => {
    const next = new Set(cur);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const done = meetings.filter((m) => m.status === "completed");
  const secs = median(done.map((m) => (m.completed_at && m.created_at
    ? (new Date(m.completed_at).getTime() - new Date(m.created_at).getTime()) / 1000 : NaN)));
  const cost = median(done.map((m) => m.cost_usd_micros ?? NaN));
  const estimate = type === "prd_panel"
    ? `${secs ? `About ${secs < 120 ? `${Math.round(secs)} seconds` : `${Math.round(secs / 60)} minutes`}` : "A few minutes"}${cost ? ` and ${usd(cost)}` : ""} a panel, nothing is built until you say so`
    : "A no-spend fixture today, no model is called";
  const fixture = type !== "prd_panel";
  return (
    <ActionForm permission="meeting:convene" action={conveneMeetingAction} className="rp-convene" data-testid="meeting-new-form"
                successMessage={fixture ? "The fixture ran, its minutes are below." : "Convened, the panel is in session below."}>
      <div className="rp-aurora" aria-hidden />
      <input type="hidden" name="project_id" value={projectId} />
      <div className="rp-cv-l">
        <div className="rp-area-name"><span className="rp-dot" aria-hidden />Panels, agents debate it, you get a decision</div>
        <h2 className="rp-h2">Convene a panel</h2>
        <div className="rp-types" role="radiogroup" aria-label="Meeting type">
          {TYPES.map((t) => (
            <label key={t.key} className="rp-type" data-on={type === t.key ? "true" : undefined}>
              <input type="radio" name="meeting_type" value={t.key} checked={type === t.key}
                     onChange={() => setType(t.key)} data-testid={`meeting-type-${t.key}`} />
              <b>{t.title}</b><span>{t.sub}</span>
            </label>
          ))}
        </div>
        <div className="rp-composer">
          <label className="rp-sr" htmlFor="rp-directive">Directive</label>
          <textarea id="rp-directive" name="directive" rows={2} required data-testid="meeting-directive-input"
                    placeholder={type === "standup" ? "Daily stand-up, blockers, commitments, today's plan."
                      : type === "go_no_go" ? "Should we ship the Lead-Triage Inbox to the first ten pilot teams this week?"
                      : "What should the panel decide or spec? e.g. “Add snoozing to the Lead-Triage Inbox: a rep snoozes a lead until a date and it returns that morning at the top.”"} />
          {site ? (
            <input name="url" className="rp-ctx rp-url" type="url" placeholder="https://example.com, the panel studies it first"
                   aria-label="Website to study" data-testid="meeting-url" autoFocus />
          ) : null}
          <div className="rp-c-row">
            {type === "prd_panel" ? (
              <>
                <button type="button" className="rp-chip" aria-pressed={site} onClick={() => setSite((x) => !x)}
                        data-testid="meeting-study-site"> Study a website</button>
                <span className="rp-chip">
                  <span aria-hidden>Link</span>
                  <select name="task_id" defaultValue="" aria-label="Attach to a task" data-testid="meeting-task-select">
                    <option value="">Attach to: new task</option>
                    {tasks.map((t) => (
                      <option key={t.id} value={t.id}>{shortTitle(t.title.replace(/^CEO command:\s*/, ""), 70)}</option>
                    ))}
                  </select>
                  <span className="rp-chip-caret" aria-hidden>⌄</span>
                </span>
              </>
            ) : <span className="rp-sub">Stand-up and go / no-go run as a no-spend fixture.</span>}
            <span className="rp-spacer" />
            <SubmitButton type="submit" className="rp-btn primary" data-testid="meeting-convene"
                          pendingLabel={fixture ? "Running…" : "Convening…"} disabled={chosen.size === 0}>
              Convene</SubmitButton>
          </div>
        </div>
        <How steps={["Opening views", "They debate", "The facilitator reconciles",
                     type === "prd_panel" ? "A PRD in a pull request" : type === "go_no_go" ? "A decision, with dissent" : "Minutes and actions"]} />
        <div className="rp-sub">{estimate}</div>
      </div>
      <div className="rp-cv-r">
        <RoundTable seats={enabled.filter((r) => chosen.has(r.key))} />
        <div className="rp-lbl" style={{ marginTop: 6 }}>Who sits at the table <em>tap to add or remove</em></div>
        <div className="rp-seats" data-testid="meeting-seats">
          {enabled.map((r) => {
            const on = chosen.has(r.key);
            return (
              <label key={r.key} className="rp-seat" data-on={on ? "true" : undefined} title={r.lens}>
                <input type="checkbox" name="roles" value={r.key} checked={on} onChange={() => toggle(r.key)} />
                <SeatAvatar role={r.title} size={28} />
                <b>{r.title}</b>
                <span className="rp-cs">{callsignOf(r.title)}</span>
              </label>
            );
          })}
          {enabled.length === 0 ? <span className="rp-sub">No seats yet, add one under Manage seats.</span> : null}
        </div>
      </div>
    </ActionForm>
  );
}

function PanelCard({ m, onOpen }: { m: Meeting; onOpen: () => void }) {
  const o = meetingOutcome(m);
  const seats = seatsOf(m);
  const live = meetingLive(m);
  const t = took(m.created_at, m.completed_at);
  return (
    <button type="button" className="rp-card-link" onClick={onOpen} data-testid="meeting-card" data-status={m.status}
            title={briefOf(m)}>
      <span className="rp-dtop">
        <Verdict tone={o.tone}>{o.pill}</Verdict>
        <span className="rp-when"><Ago iso={m.created_at} /></span>
      </span>
      <b className="rp-dtitle">{shortTitle(meetingCardTitle(m.title))}</b>
      <span className="rp-mrow">
        <span className="rp-faces">
          {seats.map((s) => <SeatAvatar key={s} role={s} size={28} state={live ? "thinking" : undefined} />)}
          <BossMark size={28} />
        </span>
        {live ? <span className="rp-prc">{PHASE_LABEL[m.phase ?? ""] ?? "In session"}</span>
          : kindOf(m) !== "prd_panel" ? <span className="rp-prc" data-tone="muted">{TYPE_LABEL[kindOf(m)]}</span>
          : m.pull_request_url ? <span className="rp-prc">PRD, PR #{m.pull_request_number}</span>
          // FROM THE PRD'S DECISION, never a hard-coded guess (meeting_service.prd_state_of).
          : <span className="rp-prc" data-tone="muted" data-testid="mr-meeting-prd-state">{m.prd_label ?? "PRD state unknown"}</span>}
      </span>
      <span className="rp-mfoot">
        <span>{seats.length} seat{seats.length === 1 ? "" : "s"} + facilitator{m.task_id ? ", task linked" : ""}</span>
        <span>{[t, m.cost_usd_micros ? usd(m.cost_usd_micros) : ""].filter(Boolean).join(", ") || "—"}</span>
      </span>
    </button>
  );
}

/* ---- Manage the org's seat template ------------------------------------------- */
function ManageSeats({ projectId, roles }: { projectId: string; roles: MeetingRole[] }) {
  return (
    <section className="rp-manage" data-testid="meeting-manage" aria-label="Manage seats">
      <p className="rp-sub">
        The seats are this organization&apos;s panel template. Add your own (Legal, Clinical, Sales…),
        and turn seats off to leave them out of new panels.
      </p>
      <div>
        {roles.map((r) => (
          <div key={r.key} className="rp-role-row" data-off={r.enabled === false ? "true" : undefined}>
            <SeatAvatar role={r.title} size={28} />
            <div>
              <b>{r.title}{r.is_default ? <span className="rp-sub">, default</span> : null}</b>
              <span>{r.lens}</span>
            </div>
            <div className="rp-role-acts">
              <ActionForm permission="meeting:configure" action={setMeetingRoleEnabledAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <input type="hidden" name="enabled" value={r.enabled === false ? "true" : "false"} />
                <SubmitButton type="submit" className="rp-btn sm">{r.enabled === false ? "Enable" : "Disable"}</SubmitButton>
              </ActionForm>
              <ActionForm permission="meeting:configure" action={deleteMeetingRoleAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <SubmitButton type="submit" className="rp-btn sm" data-testid="meeting-seat-remove"
                              onClick={(e) => {
                                // Removing a seat drops it from every new panel: ask first.
                                if (!window.confirm(`Remove the ${r.title} seat from new panels?`)) e.preventDefault();
                              }}>Remove</SubmitButton>
              </ActionForm>
            </div>
          </div>
        ))}
      </div>
      <ActionForm permission="meeting:configure" action={upsertMeetingRoleAction} className="rp-add-role">
        <input type="hidden" name="project_id" value={projectId} />
        <div className="rp-lbl">Add a seat</div>
        <input type="text" name="title" placeholder="Seat title, e.g. Legal Counsel" required aria-label="Seat title" />
        <textarea name="lens" rows={3} required aria-label="Lens"
                  placeholder="Its lens, how this seat thinks. e.g. legal and contractual risk: IP, liability, compliance, terms." />
        <div className="rp-c-row">
          <label className="rp-toggle"><input type="checkbox" name="is_default" value="true" /><i aria-hidden />On by default</label>
          <span className="rp-spacer" />
          <SubmitButton type="submit" className="rp-btn primary sm">Add seat</SubmitButton>
        </div>
      </ActionForm>
    </section>
  );
}

/* ---- Maintenance: one menu, two jobs ------------------------------------------- */

/**
 * Maintenance ⌄ → [Record a vote] [Sweep stalled panels]. One level, each job named once , 
 * not the consensus card's own folds three deep. The vote lists only panels with minutes to vote
 * against, and says why when there are none; results read as sentences.
 */
function PanelsMaintenance({ actor, meetings }: { actor: string; meetings: Meeting[] }) {
  const [job, setJob] = useState<"vote" | "sweep" | null>(null);
  const [vote, voteAction, votePending] = useActionState(callAction(tallyVotesAction, undefined, "meeting:convene"), null);
  const [sweep, sweepAction, sweepPending] = useActionState(callAction(sweepStalledMeetingsAction, undefined, "system:operate"), null);
  const votable = meetings.filter((m) => m.can_vote !== false && !meetingLive(m));
  const data = (r: typeof vote) => ((r?.data ?? {}) as Record<string, unknown>);
  return (
    <details className="rp-menu" data-testid="panels-maintenance">
      <summary className="rp-link">Maintenance ⌄</summary>
      <div className="rp-menu-body">
        <div className="rp-c-row">
          <button type="button" className={`rp-btn sm${job === "vote" ? " primary" : ""}`} aria-pressed={job === "vote"}
                  data-testid="maint-vote" onClick={() => setJob("vote")}>Record a vote</button>
          <button type="button" className={`rp-btn sm${job === "sweep" ? " primary" : ""}`} aria-pressed={job === "sweep"}
                  data-testid="maint-sweep" onClick={() => setJob("sweep")}>Sweep stalled panels</button>
        </div>
        {job === "vote" ? (
          votable.length ? (
            <form action={voteAction} className="rp-maint-form" data-testid="maint-vote-form">
              <input type="hidden" name="actor" value={actor} />
              <label className="rp-maint-field">Panel
                <select name="meeting_id" required defaultValue="" className="rp-ctx">
                  <option value="" disabled>Pick a finished panel…</option>
                  {votable.map((m) => <option key={m.id} value={m.id}>{titleOf(m)}</option>)}
                </select>
              </label>
              <div className="rp-c-row">
                {(["approve", "reject", "abstain"] as const).map((k) => (
                  <label key={k} className="rp-maint-field">{k === "approve" ? "For" : k === "reject" ? "Against" : "Abstain"}
                    <input name={k} type="number" min={0} required className="rp-ctx" />
                  </label>
                ))}
              </div>
              <SubmitButton permission="meeting:convene" type="submit" className="rp-btn primary sm" disabled={votePending}>Record the vote</SubmitButton>
              {vote ? (vote.ok
                ? <p className="rp-okmsg" data-testid="maint-vote-result">{voteSentence(data(vote))}
                    {((data(vote).dissent_records as Array<{ role?: string; reason?: string }> | undefined) ?? [])
                      .map((r, i) => <span key={i} style={{ display: "block" }}>{dissentLine(r)}</span>)}</p>
                : <p className="rp-err" role="alert">{vote.error}</p>) : null}
            </form>
          ) : <p className="rp-sub" data-testid="maint-vote-none">No panel has finished with minutes yet, so there is nothing to vote on.</p>
        ) : null}
        {job === "sweep" ? (
          <form action={sweepAction} className="rp-maint-form" data-testid="maint-sweep-form">
            <input type="hidden" name="actor" value={actor} />
            <p className="rp-sub">A panel whose worker died mid-round stays “in session” until it is swept. Sweeping marks it
              stopped, so it can be convened again.</p>
            <SubmitButton permission="system:operate" type="submit" className="rp-btn primary sm" disabled={sweepPending}>Sweep stalled panels</SubmitButton>
            {sweep ? (sweep.ok
              ? <p className="rp-okmsg" data-testid="maint-sweep-result">{Number(data(sweep).swept)
                  ? `Swept ${Number(data(sweep).swept)} stalled panel${Number(data(sweep).swept) === 1 ? "" : "s"}.`
                  : "Nothing was stalled."}</p>
              : <p className="rp-err" role="alert">{sweep.error}</p>) : null}
          </form>
        ) : null}
      </div>
    </details>
  );
}

/* ---- Meetings home (landing + routing) ---------------------------------------- */

export function MeetingsHome({
  projectId,
  actor,
  meetings,
  totals = null,
  roles,
  tasks,
  apiReachable,
}: {
  projectId: string;
  orgId?: string;
  actor: string;
  meetings: Meeting[];
  /** The server's totals over EVERY panel; the loaded pages only when an older API sends none. */
  totals?: { panels: number; prds: number } | null;
  roles: MeetingRole[];
  tasks: { id: string; title: string }[];
  apiReachable: boolean;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [showSeats, setShowSeats] = useState(false);
  // Panels past the first page ("Show more"), and whether there may be more still.
  const [more, setMore] = useState<Meeting[]>([]);
  const [moreState, setMoreState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const all = useMemo(() => {
    const seen = new Set(meetings.map((m) => m.id));
    return [...meetings, ...more.filter((m) => !seen.has(m.id))];
  }, [meetings, more]);

  // Open from a deep link after mount (avoids a hydration mismatch), `meeting=`, a run id in
  // `run=`, the task a dossier's panel was convened for (`task=`), or `#id`, and keep the
  // selection in sync with Back/Forward. A task whose panel is not on this page is looked up
  // by task (`task:<id>`), never dropped on the list.
  const meetingsRef = useMemo(() => all.map((m) => ({ id: m.id, task_id: m.task_id })), [all]);
  useEffect(() => {
    const read = () => {
      const q = new URLSearchParams(window.location.search);
      if (q.get("area") && q.get("area") !== "meetings") return;
      const found = meetingFromUrl(window.location.search, window.location.hash, meetingsRef);
      const task = q.get("task");
      setSelected(found ?? (task ? `task:${task}` : null));
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [meetingsRef]);

  // Select a meeting AND reflect it in the URL (?meeting=…) as a back-able entry.
  const selectMeeting = useCallback((id: string | null) => {
    setSelected(id);
    pushParams({ meeting: id, run: null, task: null });
  }, []);

  const loadMore = useCallback(async () => {
    setMoreState("loading");
    const page = await fetchMeetingPage(projectId, meetings.length + more.length);
    if (page === null) { setMoreState("error"); return; }
    setMore((cur) => [...cur, ...page]);
    setMoreState(page.length < 25 ? "done" : "idle");
  }, [projectId, meetings.length, more.length]);

  if (selected) {
    return (
      <MeetingRoom id={selected} listed={all.find((m) => m.id === selected) ?? null}
                   projectId={projectId} actor={actor} onBack={() => selectMeeting(null)} />
    );
  }

  const panels = totals?.panels ?? all.length;
  const prds = totals?.prds ?? all.filter((m) => m.pull_request_url).length;
  const seatsAvailable = roles.filter((r) => r.enabled !== false).length;
  return (
    <div className="rp" data-testid="panels-home">
      <ConveneHero projectId={projectId} roles={roles} tasks={tasks} meetings={meetings} />
      <section aria-label="Recent panels">
        <div className="rp-sec-h">
          <h3>Recent panels</h3>
          <span className="rp-sub" data-testid="panels-counts">
            {panels} panel{panels === 1 ? "" : "s"}, {prds} became PRDs, {seatsAvailable} role-agent{seatsAvailable === 1 ? "" : "s"}
          </span>
          <span className="rp-spacer" />
          <PanelsMaintenance actor={actor} meetings={all} />
          <button type="button" className="rp-link" aria-expanded={showSeats} data-testid="meeting-manage-seats"
                  onClick={() => setShowSeats((x) => !x)}>
            {showSeats ? "Done with seats" : "Manage seats "}
          </button>
        </div>
        {showSeats ? <div style={{ marginBottom: 14 }}><ManageSeats projectId={projectId} roles={roles} /></div> : null}
        {!apiReachable ? (
          <div className="rp-empty" role="status"><b>The control plane is not answering.</b>Panels will show here once it is back.</div>
        ) : all.length === 0 ? (
          <div className="rp-empty" data-testid="panels-empty">
            <b>No panels yet.</b>
            Convene one above. To see the phases without spending anything, run the no-spend fixture.
            <ActionForm permission="meeting:convene" action={runSimulatedMeetingAction}>
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="directive" value="Community simulated panel fixture" />
              <SubmitButton type="submit" className="rp-btn sm">Run no-spend fixture</SubmitButton>
            </ActionForm>
          </div>
        ) : (
          <>
            <div className="rp-grid">
              {all.map((m) => <PanelCard key={m.id} m={m} onOpen={() => selectMeeting(m.id)} />)}
            </div>
            {(totals ? all.length < totals.panels : meetings.length >= 25) && moreState !== "done" ? (
              <div className="rp-more-row">
                <button type="button" className="rp-btn" onClick={() => void loadMore()} disabled={moreState === "loading"}
                        data-testid="panels-show-more">
                  {moreState === "loading" ? "Loading…" : moreState === "error" ? "Try again" : "Show more"}
                </button>
              </div>
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}

// ── Inside a workflow run ──────────────────────────────────────────────────────────────────

/**
 * A panel step's meeting, inside a workflow run's step panel: the outcome first (agreed, the
 * PR), the stages and each round filling in with seat faces while it runs, and the full room
 * one press away. `buildState` is the run's own build step, so "Build" says what the run did.
 */
export function MeetingCompact({ meetingId, projectId, buildState }: {
  meetingId: string; projectId: string; buildState?: StageState;
}) {
  useIdentityVersion(); // seat callsigns are read here
  const search = useSearch();
  const { meeting, error } = useMeeting(meetingId, projectId, null);
  const full = consoleHref(search, { area: "meetings", meeting: meetingId });
  if (!meeting) {
    return (
      <div className="rp rp-compact" data-testid="meeting-compact">
        <div className="rp-cmp">
          <BossMark size={48} />
          <div className="rp-cmp-body">
            <span className={error ? "rp-err" : "rp-sub"}>{error ? `This panel isn't here any more. ${sayLoadError(error, "panel")}` : "Opening the panel…"}</span>
          </div>
          <div className="rp-cmp-foot"><a className="rp-btn sm" href={full}>Open full view ↗</a></div>
        </div>
      </div>
    );
  }
  const live = meetingLive(meeting);
  const o = meetingOutcome(meeting);
  const seats = orderedSeats(Object.fromEntries(seatsOf(meeting).map((s) => [s, 1])));
  const t = took(meeting.created_at, meeting.completed_at);
  const head = meeting.synthesis_gist?.headline;
  const names = roundNames(meeting);
  return (
    <div className="rp rp-compact" data-testid="meeting-compact" data-status={meeting.status}>
      <div className="rp-cmp">
        <span className="rp-faces" aria-label="Seats">
          {seats.slice(0, 4).map((s) => <SeatAvatar key={s} role={s} size={40} state={live ? "thinking" : "done"} />)}
          <BossMark size={40} />
        </span>
        <div className="rp-cmp-body">
          <div className="rp-cmp-top">
            <Verdict tone={o.tone} testId="meeting-compact-outcome">{o.pill}</Verdict>
            <span className="rp-sub">
              {[`${seats.length} seats`, t ? `took ${t}` : "", meeting.cost_usd_micros ? usd(meeting.cost_usd_micros) : ""].filter(Boolean).join(", ")}
            </span>
          </div>
          <b className="rp-cmp-title">{live ? panelDoing(meeting) : o.headline}</b>
          {head && !live ? <p className="rp-cmp-line" title={head}><b>Agreed:</b> {clip(head, 160)}</p>
            : <p className="rp-cmp-line">{titleOf(meeting)}</p>}
        </div>
        <div className="rp-cmp-foot"><Stepper stages={panelStages(meeting, buildState)} live={live} /></div>
        {live || Object.keys(meeting.round1 ?? {}).length ? (
          <div className="rp-rounds-live" data-testid="meeting-compact-rounds">
            {([1, 2] as const).map((r) => {
              const have = (r === 1 ? meeting.round1 : meeting.round2) ?? {};
              return (
                <div key={r} className="rp-round-live">
                  <span>{r === 1 ? names[0] : names[2]}</span>
                  <span className="rp-faces">
                    {seats.map((s) => (
                      <span key={s} data-filled={have[s] ? "true" : "false"}
                            title={have[s] ? `${callsignOf(s) || s}: ${seatGist(meeting, r, s)?.headline ?? ""}` : `${s}, not yet`}>
                        <SeatAvatar role={s} size={24} state={have[s] ? "done" : live ? "thinking" : "idle"} />
                      </span>
                    ))}
                  </span>
                  <span>{Object.keys(have).length}/{seats.length}</span>
                </div>
              );
            })}
          </div>
        ) : null}
        {/* THE BUILD'S PRD STEP SHOWS THE PRD (BOSSAPP-929): readable here, the full page and
            the meeting one press away, and its file in the repository. */}
        {!live && meeting.prd_markdown && !meeting.security_review?.run_id ? (
          <PrdRecordView testid="meeting-compact-prd" prd={{
            title: titleOf(meeting), markdown: meeting.prd_markdown, fullPage: full, meeting: null,
            repo: parsePrdRepo((meeting as { prd_repo?: unknown }).prd_repo),
          }} />
        ) : null}
        {meeting.status === "failed" && meeting.error ? (
          <div className="rp-cmp-err"><div className="rp-attn"><b>Why it stopped</b><span>{meeting.error}</span></div></div>
        ) : null}
        <div className="rp-cmp-foot">
          {meeting.pull_request_url ? (
            <a className="rp-btn sm primary" href={changeHref(meeting.pull_request_url)} {...linkTarget(meeting.pull_request_url)}
               data-testid="meeting-compact-pr">Open the PR </a>
          ) : o.stage === "awaiting" ? (
            <a className="rp-btn sm primary" href={decisionHref(search, meeting.decision_id)}>Review in Approvals</a>
          ) : null}
          <a className={`rp-btn sm${meeting.pull_request_url || o.stage === "awaiting" ? "" : " primary"}`}
             href={full} data-testid="meeting-compact-open">Open full view ↗</a>
        </div>
      </div>
    </div>
  );
}

// Kept for the stage words elsewhere in the console.
export { PHASE_LABEL as MEETING_PHASE_LABEL };
