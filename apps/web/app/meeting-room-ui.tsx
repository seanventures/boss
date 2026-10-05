"use client";

/**
 * PANELS — agent meetings that debate to a decision or a PRD (area `meetings`).
 *
 * Not a sidebar page: the usual way in is a workflow run's panel step, whose step panel shows
 * `MeetingCompact` and links here for the full room. A dossier's "Convene a panel", the
 * Factory's PRD links, Approvals and old links land here too — by `meeting=`, `run=`, `task=`
 * or `#id`. The look is the approved mockup (boss-research-panels-mockup: panels-home,
 * meeting); styles in research/research.css.
 *
 *   Panels home  — convene hero (type, directive, website, task, the seats around the table),
 *                  recent panel cards, Manage seats.
 *   Meeting room — hero with the stage stepper and the outcome card; Round 1 and Round 2 as
 *                  speaker cards (each seat's gist, "Read all" for the full reply); the
 *                  facilitator's synthesis beside the PRD's sections; a drawer with the vote,
 *                  the directive, the linked task, status and cost, and the phase timeline.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import type { Meeting, MeetingRole } from "../lib/meeting-rooms";
import { Markdown } from "./markdown";
import { meetingCardTitle } from "../lib/meeting-title";
import {
  conveneMeetingAction,
  upsertMeetingRoleAction,
  setMeetingRoleEnabledAction,
  deleteMeetingRoleAction,
  runSimulatedMeetingAction,
  loadMeetingEvidenceAction,
} from "./actions";
import { MeetingConsensusCard } from "./phase3-ui";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";
import { consoleHref } from "./url-intent";
import { BossMark, How, Stepper, Verdict, clip, pushParams, useSearch } from "./research/bits";

import { callAction } from "../lib/call-action";
import type { AvatarState } from "../lib/agent-world-types";
import { agentIdentity, seatAvatar } from "../lib/agent-identity";
import { useIdentityVersion } from "../lib/use-agent-identities";
import { fetchMeetingRoom } from "../lib/meeting-room-detail";
import {
  ago, briefLine, composerHref, meetingFromUrl, meetingLive, meetingOutcome, orderedSeats, panelDoing,
  panelStages, prdSections, repoOf, seatGist, seatsOf, shortTitle, took, usd, SEAT_ORDER,
} from "../lib/research-panels";
import { AgentAvatar } from "./agent-avatar";

export const ROLE_ORDER = SEAT_ORDER;

const PHASE_LABEL: Record<string, string> = {
  research: "Researching the site",
  round1: "Round 1 · opening views",
  round2: "Round 2 · debate",
  synthesis: "Synthesis",
  draft: "Drafting the PRD",
  finalize: "Finalizing",
  done: "Done",
};

const TYPE_LABEL: Record<string, string> = { prd_panel: "PRD panel", go_no_go: "Go / no-go", standup: "Stand-up" };

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

/** A meeting, kept fresh: the list's copy when it has one, else read by id — and re-read every
 *  4s while the panel is in session, so rounds fill in as the seats speak. */
function useMeeting(id: string, projectId: string, listed: Meeting | null): { meeting: Meeting | null; error: string | null } {
  const [loaded, setLoaded] = useState<Meeting | null>(null);
  const [error, setError] = useState<string | null>(null);
  const current = loaded && loaded.id === id ? loaded : listed;
  const live = !current || meetingLive(current);

  useEffect(() => {
    let cancelled = false;
    const load = () => fetchMeetingRoom(id, projectId).then((res) => {
      if (cancelled) return;
      if (res.ok) { setLoaded(res.meeting); setError(null); } else setError(res.error);
    });
    if (!listed || live) void load();
    if (!live) return () => { cancelled = true; };
    const t = setInterval(() => { void load(); }, 4000);
    return () => { cancelled = true; clearInterval(t); };
  }, [id, projectId, live, listed]);
  // The read wins, unless the list has since seen the panel finish and the read has not.
  const pick = loaded && loaded.id === id
    ? (listed && !meetingLive(listed) && meetingLive(loaded) ? listed : loaded)
    : listed;
  return { meeting: pick ?? null, error };
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
  return (
    <section className="rp-round" aria-label={round === 1 ? "Opening views" : "Debate"} data-testid={`meeting-round-${round}`}>
      <div className="rp-r-h">
        <span className="rp-r-n" aria-hidden>{round}</span>
        <b>{round === 1 ? "Opening views" : "Debate"}</b>
        <span className="rp-sub">{round === 1 ? "Each seat frames it through its own lens"
          : "They answer each other — agreements, gaps, risks"}</span>
      </div>
      <div className="rp-turns" data-n={Math.min(seats.length, 4)}>
        {seats.map((s) => <TurnCard key={s} meeting={meeting} round={round} seat={s} live={now} />)}
      </div>
    </section>
  );
}

