"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import type { Meeting, MeetingRole } from "../lib/meeting-rooms";
import { Markdown } from "./markdown";
import { pushUrlParam, readUrlParam } from "./url-nav";
import {
  conveneMeetingAction,
  upsertMeetingRoleAction,
  setMeetingRoleEnabledAction,
  deleteMeetingRoleAction,
} from "./actions";
import { SubmitButton } from "./submit-button";

const ROLE_META: Record<string, { initials: string; tone: string }> = {
  "Product Manager": { initials: "PM", tone: "accent" },
  "Engineer / Architect": { initials: "EN", tone: "blue" },
  "UX Designer": { initials: "UX", tone: "pink" },
  "Privacy & Safety": { initials: "PS", tone: "amber" },
  "Finance / FinOps": { initials: "FI", tone: "green" },
  "Growth / Marketing": { initials: "GR", tone: "pink" },
  "QA / Test": { initials: "QA", tone: "blue" },
  "Customer Advocate": { initials: "CA", tone: "accent" },
  Security: { initials: "SE", tone: "amber" },
  "Data / Analytics": { initials: "DA", tone: "green" },
  "Facilitator / Chief of Staff": { initials: "CS", tone: "green" },
};

function roleMeta(role: string) {
  return ROLE_META[role] ?? { initials: role.slice(0, 2).toUpperCase(), tone: "muted" };
}

const ROLE_ORDER = [
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
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
  } catch {
    return "";
  }
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

function Avatar({ role }: { role: string }) {
  const m = roleMeta(role);
  return <span className={`mr-avatar tone-${m.tone}`}>{m.initials}</span>;
}

function ContributionCard({ role, text }: { role: string; text: string }) {
  return (
    <div className="mr-card">
      <div className="mr-card-head">
        <Avatar role={role} />
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
  // Convening runs a full agent panel for three to five minutes and costs real money. The
  // button gave no feedback at all, so an operator pressed it six times and got six PRDs —
  // three of them inside the same second — and six approvals with no way to tell them apart.
  // The server now refuses a duplicate while one is running; this stops the clicks reaching
  // it, and says what is happening either way.
  const [convening, setConvening] = useState(false);
  return (
    <form action={conveneMeetingAction} className="mr-convene"
          onSubmit={() => setConvening(true)}>
      <input type="hidden" name="project_id" value={projectId} />
      <label className="mr-field-label" htmlFor="mr-directive">Directive</label>
      <textarea
        id="mr-directive"
        name="directive"
        className="mr-textarea"
        rows={8}
        required
        placeholder={
          "Describe the goal in detail. e.g.\n" +
          "Produce a PRD to redesign our website (study the URL below).\n" +
          "• Preserve 100% of current features — list them.\n" +
          "• Make accessibility the priority.\n" +
          "• Add online donations and a volunteer-hours portal.\n" +
          "Publish the final PRD as a PDF pull request."
        }
      />
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
            <Avatar role={r.title} />
            <span className="mr-role-pick-title">{r.title}</span>
          </label>
        ))}
      </div>
      <span className="mr-convene-note">
        A Facilitator is always added. The panel studies the site, debates over two rounds, synthesizes, and drafts a
        PRD — then it goes to your approval gate before publishing. Runs ~3–5 min.
      </span>
      <div className="mr-form-actions">
        <SubmitButton type="submit" className="cap-btn primary" disabled={convening}>
          {convening ? "Convening the panel…" : "Convene the panel"}
        </SubmitButton>
        <button type="button" className="cap-btn" onClick={onCancel} disabled={convening}>
          Cancel
        </button>
        {convening ? (
          <span className="detail">
            the panel is being staffed — this takes 3–5 minutes and appears in the list below
          </span>
        ) : null}
      </div>
    </form>
  );
}

