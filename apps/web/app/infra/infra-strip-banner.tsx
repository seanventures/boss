"use client";

/**
 * S1 · THE TOP STRIP while a storage incident is open (BOSSAPP-1156). Red, on every page,
 * flashing until a person acknowledges it; it then shrinks to one quiet line and STAYS until the
 * incident ends. There is no close: "Acknowledge, never hide" (the explainer §5).
 *
 * Accessible: `role="alert" aria-live="assertive"` while it asks for attention, `role="status"`
 * after. The flash is a 2 s pulse (0.5 Hz, far under WCAG 2.3.1's 3 Hz) and it stops entirely
 * with `prefers-reduced-motion` (infra-storage.css). Mounted by ConsoleShell above its notice
 * slot, so it is above every area and above the identity banners.
 */
import React, { useEffect, useState } from "react";
import { ArrowRight, Check } from "@phosphor-icons/react";

import { useGate } from "../../components/gated";

import { ACKNOWLEDGE_PERMISSION, acknowledgeIncident, clock, etaWords, gb, stripHeadline, stripSubline, useInfraIncident, type StorageIncident } from "../../lib/infra-storage";
import { linkTo, navigate, plainClick } from "../agents/live-util";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";

/** The strip's link that navigates in place (the rail's way), new tab on a modifier. */
export function StripLink({ patch, className, children, testId, orgId }: {
  patch: Record<string, string | null>; className?: string; children: React.ReactNode; testId?: string; orgId?: string | null;
}) {
  return (
    <a className={className} href={linkTo(patch, orgId)} data-testid={testId}
       onClick={(e) => { if (plainClick(e)) { e.preventDefault(); navigate(patch); } }}>{children}</a>
  );
}

export const COMMAND_CENTER_PATCH = { area: "overview", role: null, view: null, tab: null, incident: null };

/** Pure: what the strip says, for the tests and the view. */
export function stripState(i: StorageIncident | null, ackedLocally: boolean): "hidden" | "flashing" | "acknowledged" {
  if (!i || i.status === "resolved") return "hidden";
  return i.acknowledgedBy || ackedLocally ? "acknowledged" : "flashing";
}

export function InfraStripBannerView({ incident, actor, acked, onAcknowledge, busy, sample, orgId, projectId }: {
  incident: StorageIncident; actor: string; acked: boolean; onAcknowledge: () => void; busy: boolean; sample?: boolean; orgId?: string | null; projectId?: string | null;
}) {
  const state = stripState(incident, acked);
  // Acknowledge is system:operate (contract §1): a viewer without it sees the strip, not the button.
  const gate = useGate(ACKNOWLEDGE_PERMISSION, projectId);
  if (state === "hidden") return null;
  const step = incident.stepCount > 0 ? `step ${Math.min(incident.stepIndex, incident.stepCount)} of ${incident.stepCount}` : "";
  const eta = etaWords(incident.etaSeconds);
  if (state === "acknowledged") {
    const who = incident.acknowledgedBy ?? actor;
    const when = clock(incident.acknowledgedAt);
    return (
      <div className="ia ia-quiet" role="status" data-testid="infra-strip" data-state="acknowledged" data-status={incident.status}>
        <span className="ia-dot" aria-hidden />
        <span className="ia-line">
          <b>Storage {incident.severity === "warn" ? "low" : "critical"}</b>
          <span className="ia-sep" aria-hidden>·</span>
          <span>{incident.status === "asking" ? "Infra Agent needs one answer" : incident.status === "resuming" ? "Infra Agent resuming work" : "Infra Agent cleaning"}</span>
          <span className="ia-sep" aria-hidden>·</span>
          <span data-testid="infra-strip-acked">acknowledged by {who}{when ? ` ${when}` : ""}</span>
          {sample ? <span className="ia-sample">Sample data</span> : null}
        </span>
        <StripLink patch={COMMAND_CENTER_PATCH} orgId={orgId} className="ia-link" testId="infra-strip-watch">Command Center <ArrowRight size={12} weight="bold" aria-hidden /></StripLink>
      </div>
    );
  }
  return (
    <div className="ia ia-loud" role="alert" aria-live="assertive" data-testid="infra-strip" data-state="flashing" data-status={incident.status}>
      <span className="ia-dot ia-pulse" aria-hidden />
      <span className="ia-face" aria-hidden><AgentAvatar agent="infra_steward" size={28} aiBadge={false} state="working" /></span>
      <span className="ia-text">
        <b className="ia-head" data-testid="infra-strip-head">{stripHeadline(incident)}</b>
        <span className="ia-sub" data-testid="infra-strip-sub" data-more={[` · ${gb(incident.freedGb)} freed`, eta ? ` · ${eta}` : "", step ? ` · ${step}` : ""].join("")}>{stripSubline(incident)}</span>
      </span>
      <span className="ia-meta" data-testid="infra-strip-meta">
        <span className="ia-chip" data-tone="green"><Check size={11} weight="bold" aria-hidden />{gb(incident.freedGb)} freed</span>
        {eta ? <span className="ia-chip" data-tone="amber">{eta}</span> : null}
        {step ? <span className="ia-chip" data-tone="blue">{step}</span> : null}
        {sample ? <span className="ia-sample">Sample data</span> : null}
      </span>
      <span className="ia-actions">
        <StripLink patch={COMMAND_CENTER_PATCH} orgId={orgId} className="ia-btn ia-btn-primary" testId="infra-strip-watch">
          Watch in Command Center <ArrowRight size={13} weight="bold" aria-hidden />
        </StripLink>
        {gate.allowed ? (
          <button type="button" className="ia-btn" data-testid="infra-strip-ack" disabled={busy} onClick={onAcknowledge}
                  title="Stops the flashing. The strip stays until the incident ends.">
            <Check size={13} weight="bold" aria-hidden /> Acknowledge
          </button>
        ) : null}
      </span>
    </div>
  );
}

const ACK_KEY = "boss.infra_ack";
const readAck = (): string | null => { try { return window.sessionStorage.getItem(ACK_KEY); } catch { return null; } };
const keepAck = (id: string) => { try { window.sessionStorage.setItem(ACK_KEY, id); } catch { /* the API's answer still carries it */ } };

export function InfraStripBanner({ projectId, actor, orgId }: { projectId: string | null; actor: string; orgId?: string | null }) {
  const { incident, refresh, sample } = useInfraIncident(projectId);
  // The press is remembered for this tab until the API's own `acknowledged_by` arrives, so a
  // re-mount of the shell (an area change) does not flash a strip the person already answered.
  const [acked, setAcked] = useState<string | null>(null);
  useEffect(() => { setAcked(readAck()); }, []);
  const [busy, setBusy] = useState(false);
  // A new incident flashes again: the local acknowledgement belongs to one incident id.
  useEffect(() => { if (incident && acked && acked !== incident.id) setAcked(null); }, [incident, acked]);
  if (!incident || !projectId) return null;
  const onAcknowledge = async () => {
    if (busy) return;
    setBusy(true);
    const r = await acknowledgeIncident(incident.id);
    setBusy(false);
    if (!r.ok) { showToast("error", r.error ?? "Not acknowledged."); return; }
    setAcked(incident.id);
    keepAck(incident.id);
    refresh();
  };
  return <InfraStripBannerView incident={incident} actor={actor} acked={acked === incident.id} onAcknowledge={() => void onAcknowledge()}
                               busy={busy} sample={sample} orgId={orgId} projectId={projectId} />;
}