function Synthesis({ meeting }: { meeting: Meeting }) {
  const [open, setOpen] = useState(false);
  const [prdOpen, setPrdOpen] = useState(false);
  const sections = prdSections(meeting.prd_markdown);
  const gist = meeting.synthesis_gist ?? null;
  if (!meeting.synthesis && !meeting.prd_markdown) return null;
  return (
    <div className="rp-synth-row">
      {meeting.synthesis ? (
        <div className="rp-synth" data-testid="meeting-synthesis">
          <div className="rp-t-h">
            <BossMark size={36} />
            <div><b>Facilitator</b><span>Reconciles the panel</span></div>
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
      {meeting.prd_markdown ? (
        <div className="rp-prd" data-testid="meeting-prd">
          <div className="rp-card-h">
            <b>The PRD</b><span className="rp-spacer" />
            {meeting.pull_request_url ? (
              <a className="rp-link" href={meeting.pull_request_url} target="_blank" rel="noreferrer">
                PR #{meeting.pull_request_number ?? ""} ↗
              </a>
            ) : null}
          </div>
          {sections.length ? <ol>{sections.map((h) => <li key={h}>{h}</li>)}</ol> : null}
          <button type="button" className="rp-link sm" aria-expanded={prdOpen} onClick={() => setPrdOpen((x) => !x)}
                  style={{ alignSelf: "flex-start" }}>
            {prdOpen ? "Hide the PRD ↑" : "Read the PRD ↓"}
          </button>
          {prdOpen ? <div className="rp-full"><Markdown text={meeting.prd_markdown} /></div> : null}
        </div>
      ) : null}
    </div>
  );
}

// ── The outcome card ───────────────────────────────────────────────────────────────────────

function buildBrief(meeting: Meeting): string {
  const directive = (meeting.directive ?? meetingCardTitle(meeting.title)).trim();
  return meeting.pull_request_url ? `${directive}\n\nThe PRD: ${meeting.pull_request_url}` : directive;
}

function OutcomeCard({ meeting, search, inWorkflow = false }: { meeting: Meeting; search: string; inWorkflow?: boolean }) {
  const o = meetingOutcome(meeting);
  const live = meetingLive(meeting);
  const sections = prdSections(meeting.prd_markdown).length;
  const seats = seatsOf(meeting);
  const repo = repoOf(meeting.pull_request_url);
  const title = shortTitle(meeting.title);
  return (
    <div className="rp-vcard" data-tone={o.tone} data-testid="meeting-outcome">
      <Verdict tone={o.tone} big>
        {o.pill}{o.pill === "Agreed" ? (meeting.pull_request_url ? " · PRD ready" : " · PRD drafted") : ""}
      </Verdict>
      <b>{live ? panelDoing(meeting) : o.headline}</b>
      <span className="rp-sub">
        {live ? `${seats.length} seats · updates live`
          : meeting.pull_request_url ? [repo, sections ? `${sections} sections` : ""].filter(Boolean).join(" · ")
          : meeting.prd_markdown ? "Publishing waits for your approval"
          : meeting.status === "simulated" ? "No model was called"
          : meeting.status === "failed" ? `Stopped at ${PHASE_LABEL[meeting.phase ?? ""] ?? meeting.phase ?? "a phase"}`
          : ""}
      </span>
      {live ? (
        <span className="rp-faces" aria-label="Seats">
          {seats.map((s) => <SeatAvatar key={s} role={s} size={32} state="thinking" />)}
          <BossMark size={32} />
        </span>
      ) : null}
      <div className="rp-vact">
        {meeting.pull_request_url && !inWorkflow ? (
          <a className="rp-btn primary" data-testid="meeting-build-it"
             href={composerHref(search, { title, brief: buildBrief(meeting) })}>Build it →</a>
        ) : null}
        {meeting.pull_request_url ? (
          <a className={`rp-btn${inWorkflow ? " primary" : ""}`} href={meeting.pull_request_url} target="_blank" rel="noreferrer">
            Open the PR ↗
          </a>
        ) : meeting.prd_markdown && !live ? (
          <a className="rp-btn primary" href={consoleHref(search, { area: "decisions" })}>Review in Approvals →</a>
        ) : null}
      </div>
    </div>
  );
}

// ── The drawer: vote, directive, task, status, timeline ────────────────────────────────────

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
    callAction(loadMeetingEvidenceAction)(meetingId, projectId).then((result) => {
      if (!cancelled && result.ok) setEvidence(result);
    });
    return () => { cancelled = true; };
  }, [meetingId, projectId]);
  if (!evidence || (evidence.phase_timeline.length === 0 && evidence.artifact_links.length === 0)) {
    return (
      <div className="rp-card" data-testid="meeting-evidence-none">
        <div className="rp-card-h"><b>Phase timeline</b></div>
        <span className="rp-sub">{evidence ? "No phase artifacts were recorded." : "Loading…"}</span>
      </div>
    );
  }
  return (
    <div className="rp-card" data-testid="meeting-evidence">
      <div className="rp-card-h"><b>Phase timeline &amp; artifacts</b></div>
      {evidence.meeting_type ? <p className="rp-sub" data-testid="meeting-type">Type <code>{evidence.meeting_type}</code></p> : null}
      {evidence.simulated ? <p className="rp-sub">Simulated fixture — no live model was called.</p> : null}
      {evidence.decision_id ? <p className="rp-sub">Decision <code>{evidence.decision_id}</code></p> : null}
      {evidence.phase_timeline.length ? (
        <ol className="rp-timeline">
          {evidence.phase_timeline.map((step) => (
            <li key={step.phase}>
              <b>{PHASE_LABEL[step.phase] ?? step.phase}</b> {step.status ?? "recorded"}
              {step.artifact_id ? <> · <code>{step.artifact_id}</code></> : null}
            </li>
          ))}
        </ol>
      ) : null}
      {evidence.artifact_links.length ? (
        <ul className="rp-timeline">
          {evidence.artifact_links.map((link) => (
            <li key={`${link.kind}:${link.artifact_id}`}>{link.kind}: <code>{link.artifact_id}</code></li>
          ))}
        </ul>
      ) : null}
      {evidence.receipt_id ? <p className="rp-sub">Receipt <code>{evidence.receipt_id}</code></p> : null}
    </div>
  );
}

