"use client";

import { useCallback, useEffect, useState } from "react";
import { useFormStatus } from "react-dom";
import Link from "next/link";
import { formatDateTimeShort } from "../lib/datetime";
import type { Meeting, MeetingRole } from "../lib/meeting-rooms";
import { Markdown } from "./markdown";
import { meetingCardTitle } from "../lib/meeting-title";
import { pushUrlParam, readUrlParam } from "./url-nav";
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

import { callAction } from "../lib/call-action";
import type { AvatarState } from "../lib/agent-world-types";
import { seatAvatar, useIdentityVersion } from "../lib/agent-identity";
import { AgentAvatar } from "./agent-avatar";

export const ROLE_ORDER = [
  "Product Manager", "Engineer / Architect", "UX Designer", "Privacy & Safety",
  "Finance / FinOps", "Growth / Marketing", "QA / Test", "Customer Advocate", "Security", "Data / Analytics",
];

function orderedRoles(map: Record<string, string>): string[] {
  const known = ROLE_ORDER.filter((r) => map[r] !== undefined);
  const extra = Object.keys(map).filter((r) => !ROLE_ORDER.includes(r));
  return [...known, ...extra];
}

function fmtCost(micros?: number | null): string {
  if (!micros) return "—";
  return `$${(micros / 1e6).toFixed(4)}`;
}

const PHASE_STEPS = ["research", "round1", "round2", "synthesis", "draft", "finalize"];
const PHASE_LABEL: Record<string, string> = {
  research: "Researching the site",
  round1: "Round 1 · perspectives",
  round2: "Round 2 · debate",
  synthesis: "Synthesizing",
  draft: "Drafting the PRD",
  finalize: "Finalizing",
  done: "Done",
};
function isRunning(m: Meeting): boolean {
  return m.status === "running";
}
function phaseIndex(phase?: string): number {
  const i = PHASE_STEPS.indexOf(phase ?? "done");
  return i < 0 ? PHASE_STEPS.length : i;
}
function fmtDate(iso?: string | null): string {
  return formatDateTimeShort(iso, "");
}

