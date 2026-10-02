"use client";

import { formatDateTime } from "../lib/datetime";
import type { ReliabilityFeed, ReliabilityIncident } from "../lib/reliability";
import {
  reliabilityDiagnoseAction,
  reliabilityFeedbackAction,
  reliabilityRemediateAction,
} from "./actions";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

const SEV_COLOR: Record<string, string> = {
  critical: "#f85149",
  warning: "#d29922",
  info: "#58a6ff",
};

const STATUS_COLOR: Record<string, string> = {
  open: "#f85149",
  triaged: "#d29922",
  diagnosing: "#58a6ff",
  remediating: "#a371f7",
  resolved: "#3fb950",
  dismissed: "#8b949e",
};

function Pill({ text, color }: { text: string; color: string }) {
  return (
    <span
      style={{
        fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999,
        color, border: `1px solid ${color}55`, background: `${color}18`,
        textTransform: "uppercase", letterSpacing: "0.04em", whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
}

/** A SubmitButton, so each press carries its own key to the server action (lib/api-write.ts):
 *  a double-pressed "Fix PR" opens one pull request, not two. */
function ActionButton({ label }: { label: string }) {
  return (
    <SubmitButton
      type="submit"
      style={{
        background: "transparent", color: "var(--ink-soft)", border: "1px solid var(--line)",
        borderRadius: 7, padding: "3px 10px", fontSize: 11.5, fontWeight: 600, cursor: "pointer",
      }}
    >
      {label}
    </SubmitButton>
  );
}

function IncidentRow({ projectId, incident }: { projectId: string; incident: ReliabilityIncident }) {
  const sev = SEV_COLOR[incident.severity] ?? "#8b949e";
  const canRemediate = ["triaged", "diagnosing"].includes(incident.status);
  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 10, padding: "9px 10px",
        borderLeft: `3px solid ${sev}`, background: "var(--panel-2, rgba(128,128,128,0.04))",
        borderRadius: 8,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <b style={{ fontSize: 13, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis" }}>
            {incident.title}
          </b>
          {incident.service ? (
            <span style={{ fontSize: 11.5, color: "var(--muted)", fontFamily: "var(--mono, monospace)" }}>
              {incident.service}
            </span>
          ) : null}
        </div>
        <div style={{ fontSize: 11.5, color: "var(--muted)", marginTop: 2 }}>
          {incident.signal_count} signal{incident.signal_count === 1 ? "" : "s"} · confidence{" "}
          {Number(incident.confidence ?? 0).toFixed(2)} · last seen{" "}
          <span>{formatDateTime(incident.last_seen)}</span>
        </div>
      </div>
      <Pill text={incident.status} color={STATUS_COLOR[incident.status] ?? "#8b949e"} />
      <div style={{ display: "flex", gap: 6 }}>
        <ActionForm action={reliabilityDiagnoseAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="incident_id" value={incident.id} />
          <ActionButton label="Diagnose" />
        </ActionForm>
        {canRemediate ? (
          <ActionForm action={reliabilityRemediateAction}>
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="incident_id" value={incident.id} />
            <ActionButton label="Fix PR" />
          </ActionForm>
        ) : null}
        <ActionForm action={reliabilityFeedbackAction}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="incident_id" value={incident.id} />
          <input type="hidden" name="verdict" value="false_alarm" />
          <ActionButton label="False alarm" />
        </ActionForm>
      </div>
    </div>
  );
}

export function ReliabilityHome({ feed }: { feed: ReliabilityFeed }) {
  const { incidents, learning, policy, projectId } = feed;
  const falseAlarm = learning?.evaluation?.false_alarm_rate;
  const fixSuccess = learning?.evaluation?.fix_success_rate;
  return (
    <div className="stack-grid">
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Incident board</h3>
        <p className="detail">
          Signals cluster into named incidents; diagnose runs the autonomous investigation engine
          (memory · change-correlation · dependency graph), and Fix PR launches the governed
          remediation pipeline — tested, review-paneled, gated.
        </p>
        {!feed.apiReachable ? (
          <p className="detail">API unreachable — the board will populate once the control plane responds.</p>
        ) : incidents.length === 0 ? (
          <p className="detail">No incidents — scans are clean or none have run yet.</p>
        ) : (
          <div style={{ display: "grid", gap: 6 }}>
            {incidents.map((i) => (
              <IncidentRow key={i.id} projectId={projectId} incident={i} />
            ))}
          </div>
        )}
      </article>

      <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
        <article className="stack-card">
          <h3 style={{ margin: 0 }}>Learning</h3>
          <p className="detail">The loop gets smarter per incident — verdicts tune thresholds and feed few-shot context.</p>
          <div style={{ display: "grid", gap: 5, fontSize: 12.5, color: "var(--ink-soft)" }}>
            <div>False-alarm rate: <b style={{ color: "var(--ink)" }}>{falseAlarm == null ? "—" : `${Math.round(falseAlarm * 100)}%`}</b></div>
            <div>Fix success rate: <b style={{ color: "var(--ink)" }}>{fixSuccess == null ? "—" : `${Math.round(fixSuccess * 100)}%`}</b></div>
            <div>Examples recorded: <b style={{ color: "var(--ink)" }}>{learning?.evaluation?.total ?? 0}</b></div>
            <div>Tuned (raised) signatures: <b style={{ color: "var(--ink)" }}>{learning?.raise_threshold?.length ?? 0}</b></div>
            <div>Few-shot corpus: <b style={{ color: "var(--ink)" }}>{learning?.few_shot_count ?? 0}</b></div>
          </div>
        </article>

        <article className="stack-card">
          <h3 style={{ margin: 0 }}>Autonomy policy</h3>
          <p className="detail">How far the agent may go alone — every action stays receipted and gated.</p>
          <div style={{ display: "grid", gap: 5, fontSize: 12.5, color: "var(--ink-soft)" }}>
            <div>
              Auto-remediate:{" "}
              <Pill
                text={policy?.auto_remediate ? "ON — Tier-0" : "OFF — Tier-1"}
                color={policy?.auto_remediate ? "#3fb950" : "#8b949e"}
              />
            </div>
            <div>Confidence floor: <b style={{ color: "var(--ink)" }}>{policy?.min_confidence ?? 0.8}</b></div>
            <div>Blast-radius cap: <b style={{ color: "var(--ink)" }}>{policy?.max_blast_radius ?? 5}</b></div>
            <div>Max auto severity: <b style={{ color: "var(--ink)" }}>{policy?.max_auto_severity ?? "critical"}</b></div>
          </div>
        </article>
      </div>
    </div>
  );
}