function Drawer({ meeting, projectId, actor, search }: { meeting: Meeting; projectId: string; actor: string; search: string }) {
  return (
    <details className="rp-more" data-testid="meeting-drawer">
      <summary>Record the vote · directive · linked task</summary>
      <div className="rp-drawer">
        <MeetingConsensusCard projectId={projectId} actor={actor} meetingId={meeting.id} />
        <div className="rp-card" data-testid="meeting-directive">
          <div className="rp-card-h"><b>Directive</b></div>
          <div className="rp-directive">{(meeting.directive ?? meetingCardTitle(meeting.title)).replace(/\r\n/g, "\n")}</div>
          {meeting.url ? (
            <a className="rp-link sm" href={meeting.url} target="_blank" rel="noreferrer">Studied {meeting.url} ↗</a>
          ) : null}
        </div>
        <div className="rp-card" data-testid="meeting-task">
          <div className="rp-card-h"><b>Linked task</b></div>
          {meeting.task_id ? (
            <>
              <p className="rp-hyp">{(meeting.task_title ?? meeting.task_id).replace(/^(CEO command|PRD meeting):\s*/, "").slice(0, 200)}</p>
              <span className="rp-sub">{meeting.task_status ?? ""}</span>
              <a className="rp-link sm" href={consoleHref(search, { area: "work", task: meeting.task_id })}>View in Work →</a>
            </>
          ) : <span className="rp-sub">No task — this panel stands on its own.</span>}
        </div>
        <div className="rp-card" data-testid="meeting-status">
          <div className="rp-card-h"><b>Status and cost</b></div>
          <dl className="rp-kv">
            <dt>Status</dt><dd>{meeting.status}{meetingLive(meeting) ? ` · ${PHASE_LABEL[meeting.phase ?? ""] ?? meeting.phase}` : ""}</dd>
            {meeting.prd_label ? <><dt>PRD</dt><dd>{meeting.prd_label}</dd></> : null}
            {took(meeting.created_at, meeting.completed_at) ? <><dt>Took</dt><dd>{took(meeting.created_at, meeting.completed_at)}</dd></> : null}
            {meeting.model ? <><dt>Model</dt><dd>{meeting.model}</dd></> : null}
            <dt>Cost</dt><dd>{meeting.cost_usd_micros ? `${usd(meeting.cost_usd_micros, 4)} equiv` : "—"}</dd>
            {meeting.research_summary?.pages_studied ? <><dt>Studied</dt><dd>{meeting.research_summary.pages_studied} pages</dd></> : null}
            <dt>Seats</dt><dd>{(meeting.participants ?? []).join(", ")}</dd>
            <dt>Id</dt><dd><code>{meeting.id}</code></dd>
          </dl>
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
        <div className="rp-empty" role="status">
          {error ? <><b>That panel did not open.</b>{error === "meeting not found" ? `Meeting ${id} is not in this project.` : error}</>
            : <b>Opening the panel…</b>}
        </div>
      </div>
    );
  }
  const live = meetingLive(meeting);
  const seats = orderedSeats(Object.fromEntries(seatsOf(meeting).map((s) => [s, 1])));
  const line = briefLine(meeting.directive);
  const type = TYPE_LABEL[(meeting as { meeting_type?: string }).meeting_type ?? ""] ?? "PRD panel";
  return (
    <div className="rp" data-testid="meeting-room" data-status={meeting.status}>
      <button type="button" className="rp-back" onClick={onBack}>← All panels</button>
      <section className="rp-dhero" data-kind="meeting">
        <div className="rp-aurora" aria-hidden />
        <div className="rp-dh-l">
          <div className="rp-eyebrow">
            <span className="rp-dot" data-tone={live ? undefined : meeting.status === "failed" ? "bad" : "idle"} aria-hidden />
            {meeting.status === "simulated" ? "Fixture" : type}
            {meeting.created_at ? ` · ${ago(meeting.created_at)}` : ""}
            {took(meeting.created_at, meeting.completed_at) ? ` · took ${took(meeting.created_at, meeting.completed_at)}` : ""}
            {meeting.cost_usd_micros ? ` · ${usd(meeting.cost_usd_micros)}` : ""}
          </div>
          <h2 className="rp-dh-t" title={meetingCardTitle(meeting.title)}>{shortTitle(meeting.title)}</h2>
          {line ? <p className="rp-dh-s">{line}</p> : null}
          <Stepper stages={panelStages(meeting)} live={live} testId="meeting-stages" />
          {live ? <p className="rp-sub" role="status">{panelDoing(meeting)} — this room updates live.</p> : null}
        </div>
        <div className="rp-dh-r"><OutcomeCard meeting={meeting} search={search} /></div>
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
    ? `${secs ? `About ${secs < 120 ? `${Math.round(secs)} seconds` : `${Math.round(secs / 60)} minutes`}` : "A few minutes"}${cost ? ` and ${usd(cost)}` : ""} a panel · nothing is built until you say so`
    : "A no-spend fixture today — no model is called";
  const fixture = type !== "prd_panel";
  return (
    <ActionForm action={conveneMeetingAction} className="rp-convene" data-testid="meeting-new-form"
                successMessage={fixture ? "The fixture ran — its minutes are below." : "Convened — the panel is in session below."}>
      <div className="rp-aurora" aria-hidden />
      <input type="hidden" name="project_id" value={projectId} />
      <div className="rp-cv-l">
        <div className="rp-eyebrow"><span className="rp-dot" aria-hidden />Panels · agents debate it, you get a decision</div>
        <h2 className="rp-h2">Convene a <em>panel</em></h2>
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
                    placeholder={type === "standup" ? "Daily stand-up — blockers, commitments, today's plan."
                      : type === "go_no_go" ? "Should we ship the Lead-Triage Inbox to the first ten pilot teams this week?"
                      : "What should the panel decide or spec? e.g. “Add snoozing to the Lead-Triage Inbox: a rep snoozes a lead until a date and it returns that morning at the top.”"} />
          {site ? (
            <input name="url" className="rp-ctx rp-url" type="url" placeholder="https://example.com — the panel studies it first"
                   aria-label="Website to study" data-testid="meeting-url" autoFocus />
          ) : null}
          <div className="rp-c-row">
            {type === "prd_panel" ? (
              <>
                <button type="button" className="rp-chip" aria-pressed={site} onClick={() => setSite((x) => !x)}
                        data-testid="meeting-study-site">↗ Study a website</button>
                <span className="rp-chip">
                  <span aria-hidden>⛓</span>
                  <select name="task_id" defaultValue="" aria-label="Attach to a task" data-testid="meeting-task-select">
                    <option value="">Attach to: new task</option>
                    {tasks.map((t) => (
                      <option key={t.id} value={t.id}>{t.title.replace(/^(CEO command|PRD meeting):\s*/, "").slice(0, 70)}</option>
                    ))}
                  </select>
                  <span className="rp-chip-caret" aria-hidden>⌄</span>
                </span>
              </>
            ) : <span className="rp-sub">Stand-up and go / no-go run as a no-spend fixture.</span>}
            <span className="rp-spacer" />
            <SubmitButton type="submit" className="rp-btn primary" data-testid="meeting-convene"
                          pendingLabel={fixture ? "Running…" : "Convening…"} disabled={chosen.size === 0}>
              Convene →
            </SubmitButton>
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
          {enabled.length === 0 ? <span className="rp-sub">No seats yet — add one under Manage seats.</span> : null}
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
            title={meetingCardTitle(m.title)}>
      <span className="rp-dtop">
        <Verdict tone={o.tone}>{o.pill}</Verdict>
        <span className="rp-when">{ago(m.created_at)}</span>
      </span>
      <b className="rp-dtitle">{shortTitle(meetingCardTitle(m.title))}</b>
      <span className="rp-mrow">
        <span className="rp-faces">
          {seats.map((s) => <SeatAvatar key={s} role={s} size={28} state={live ? "thinking" : undefined} />)}
          <BossMark size={28} />
        </span>
        {live ? <span className="rp-prc">{PHASE_LABEL[m.phase ?? ""] ?? "In session"}</span>
          : m.pull_request_url ? <span className="rp-prc">PRD · PR #{m.pull_request_number}</span>
          // FROM THE PRD'S DECISION, never a hard-coded guess (meeting_service.prd_state_of).
          : <span className="rp-prc" data-tone="muted" data-testid="mr-meeting-prd-state">{m.prd_label ?? "PRD state unknown"}</span>}
      </span>
      <span className="rp-mfoot">
        <span>{seats.length} seat{seats.length === 1 ? "" : "s"} + facilitator{m.task_id ? " · task linked" : ""}</span>
        <span>{[t, m.cost_usd_micros ? usd(m.cost_usd_micros) : ""].filter(Boolean).join(" · ") || "—"}</span>
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
              <b>{r.title}{r.is_default ? <span className="rp-sub"> · default</span> : null}</b>
              <span>{r.lens}</span>
            </div>
            <div className="rp-role-acts">
              <ActionForm action={setMeetingRoleEnabledAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <input type="hidden" name="enabled" value={r.enabled === false ? "true" : "false"} />
                <SubmitButton type="submit" className="rp-btn sm">{r.enabled === false ? "Enable" : "Disable"}</SubmitButton>
              </ActionForm>
              <ActionForm action={deleteMeetingRoleAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <SubmitButton type="submit" className="rp-btn sm">Remove</SubmitButton>
              </ActionForm>
            </div>
          </div>
        ))}
      </div>
      <ActionForm action={upsertMeetingRoleAction} className="rp-add-role">
        <input type="hidden" name="project_id" value={projectId} />
        <div className="rp-lbl">Add a seat</div>
        <input type="text" name="title" placeholder="Seat title — e.g. Legal Counsel" required aria-label="Seat title" />
        <textarea name="lens" rows={3} required aria-label="Lens"
                  placeholder="Its lens — how this seat thinks. e.g. legal and contractual risk: IP, liability, compliance, terms." />
        <div className="rp-c-row">
          <label className="rp-toggle"><input type="checkbox" name="is_default" value="true" /><i aria-hidden />On by default</label>
          <span className="rp-spacer" />
          <SubmitButton type="submit" className="rp-btn primary sm">Add seat</SubmitButton>
        </div>
      </ActionForm>
    </section>
  );
}

