"use client";

/**
 * `+ Deploy` (spec §4.6; parity P-I-28, P-I-50, P-S-39): Repository, Ref, Target → `Plan`, the
 * one routed deploy (POST /v1/infra/targets/{id}/deploy; the server picks stack, delivery or
 * registry by the target's kind). It replaces DeployRepository, DeployWizard and the registry's
 * `Plan deployment run`. The registry-only fields sit under `More`: expected cost in DOLLARS
 * (the server converts), a budget reservation, and a release packet that must parse as a JSON
 * object before anything is sent.
 */
import { GatedButton } from "../../components/gated";
import React, { useEffect, useMemo, useState } from "react";

import { showToast } from "../client-forms";
import { listTargets, type TargetOption } from "./deploy-actions";
import { afterWrite, deployTarget, listRefs, listRepositories } from "./infra-actions";
import { ErrorLine } from "./infra-util";
import { Sheet } from "./kit";
import { MovedToProjectSettings } from "../projects/moved";

/** The release packet, or why it is not one: `Line 2: Unexpected token`. */
export function packetProblem(text: string): { value: Record<string, unknown> | null; error: string | null } {
  const t = text.trim();
  if (!t) return { value: null, error: null };
  try {
    const v = JSON.parse(t) as unknown;
    if (!v || typeof v !== "object" || Array.isArray(v)) return { value: null, error: "Must be a JSON object" };
    return { value: v as Record<string, unknown>, error: null };
  } catch (e) {
    const msg = e instanceof Error ? e.message : "Not JSON";
    const pos = /position (\d+)/.exec(msg);
    const line = pos ? t.slice(0, Number(pos[1])).split("\n").length : null;
    return { value: null, error: line ? `Line ${line}: ${msg}` : msg };
  }
}

