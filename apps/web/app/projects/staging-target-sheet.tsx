"use client";

/**
 * ONBOARDING'S "USE FOR STAGING", DONE IN PLACE (2026-11-07). The button used to send the
 * operator to Infra's generic App target sheet: production by default, no port, and a refusal
 * printed as raw JSON because the host is shared from another project. Now it is one confirm
 * sheet here — What · Where · Port · Manifest — whose press creates the staging target
 * (POST …/onboarding/deploy-target, step-up through useGuardedCall); the checklist row turns
 * green and the run moves on. Infra keeps its own sheet.
 */
import React, { useEffect, useState } from "react";
import { Rocket } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError, idGet, idPost, refusalSentence } from "../../lib/identity/api";
import { isPxDemo, pxHref, type OnboardingAction, type StagingProposal } from "../../lib/project-experience";
import { notifyChanged } from "../changed";
import { ConfirmSheet } from "./confirm-sheet";

/** The words for a refusal from the proposal or the create: never JSON. */
const said = (e: unknown, cancelled: string) =>
  e instanceof IdentityError && e.code === "step_up_cancelled" ? cancelled : refusalSentence(e);

export function StagingTargetSheet({ projectId, action, onClose, onDone }: {
  projectId: string; action: OnboardingAction; onClose: () => void; onDone?: () => void;
}) {
  const guarded = useGuardedCall();
  const [plan, setPlan] = useState<StagingProposal | null>(null);
  const [why, setWhy] = useState("");
  useEffect(() => {
    let live = true;
    const q = action.host_id ? `?host_id=${encodeURIComponent(action.host_id)}` : "";
    idGet<StagingProposal>(`/v1/projects/${encodeURIComponent(projectId)}/onboarding/deploy-target${q}`)
      .then((p) => { if (live) setPlan(p); })
      .catch((e: unknown) => { if (live) setWhy(said(e, "")); });
    return () => { live = false; };
  }, [projectId, action.host_id]);
  const wait = why ? "—" : "…";
  const rows = [
    { label: "What", value: plan ? `Staging target for ${plan.project}` : wait },
    { label: "Where", value: plan ? `${plan.address}${plan.shared ? " · shared host" : ""}` : wait, mono: true },
    { label: "Port", value: plan ? (plan.port ? String(plan.port) : "chosen at the first deploy") : wait, mono: true },
    { label: "Manifest", value: plan ? plan.manifest_path : wait, mono: true },
  ];
  const confirm = async (): Promise<string | null> => {
    if (why) return why;
    if (!plan) return "Still reading the host — a moment.";
    if (isPxDemo()) return null;
    try {
      await guarded("Add the staging target.", () => idPost(action.post ?? "", { host_id: plan.host_id, port: plan.port }));
    } catch (e) {
      return said(e, "Not added: you cancelled the check.");
    }
    notifyChanged("project.onboarding");
    notifyChanged("infra");
    onDone?.();
    return null;
  };
  return (
    <ConfirmSheet title="Use for staging" icon={<Rocket size={16} aria-hidden />} rows={rows} testid="px-ob-staging"
                  lede={why || "BOSS deploys staging here once the first work merges. Nothing is deployed now."}
                  press="Use for staging" onConfirm={confirm} onClose={onClose} />
  );
}

/** An onboarding item's one action: a link, or (`do`) the job done in place. */
export function ItemAction({ projectId, action, testid, onDone }: {
  projectId: string; action: OnboardingAction; testid?: string; onDone?: () => void;
}) {
  const [open, setOpen] = useState(false);
  if (action.do !== "staging_target" || !action.post) {
    return <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(action.href)} data-testid={testid}>{action.label}</a>;
  }
  return (
    <>
      <GatedButton permission="deployment_target:manage" projectId={projectId} className="px-btn" data-kind="amber" data-size="s" data-testid={testid}
                   onClick={() => setOpen(true)}>{action.label}</GatedButton>
      {open ? <StagingTargetSheet projectId={projectId} action={action} onClose={() => setOpen(false)} onDone={onDone} /> : null}
    </>
  );
}