// How long the meeting took, from start to conclusion (e.g. "2m 41s").
function fmtDuration(start?: string | null, end?: string | null): string {
  if (!start || !end) return "";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "";
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

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

function ContributionCard({ role, text }: { role: string; text: string }) {
  return (
    <div className="mr-card">
      <div className="mr-card-head">
        <SeatAvatar role={role} />
        <span className="mr-card-role">{role}</span>
      </div>
      <div className="mr-card-body"><Markdown text={text} /></div>
    </div>
  );
}

/* ---- New meeting form ------------------------------------------------------- */
function NewMeetingForm({
  projectId,
  roles,
  tasks,
  onCancel,
}: {
  projectId: string;
  roles: MeetingRole[];
  tasks: { id: string; title: string }[];
  onCancel: () => void;
}) {
  const [meetingType, setMeetingType] = useState<"prd_panel" | "standup">("prd_panel");
  return (
    <ActionForm action={conveneMeetingAction} className="mr-convene" data-testid="meeting-new-form">
      <input type="hidden" name="project_id" value={projectId} />
      <label className="mr-field-label" htmlFor="mr-type">Meeting type</label>
      <select
        id="mr-type"
        name="meeting_type"
        className="mr-select"
        value={meetingType}
        onChange={(event) => setMeetingType(event.target.value === "standup" ? "standup" : "prd_panel")}
      >
        <option value="prd_panel">PRD panel (live model)</option>
        <option value="standup">Standup (no-spend fixture)</option>
      </select>
      <label className="mr-field-label" htmlFor="mr-directive">Directive</label>
      <textarea
        id="mr-directive"
        name="directive"
        className="mr-textarea"
        rows={8}
        required
        placeholder={
          meetingType === "standup"
            ? "Daily standup — blockers, commitments, and today's plan."
            : (
              "Describe the goal in detail. e.g.\n" +
              "Produce a PRD to redesign our website (study the URL below).\n" +
              "• Preserve 100% of current features — list them.\n" +
              "• Make accessibility the priority.\n" +
              "• Add online donations and a volunteer-hours portal.\n" +
              "Publish the final PRD as a PDF pull request."
            )
        }
      />
      {meetingType === "prd_panel" ? (
        <>
          <label className="mr-field-label" htmlFor="mr-url">Website to study (optional)</label>
          <input id="mr-url" name="url" className="mr-url-input" placeholder="https://example.com" />

          <label className="mr-field-label">Attach to a task (optional — otherwise a new task is created)</label>
          <select name="task_id" className="mr-select" defaultValue="">
            <option value="">＋ Create a new task for this meeting</option>
            {tasks.map((t) => (
              <option key={t.id} value={t.id}>{t.title.replace(/^CEO command:\s*/, "").slice(0, 70)}</option>
            ))}
          </select>

          <label className="mr-field-label">Staff the panel (the meeting template)</label>
          <div className="mr-roles">
            {roles.filter((r) => r.enabled !== false).map((r) => (
              <label key={r.key} className="mr-role-pick" title={r.lens}>
                <input type="checkbox" name="roles" value={r.key} defaultChecked={r.default} />
                <SeatAvatar role={r.title} />
                <span className="mr-role-pick-title">{r.title}</span>
              </label>
            ))}
          </div>
          <span className="mr-convene-note">
            A Facilitator is always added. The panel studies the site, debates over two rounds, synthesizes, and drafts a
            PRD — then it goes to your approval gate before publishing. Runs ~3–5 min.
          </span>
        </>
      ) : (
        <span className="mr-convene-note">
          Standup uses the no-spend fixture by default: minutes, action items, optional dissent, and a Decision.
          No live model is called.
        </span>
      )}
      <ConveneActions meetingType={meetingType} onCancel={onCancel} />
    </ActionForm>
  );
}

/**
 * Convene / Cancel, with the pending state taken from the FORM rather than tracked by hand.
 *
 * Convening runs a full agent panel for three to five minutes and costs real money, so the
 * button has to say it is working — it once gave no feedback at all and an operator pressed it
 * six times, producing six PRDs, three inside the same second.
 *
 * The fix for that was a `convening` flag set to true on submit — AND NEVER SET BACK. Nothing
 * cleared it on success, on failure, or on a thrown action, so once the API was slow enough to
 * fail the request the button read "Convening the panel…" forever and the operator reported the
 * meeting as stuck. useFormStatus is derived from the form's own lifecycle and therefore clears
 * itself however the action ends; a hand-rolled boolean only clears on paths someone remembered
 * to write.
 */
function ConveneActions({ meetingType, onCancel }: {
  meetingType: "prd_panel" | "standup";
  onCancel: () => void;
}) {
  const { pending } = useFormStatus();
  const idle = meetingType === "standup" ? "Run standup fixture" : "Convene the panel";
  const busy = meetingType === "standup" ? "Running standup fixture…" : "Convening the panel…";
  return (
    <div className="mr-form-actions">
      <SubmitButton type="submit" className="cap-btn primary" pendingLabel={busy}>
        {idle}
      </SubmitButton>
      <button type="button" className="cap-btn" onClick={onCancel} disabled={pending}>
        Cancel
      </button>
      {pending ? (
        <span className="detail">
          {meetingType === "standup"
            ? "standup fixture is completing — minutes and a Decision appear below"
            : "the panel is being staffed — this takes 3–5 minutes and appears in the list below"}
        </span>
      ) : null}
    </div>
  );
}

/* ---- Single meeting detail (the Meeting Room) ------------------------------- */
function MeetingEvidence({
  meetingId,
  projectId,
}: {
  meetingId: string;
  projectId: string;
}) {
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
      if (!cancelled && result.ok) {
        setEvidence(result);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [meetingId, projectId]);
  if (!evidence || (evidence.phase_timeline.length === 0 && evidence.artifact_links.length === 0)) {
    return null;
  }
  return (
    <div className="mr-evidence" data-testid="meeting-evidence">
      <div className="mr-section-title">Phase timeline &amp; artifacts</div>
      {evidence.meeting_type ? (
        <p className="detail" data-testid="meeting-type">Type <code>{evidence.meeting_type}</code></p>
      ) : null}
      {evidence.simulated ? (
        <p className="detail">Simulated fixture — no live model was called.</p>
      ) : null}
      {evidence.decision_id ? (
        <p className="detail">Decision <code>{evidence.decision_id}</code></p>
      ) : null}
      {evidence.phase_timeline.length > 0 ? (
        <ol className="mr-phase-timeline">
          {evidence.phase_timeline.map((step) => (
            <li key={step.phase}>
              <strong>{PHASE_LABEL[step.phase] ?? step.phase}</strong>
              <span className="detail"> {step.status ?? "recorded"}</span>
              {step.artifact_id ? (
                <span className="mr-artifact-link"> artifact {step.artifact_id}</span>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
      {evidence.artifact_links.length > 0 ? (
        <ul className="mr-artifact-list">
          {evidence.artifact_links.map((link) => (
            <li key={`${link.kind}:${link.artifact_id}`}>
              {link.kind}: <code>{link.artifact_id}</code>
            </li>
          ))}
        </ul>
      ) : null}
      {evidence.receipt_id ? (
        <p className="detail">Receipt <code>{evidence.receipt_id}</code></p>
      ) : null}
    </div>
  );
}

function MeetingDetail({
  meeting,
  onBack,
  orgId,
  projectId,
  actor,
}: {
  meeting: Meeting;
  onBack: () => void;
  orgId?: string;
  projectId: string;
  actor: string;
}) {
  const orgQuery = orgId ? `org=${encodeURIComponent(orgId)}&` : "";
  return (
    <div className="mr">
      <button type="button" className="mr-back" onClick={onBack}>← All meetings</button>

      <div className="mr-header">
        <div className="mr-header-main">
          <span className="eyebrow">Meeting room</span>
          {/* A meeting launched from a benchmark is titled with the ENTIRE directive — every
              acceptance rule, every required npm script, ~2k characters. Rendered as an <h3>
              that became a wall of 28px bold text pushing the whole panel below the fold, so
              the meeting looked broken rather than verbose. The heading takes the lead
              sentence; the directive itself belongs in the body, at body size. */}
          <h3 className="mr-title">{leadTitle(meeting.title)}</h3>
          {fullDirective(meeting.title) ? (
            <details className="mr-directive">
              <summary>Full directive</summary>
              <p className="detail">{fullDirective(meeting.title)}</p>
            </details>
          ) : null}
          {meeting.url ? (
            <a className="mr-source" href={meeting.url} target="_blank" rel="noreferrer">studied {meeting.url} ↗</a>
          ) : null}
        </div>
        <div className="mr-header-meta">
          <span className={`cap-badge tone-${meeting.status === "completed" || meeting.status === "simulated" ? "green" : "amber"}`}>{meeting.status}</span>
          {fmtDuration(meeting.created_at, meeting.completed_at) ? (
            <span className="mr-meta-item" title="Time from convening to conclusion">⏱ took {fmtDuration(meeting.created_at, meeting.completed_at)}</span>
          ) : null}
          <span className="mr-meta-item">{meeting.model}</span>
          <span className="mr-meta-item">{fmtCost(meeting.cost_usd_micros)} equiv</span>
          {meeting.research_summary?.pages_studied ? (
            <span className="mr-meta-item">{meeting.research_summary.pages_studied} pages studied</span>
          ) : null}
        </div>
      </div>

      {/* Linked task */}
      {meeting.task_id ? (
        <div className="mr-task-link">
          <span className="mr-task-link-label">Linked task</span>
          <span className="mr-task-link-title">{(meeting.task_title ?? meeting.task_id).replace(/^CEO command:\s*/, "")}</span>
          {meeting.task_status ? <span className="cap-badge tone-muted">{meeting.task_status}</span> : null}
          <Link className="mr-task-link-go" href={`/?${orgQuery}area=work&task=${encodeURIComponent(meeting.task_id)}`}>
            View in Work →
          </Link>
        </div>
      ) : null}

      {/* Directive (full text — collapsed, expand to read) */}
      {meeting.directive ? (
        <details className="mr-directive-detail">
          <summary>Directive</summary>
          <div className="mr-directive-body">{meeting.directive.replace(/\r\n/g, "\n")}</div>
        </details>
      ) : null}

      {/* Participants */}
      <div className="mr-participants">
        {(meeting.participants ?? []).map((p) => (
          <span key={p} className="mr-participant"><SeatAvatar role={p} />{p}</span>
        ))}
      </div>

      {/* Flow ribbon — reflects the live phase for in-progress meetings */}
      <div className="mr-flow">
        {(() => {
          const labels = ["Research", "Round 1", "Round 2", "Synthesis", "Draft PRD", meeting.pull_request_url ? "Published" : "Publish"];
          const running = isRunning(meeting);
          const cur = phaseIndex(meeting.phase);
          return labels.map((s, i, arr) => {
            let cls = "";
            if (running) cls = i < cur ? "" : i === cur ? " active" : " pending";
            const showSpinner = running && i === cur;
            return (
              <span key={s} className="mr-flow-step">
                <span className={`mr-flow-dot${cls}`} />
                {showSpinner ? <em className="mr-flow-now">{s}…</em> : s}
                {i < arr.length - 1 ? <span className="mr-flow-arrow">→</span> : null}
              </span>
            );
          });
        })()}
      </div>
      {isRunning(meeting) ? (
        <p className="mr-running-note">⏳ Meeting in progress — {PHASE_LABEL[meeting.phase ?? ""] ?? meeting.phase}. This view updates live; the panel survives restarts.</p>
      ) : null}
      <MeetingEvidence meetingId={meeting.id} projectId={projectId} />
      <MeetingConsensusCard projectId={projectId} actor={actor} meetingId={meeting.id} />

      <div className="mr-section-title">Round 1 — opening perspectives</div>
      <div className="mr-grid">
        {orderedRoles(meeting.round1).map((role) => (
          <ContributionCard key={role} role={role} text={meeting.round1[role]} />
        ))}
      </div>

      <div className="mr-section-title">Round 2 — debate &amp; critique</div>
      <div className="mr-grid">
        {orderedRoles(meeting.round2).map((role) => (
          <ContributionCard key={role} role={role} text={meeting.round2[role]} />
        ))}
      </div>

      {meeting.synthesis ? (
        <>
          <div className="mr-section-title">Synthesis — the facilitator reconciles</div>
          <div className="mr-synth">
            <div className="mr-card-head">
              <SeatAvatar role="Facilitator / Chief of Staff" />
              <span className="mr-card-role">Facilitator / Chief of Staff</span>
            </div>
            <div className="mr-card-body tall"><Markdown text={meeting.synthesis} /></div>
          </div>
        </>
      ) : null}

      <div className="mr-section-title">Outcome</div>
      <div className="mr-outcome">
        {meeting.pull_request_url ? (
          <a className="wf-out wf-out-ext" href={meeting.pull_request_url} target="_blank" rel="noreferrer">
            <span className="wf-out-body">
              <span className="wf-out-title">The team&apos;s PRD{meeting.pull_request_number ? ` — GitHub PR #${meeting.pull_request_number}` : ""}</span>
              <span className="wf-out-sub mono">{meeting.pull_request_url}</span>
            </span>
            <span className="wf-out-go">open ↗</span>
          </a>
        ) : (
          <p className="detail">PRD drafted — awaiting your approval to publish (see Decisions).</p>
        )}
        {meeting.prd_markdown ? (
          <details className="mr-prd">
            <summary>Read the drafted PRD ▾</summary>
            <div className="mr-card-body tall"><Markdown text={meeting.prd_markdown} /></div>
          </details>
        ) : null}
      </div>
    </div>
  );
}

/* ---- Manage the org's role-agent template --------------------------------- */
function ManageRoles({ projectId, roles }: { projectId: string; roles: MeetingRole[] }) {
  return (
    <div className="mr-manage">
      <p className="mr-convene-note">
        These role-agents are this organization&apos;s meeting template — stored in the DB and editable
        here. The 10 baseline roles are seeded for every org; add your own (e.g. Legal, Clinical, Sales)
        and toggle which appear in the panel.
      </p>
      <div className="mr-role-list">
        {roles.map((r) => (
          <div key={r.key} className={`mr-role-row${r.enabled === false ? " off" : ""}`}>
            <SeatAvatar role={r.title} />
            <div className="mr-role-row-body">
              <span className="mr-role-row-title">{r.title} {r.is_default ? <span className="cap-badge tone-green">default</span> : null}</span>
              <span className="mr-role-row-lens">{r.lens}</span>
            </div>
            <div className="mr-role-row-actions">
              <ActionForm action={setMeetingRoleEnabledAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <input type="hidden" name="enabled" value={r.enabled === false ? "true" : "false"} />
                <SubmitButton type="submit" className="cap-btn">{r.enabled === false ? "Enable" : "Disable"}</SubmitButton>
              </ActionForm>
              <ActionForm action={deleteMeetingRoleAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <SubmitButton type="submit" className="cap-btn danger">Remove</SubmitButton>
              </ActionForm>
            </div>
          </div>
        ))}
      </div>
      <ActionForm action={upsertMeetingRoleAction} className="mr-convene">
        <input type="hidden" name="project_id" value={projectId} />
        <div className="mr-convene-row">
          <label>Role title<input name="title" placeholder="e.g. Legal Counsel" required /></label>
          <label className="mr-role-default-pick">
            <input type="checkbox" name="is_default" value="true" /> default-checked in new meetings
          </label>
        </div>
        <label className="mr-field-label">Persona / lens (the system prompt that defines how this agent thinks)</label>
        <textarea name="lens" className="mr-lens-input" rows={3} required
          placeholder="e.g. legal and contractual risk — IP, liability, compliance, terms, and what could expose the org legally." />
        <SubmitButton type="submit" className="cap-btn primary">Add role-agent</SubmitButton>
      </ActionForm>
    </div>
  );
}

/* ---- Meetings home (landing + routing) ------------------------------------- */

/** The first sentence, capped — what a heading can carry without becoming the document. */
function cleanTitle(raw: string): string {
  return (raw || "").replace(/^PRD meeting:\s*/, "").replace(/\s+/g, " ").trim();
}


function leadTitle(raw: string): string {
  const text = cleanTitle(raw);
  if (text.length <= 96) return text;
  // Prefer a real sentence break; fall back to a word break so a title never ends mid-word.
  const stop = text.slice(0, 120).search(/[.:;]\s/);
  if (stop > 24) return text.slice(0, stop);
  const cut = text.lastIndexOf(" ", 93);
  return `${text.slice(0, cut > 24 ? cut : 93)}…`;
}

/** Whatever the heading had to leave out — empty when the title already fits. */
function fullDirective(raw: string): string {
  const text = cleanTitle(raw);
  return text.length > 96 ? text : "";
}

export function MeetingsHome({
  projectId,
  orgId,
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
  const [showNew, setShowNew] = useState(false);
  const [showRoles, setShowRoles] = useState(false);

  // Open from a ?meeting= deep link after mount (avoids a hydration mismatch),
  // and keep selection in sync with Back/Forward.
  useEffect(() => {
    setSelected(readUrlParam("meeting") ?? null);
    const onPop = () => setSelected(readUrlParam("meeting") ?? null);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Select a meeting AND reflect it in the URL (?meeting=…) as a back-able entry.
  const selectMeeting = useCallback((id: string | null) => {
    setSelected(id);
    pushUrlParam("meeting", id);
  }, []);

  if (!apiReachable) return <p className="detail">Meetings API unreachable.</p>;

  const active = meetings.find((m) => m.id === selected);
  if (active) {
    return (
      <MeetingDetail
        meeting={active}
        onBack={() => selectMeeting(null)}
        orgId={orgId}
        projectId={projectId}
        actor={actor}
      />
    );
  }

  return (
    <div className="mr">
      <div className="mr-home-bar">
        <span className="mr-home-count">{meetings.length} meeting{meetings.length === 1 ? "" : "s"} · {roles.filter((r) => r.enabled !== false).length} role-agents</span>
        <div className="mr-home-actions">
          <button type="button" className="cap-btn" onClick={() => { setShowRoles((v) => !v); setShowNew(false); }}>
            {showRoles ? "Close" : "Manage roles"}
          </button>
          <button type="button" className="cap-btn primary" data-testid="meeting-new"
                  onClick={() => { setShowNew((v) => !v); setShowRoles(false); }}>
            {showNew ? "Close" : "＋ New meeting"}
          </button>
        </div>
      </div>

      {showRoles ? <ManageRoles projectId={projectId} roles={roles} /> : null}

      {showNew ? (
        <NewMeetingForm projectId={projectId} roles={roles} tasks={tasks} onCancel={() => setShowNew(false)} />
      ) : null}

      {meetings.length === 0 ? (
        <div className="mr-empty">
          <p>No meetings yet. Convene your first cross-functional panel with “New meeting”.</p>
          <p className="detail">
            A live panel calls a model. To prove the phase machine without spend, run a simulated fixture.
          </p>
          <ActionForm action={runSimulatedMeetingAction}>
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="directive" value="Community simulated panel fixture" />
            <SubmitButton type="submit" className="cap-btn">
              Run no-spend fixture
            </SubmitButton>
          </ActionForm>
        </div>
      ) : (
        <div className="mr-home-grid">
          {meetings.map((m) => (
            <button key={m.id} type="button" className="mr-meeting-card" onClick={() => selectMeeting(m.id)}>
              <div className="mr-meeting-card-head">
                <span className={`cap-badge tone-${m.status === "completed" || m.status === "simulated" ? "green" : isRunning(m) ? "blue" : "amber"}`}>
                  {isRunning(m) ? "running" : m.status}
                </span>
                <span className="mr-meeting-date">{fmtDate(m.completed_at ?? m.created_at)}</span>
              </div>
              <span className="mr-meeting-title">{meetingCardTitle(m.title)}</span>
              <div className="mr-meeting-avatars">
                {(m.participants ?? []).map((p) => <SeatAvatar key={p} role={p} size={24} />)}
              </div>
              <div className="mr-meeting-foot">
                {isRunning(m) ? (
                  <span className="mr-meeting-pill accent">⏳ {PHASE_LABEL[m.phase ?? ""] ?? "running"}</span>
                ) : m.pull_request_url ? (
                  <span className="mr-meeting-pill accent">PR #{m.pull_request_number}</span>
                ) : (
                  // FROM THE PRD'S DECISION, not from "has a PR URL": every card without one said
                  // "awaiting approval" on boss-1, including meetings whose PRD fed shipped projects.
                  <span className="mr-meeting-pill" data-testid="mr-meeting-prd-state">
                    {m.prd_label ?? "PRD state unknown"}
                  </span>
                )}
                <span className="mr-meeting-pill">{fmtCost(m.cost_usd_micros)}</span>
                {fmtDuration(m.created_at, m.completed_at) ? (
                  <span className="mr-meeting-pill">⏱ {fmtDuration(m.created_at, m.completed_at)}</span>
                ) : null}
                {m.task_id ? <span className="mr-meeting-pill">task linked</span> : null}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