/* ---- Meetings home (landing + routing) ---------------------------------------- */

export function MeetingsHome({
  projectId,
  actor,
  meetings,
  roles,
  tasks,
  apiReachable,
}: {
  projectId: string;
  orgId?: string;
  actor: string;
  meetings: Meeting[];
  roles: MeetingRole[];
  tasks: { id: string; title: string }[];
  apiReachable: boolean;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [showSeats, setShowSeats] = useState(false);

  // Open from a deep link after mount (avoids a hydration mismatch) — `meeting=`, a run id in
  // `run=`, the task a dossier's panel was convened for (`task=`), or `#id` — and keep the
  // selection in sync with Back/Forward.
  const meetingsRef = useMemo(() => meetings.map((m) => ({ id: m.id, task_id: m.task_id })), [meetings]);
  useEffect(() => {
    const read = () => {
      const q = new URLSearchParams(window.location.search);
      if (q.get("area") && q.get("area") !== "meetings") return;
      setSelected(meetingFromUrl(window.location.search, window.location.hash, meetingsRef));
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

  if (selected) {
    return (
      <MeetingRoom id={selected} listed={meetings.find((m) => m.id === selected) ?? null}
                   projectId={projectId} actor={actor} onBack={() => selectMeeting(null)} />
    );
  }

  const prds = meetings.filter((m) => m.pull_request_url).length;
  return (
    <div className="rp" data-testid="panels-home">
      <ConveneHero projectId={projectId} roles={roles} tasks={tasks} meetings={meetings} />
      <section aria-label="Recent panels">
        <div className="rp-sec-h">
          <h3>Recent panels</h3>
          <span className="rp-sub">{meetings.length} panel{meetings.length === 1 ? "" : "s"} · {prds} became PRDs</span>
          <span className="rp-spacer" />
          <button type="button" className="rp-link" aria-expanded={showSeats} data-testid="meeting-manage-seats"
                  onClick={() => setShowSeats((x) => !x)}>
            {showSeats ? "Done with seats" : "Manage seats ↗"}
          </button>
        </div>
        {showSeats ? <div style={{ marginBottom: 14 }}><ManageSeats projectId={projectId} roles={roles} /></div> : null}
        {!apiReachable ? (
          <div className="rp-empty" role="status"><b>The control plane is not answering.</b>Panels will show here once it is back.</div>
        ) : meetings.length === 0 ? (
          <div className="rp-empty" data-testid="panels-empty">
            <b>No panels yet.</b>
            Convene one above. To see the phases without spending anything, run the no-spend fixture.
            <ActionForm action={runSimulatedMeetingAction}>
              <input type="hidden" name="project_id" value={projectId} />
              <input type="hidden" name="directive" value="Community simulated panel fixture" />
              <SubmitButton type="submit" className="rp-btn sm">Run no-spend fixture</SubmitButton>
            </ActionForm>
          </div>
        ) : (
          <div className="rp-grid">
            {meetings.map((m) => <PanelCard key={m.id} m={m} onOpen={() => selectMeeting(m.id)} />)}
          </div>
        )}
      </section>
    </div>
  );
}

// ── Inside a workflow run ──────────────────────────────────────────────────────────────────

/**
 * A panel step's meeting, inside a workflow run's step panel: the outcome first (agreed, the
 * PR), the stages and each round filling in with seat faces while it runs, and the full room
 * one press away.
 */
export function MeetingCompact({ meetingId, projectId }: { meetingId: string; projectId: string }) {
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
            <span className={error ? "rp-err" : "rp-sub"}>{error ? `The panel did not load: ${error}` : "Opening the panel…"}</span>
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
  return (
    <div className="rp rp-compact" data-testid="meeting-compact" data-status={meeting.status}>
      <div className="rp-cmp">
        <span className="rp-faces" aria-label="Seats">
          {seats.slice(0, 4).map((s) => <SeatAvatar key={s} role={s} size={40} state={live ? "thinking" : "done"} />)}
          <BossMark size={40} />
        </span>
        <div className="rp-cmp-body">
          <div className="rp-cmp-top">
            <Verdict tone={o.tone} testId="meeting-compact-outcome">
              {o.pill}{o.pill === "Agreed" ? (meeting.pull_request_url ? " · PRD ready" : " · PRD drafted") : ""}
            </Verdict>
            <span className="rp-sub">
              {[`${seats.length} seats`, t ? `took ${t}` : "", meeting.cost_usd_micros ? usd(meeting.cost_usd_micros) : ""].filter(Boolean).join(" · ")}
            </span>
          </div>
          <b className="rp-cmp-title">{live ? panelDoing(meeting) : o.headline}</b>
          {head && !live ? <p className="rp-cmp-line" title={head}><b>Agreed:</b> {clip(head, 160)}</p>
            : <p className="rp-cmp-line">{shortTitle(meeting.title)}</p>}
        </div>
        <div className="rp-cmp-foot"><Stepper stages={panelStages(meeting)} live={live} /></div>
        {live || Object.keys(meeting.round1 ?? {}).length ? (
          <div className="rp-rounds-live" data-testid="meeting-compact-rounds">
            {([1, 2] as const).map((r) => {
              const have = (r === 1 ? meeting.round1 : meeting.round2) ?? {};
              return (
                <div key={r} className="rp-round-live">
                  <span>Round {r}</span>
                  <span className="rp-faces">
                    {seats.map((s) => (
                      <span key={s} data-filled={have[s] ? "true" : "false"}
                            title={have[s] ? `${callsignOf(s) || s}: ${seatGist(meeting, r, s)?.headline ?? ""}` : `${s} — not yet`}>
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
        <div className="rp-cmp-foot">
          {meeting.pull_request_url ? (
            <a className="rp-btn sm primary" href={meeting.pull_request_url} target="_blank" rel="noreferrer">Open the PR ↗</a>
          ) : meeting.prd_markdown && !live ? (
            <a className="rp-btn sm primary" href={consoleHref(search, { area: "decisions" })}>Review in Approvals →</a>
          ) : null}
          <a className={`rp-btn sm${meeting.pull_request_url || (meeting.prd_markdown && !live) ? "" : " primary"}`}
             href={full} data-testid="meeting-compact-open">Open full view ↗</a>
        </div>
      </div>
    </div>
  );
}

// Kept for the stage words elsewhere in the console.
export { PHASE_LABEL as MEETING_PHASE_LABEL };