/* ---- Single meeting detail (the Meeting Room) ------------------------------- */
function MeetingDetail({ meeting, onBack, orgId }: { meeting: Meeting; onBack: () => void; orgId?: string }) {
  const orgQuery = orgId ? `org=${encodeURIComponent(orgId)}&` : "";
  return (
    <div className="mr">
      <button type="button" className="mr-back" onClick={onBack}>← All meetings</button>

      <div className="mr-header">
        <div className="mr-header-main">
          <span className="eyebrow">Meeting room</span>
          <h3 className="mr-title">{meeting.title.replace(/^PRD meeting:\s*/, "").replace(/\s+/g, " ")}</h3>
          {meeting.url ? (
            <a className="mr-source" href={meeting.url} target="_blank" rel="noreferrer">studied {meeting.url} ↗</a>
          ) : null}
        </div>
        <div className="mr-header-meta">
          <span className={`cap-badge tone-${meeting.status === "completed" ? "green" : "amber"}`}>{meeting.status}</span>
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
          <span key={p} className="mr-participant"><Avatar role={p} />{p}</span>
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
              <Avatar role="Facilitator / Chief of Staff" />
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
            <Avatar role={r.title} />
            <div className="mr-role-row-body">
              <span className="mr-role-row-title">{r.title} {r.is_default ? <span className="cap-badge tone-green">default</span> : null}</span>
              <span className="mr-role-row-lens">{r.lens}</span>
            </div>
            <div className="mr-role-row-actions">
              <form action={setMeetingRoleEnabledAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <input type="hidden" name="enabled" value={r.enabled === false ? "true" : "false"} />
                <SubmitButton type="submit" className="cap-btn">{r.enabled === false ? "Enable" : "Disable"}</SubmitButton>
              </form>
              <form action={deleteMeetingRoleAction}>
                <input type="hidden" name="project_id" value={projectId} />
                <input type="hidden" name="role_key" value={r.key} />
                <SubmitButton type="submit" className="cap-btn danger">Remove</SubmitButton>
              </form>
            </div>
          </div>
        ))}
      </div>
      <form action={upsertMeetingRoleAction} className="mr-convene">
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
      </form>
    </div>
  );
}

/* ---- Meetings home (landing + routing) ------------------------------------- */
export function MeetingsHome({
  projectId,
  orgId,
  meetings,
  roles,
  tasks,
  apiReachable,
}: {
  projectId: string;
  orgId?: string;
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
    return <MeetingDetail meeting={active} onBack={() => selectMeeting(null)} orgId={orgId} />;
  }

  return (
    <div className="mr">
      <div className="mr-home-bar">
        <span className="mr-home-count">{meetings.length} meeting{meetings.length === 1 ? "" : "s"} · {roles.filter((r) => r.enabled !== false).length} role-agents</span>
        <div className="mr-home-actions">
          <button type="button" className="cap-btn" onClick={() => { setShowRoles((v) => !v); setShowNew(false); }}>
            {showRoles ? "Close" : "Manage roles"}
          </button>
          <button type="button" className="cap-btn primary" onClick={() => { setShowNew((v) => !v); setShowRoles(false); }}>
            {showNew ? "Close" : "＋ New meeting"}
          </button>
        </div>
      </div>

      {showRoles ? <ManageRoles projectId={projectId} roles={roles} /> : null}

      {showNew ? (
        <NewMeetingForm projectId={projectId} roles={roles} tasks={tasks} onCancel={() => setShowNew(false)} />
      ) : null}

      {meetings.length === 0 ? (
        <p className="mr-empty">No meetings yet. Convene your first cross-functional panel with “New meeting”.</p>
      ) : (
        <div className="mr-home-grid">
          {meetings.map((m) => (
            <button key={m.id} type="button" className="mr-meeting-card" onClick={() => selectMeeting(m.id)}>
              <div className="mr-meeting-card-head">
                <span className={`cap-badge tone-${m.status === "completed" ? "green" : isRunning(m) ? "blue" : "amber"}`}>
                  {isRunning(m) ? "running" : m.status}
                </span>
                <span className="mr-meeting-date" suppressHydrationWarning>{fmtDate(m.completed_at ?? m.created_at)}</span>
              </div>
              <span className="mr-meeting-title">{m.title.replace(/^PRD meeting:\s*/, "").replace(/\s+/g, " ")}</span>
              <div className="mr-meeting-avatars">
                {(m.participants ?? []).map((p) => <Avatar key={p} role={p} />)}
              </div>
              <div className="mr-meeting-foot">
                {isRunning(m) ? (
                  <span className="mr-meeting-pill accent">⏳ {PHASE_LABEL[m.phase ?? ""] ?? "running"}</span>
                ) : m.pull_request_url ? (
                  <span className="mr-meeting-pill accent">PR #{m.pull_request_number}</span>
                ) : (
                  <span className="mr-meeting-pill">awaiting approval</span>
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