export function DeploySheet({ projectId, actor, onClose, onPlanned, initialTarget }: {
  projectId: string; actor: string; onClose: () => void;
  /** The new deploy's ref (`stack:sd_…`): the tab opens its drawer. */
  onPlanned?: (ref: string | null) => void;
  initialTarget?: string | null;
}) {
  const [repos, setRepos] = useState<string[]>([]);
  const [repo, setRepo] = useState("");
  const [targets, setTargets] = useState<TargetOption[]>([]);
  const [targetId, setTargetId] = useState(initialTarget ?? "");
  const [ref, setRef] = useState("main");
  const [refs, setRefs] = useState<string[]>([]);
  const [more, setMore] = useState(false);
  const [cost, setCost] = useState("");
  const [reservation, setReservation] = useState("");
  const [packet, setPacket] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void listRepositories(projectId).then((r) => {
      if (!r.ok) return;
      const names = (r.data.repositories ?? []).filter((x) => x.full_name && !x.archived).map((x) => x.full_name);
      setRepos(names);
      setRepo((cur) => cur || (r.data.suggested && names.includes(r.data.suggested) ? r.data.suggested : names[0] ?? ""));
    });
    void listTargets(projectId).then((t) => { if (typeof t === "string") setError(t); else setTargets(t); });
  }, [projectId]);

  useEffect(() => {
    if (!repo.includes("/")) return;
    void listRefs(projectId, repo).then((r) => {
      if (!r.ok) return;
      setRefs((r.data.branches ?? []).map((b) => b.name));
      if (r.data.default_branch) setRef((cur) => (cur && cur !== "main" ? cur : r.data.default_branch as string));
    });
  }, [projectId, repo]);

  // Targets of the chosen repository first; a target with no repository recorded is offered too.
  const shown = useMemo(() => {
    const mine = targets.filter((t) => !t.repo || !repo || t.repo === repo || repo.endsWith(`/${t.repo}`));
    return mine.length ? mine : targets;
  }, [targets, repo]);
  useEffect(() => {
    if (!shown.some((t) => t.id === targetId)) setTargetId(shown[0]?.id ?? "");
  }, [shown, targetId]);

  const pk = packetProblem(packet);
  const costNum = cost.trim() ? Number(cost) : null;
  const costBad = costNum !== null && (!Number.isFinite(costNum) || costNum < 0);

  const plan = async () => {
    if (!targetId) { setError("Choose a target"); return; }
    if (!ref.trim() || /\s/.test(ref.trim())) { setError("One ref: a branch, tag or sha"); return; }
    if (pk.error || costBad) return;
    setBusy(true); setError(null);
    const r = await deployTarget(projectId, targetId, actor, ref.trim(), {
      ...(costNum !== null ? { expected_cost_usd: costNum } : {}),
      ...(reservation.trim() ? { budget_reservation_id: reservation.trim() } : {}),
      ...(pk.value ? { release_packet: pk.value } : {}),
    });
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Deploy planned");
    afterWrite();
    onPlanned?.(r.data.ref ?? null);
    onClose();
  };

  return (
    <Sheet title="Deploy" testId="inf-deploy-sheet" onClose={onClose}>
      <form className="inf-form" noValidate onSubmit={(e) => { e.preventDefault(); void plan(); }}>
        {/* A project's targets & hosts live on Project › Settings now; this sheet keeps deploying. */}
        <MovedToProjectSettings section="targets" projectId={projectId} testid="inf-deploy-moved" />
        <div className="inf-form-row">
          <label className="inf-field"><span>Repository</span>
            {repos.length ? (
              <select name="repository" value={repo} onChange={(e) => setRepo(e.target.value)} data-testid="inf-deploy-repo">
                {repos.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            ) : <input name="repository" value={repo} placeholder="owner/name" onChange={(e) => setRepo(e.target.value)}
                       data-testid="inf-deploy-repo" autoComplete="off" spellCheck={false} />}
          </label>
          <label className="inf-field"><span>Ref</span>
            <input name="ref" value={ref} onChange={(e) => setRef(e.target.value)} list="inf-deploy-refs" data-testid="inf-deploy-ref"
                   autoComplete="off" spellCheck={false} title="A branch, tag or commit sha" />
            <datalist id="inf-deploy-refs">{refs.map((r) => <option key={r} value={r} />)}</datalist>
          </label>
        </div>
        <label className="inf-field"><span>Target</span>
          <select name="target" value={targetId} onChange={(e) => setTargetId(e.target.value)} data-testid="inf-deploy-target">
            {shown.length === 0 ? <option value="">No targets yet</option> : null}
            {shown.map((t) => (
              <option key={t.id} value={t.id}>{t.app_name}, {t.environment}{t.target_kind ? ` (${t.target_kind})` : ""}</option>
            ))}
          </select>
        </label>
        <button type="button" className="inf-link" aria-expanded={more} data-testid="inf-deploy-more"
                title="Cloud account deploys: cost, budget hold, release packet" onClick={() => setMore((v) => !v)}>
          {more ? "Less" : "More"}
        </button>
        {more ? (
          <div className="inf-form" data-testid="inf-deploy-more-fields">
            <div className="inf-form-row">
              <label className="inf-field" data-invalid={costBad ? "1" : undefined}><span>Expected cost ($)</span>
                <input name="expected_cost_usd" inputMode="decimal" value={cost} placeholder="0.00"
                       onChange={(e) => setCost(e.target.value)} data-testid="inf-deploy-cost" /></label>
              <label className="inf-field"><span>Budget hold</span>
                <input name="budget_reservation_id" value={reservation} onChange={(e) => setReservation(e.target.value)}
                       placeholder="Reservation id" data-testid="inf-deploy-reservation" autoComplete="off" /></label>
            </div>
            <label className="inf-field" data-invalid={pk.error ? "1" : undefined}><span>Release packet</span>
              <textarea name="release_packet" rows={4} value={packet} onChange={(e) => setPacket(e.target.value)}
                        placeholder='{"ci_status": "passed"}' spellCheck={false} data-testid="inf-deploy-packet" />
            </label>
            <ErrorLine error={pk.error} testId="inf-deploy-packet-error" />
          </div>
        ) : null}
        <div className="inf-actions">
          <GatedButton permission="deployment:create" type="submit" className="ag-btn ag-btn-primary" disabled={busy || !targetId || !!pk.error || costBad}
                  data-testid="inf-deploy-plan-btn">{busy ? "Planning…" : "Plan"}</GatedButton>
          <ErrorLine error={error} testId="inf-deploy-sheet-error" />
        </div>
      </form>
    </Sheet>
  );
}
