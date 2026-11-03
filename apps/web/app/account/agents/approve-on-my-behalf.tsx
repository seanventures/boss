"use client";

/**
 * MY ACCOUNT › AGENTS ACTING FOR ME › APPROVE ON MY BEHALF (IDENTITY.md §6.15): an Owner or Admin
 * names a delegate that approves or rejects chosen kinds of decisions in chosen projects, for up
 * to 90 days, optionally capped per day. Step-up happens once, here. Creating it shows the
 * delegate's key ONCE (BOSS keeps only a hash). BOSS never lets a delegate merge to main, deploy,
 * raise a budget or change identity, security or settings — whatever is chosen here.
 */
import React, { useState } from "react";
import { Plus, Prohibit, Stamp } from "@phosphor-icons/react";

import { idGet, idPost, refusalSentence, IdentityError } from "../../../lib/identity/api";
import type { IssuedSecret } from "../../../lib/identity/types";
import { CanButton, useGuardedCall } from "../../../components/identity/identity-provider";
import { Ago, ErrorLine, Pill, Sheet, shortDate } from "../../../components/identity/kit";
import { ListHeader } from "../../../components/identity/list-header";
import { EmptyState } from "../../../components/identity/states";
import { ShownOnce } from "../../identity/service-accounts/service-accounts";
import { showToast } from "../../client-forms";

export type ApprovalDelegation = {
  id: string; status: "active" | "revoked" | "expired";
  person: { id: string; name: string | null }; delegate: { id: string; name: string | null };
  key_id: string | null; kinds: string[]; kind_labels: string[]; projects: string[];
  extra_risk_tiers: number[]; max_risk_tier: number; daily_cap: number | null; used_today: number;
  purpose: string; created_at: string | null; expires_at: string | null; revoked_at: string | null;
  last_used_at: string | null;
};
export type DelegationKind = { id: string; label: string };
export type ApprovalDelegationsAnswer = {
  approval_delegations: ApprovalDelegation[]; kinds: DelegationKind[]; max_days: number; max_risk_tier: number;
};

/** What a delegate can never do, said once wherever a delegation is made or listed. */
export const NEVER = "Never merges to main, deploys, raises a budget, or changes identity, security or settings.";

export function delegationLine(d: ApprovalDelegation): string {
  const cap = d.daily_cap ? ` · up to ${d.daily_cap} a day (${d.used_today} today)` : "";
  const until = d.status === "revoked" ? "revoked" : d.expires_at ? `until ${shortDate(d.expires_at)}` : "";
  return `${d.kind_labels.join(", ")} · ${d.projects.join(", ")} · ${until}${cap}`;
}

export function ApproveOnMyBehalf({ initial, projects }: {
  initial: ApprovalDelegationsAnswer; projects: { id: string; name: string }[];
}) {
  const [answer, setAnswer] = useState(initial);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<IssuedSecret | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const reload = async () => setAnswer(await idGet<ApprovalDelegationsAnswer>("/v1/identity/me/approval-delegations"));
  const live = answer.approval_delegations.filter((d) => d.status === "active");
  const create = (
    <CanButton permission="service_account:manage" className="idn-btn primary sm" onClick={() => setCreating(true)}
      tip="Owners and Admins: a delegate approves chosen decisions for you, unattended" data-testid="new-approval-delegation">
      <Plus size={14} weight="bold" aria-hidden /> Approve on my behalf
    </CanButton>
  );
  return (
    <section className="idn-card" aria-labelledby="aomb-h" data-testid="approve-on-my-behalf">
      <ListHeader level={2} id="aomb-h" title="Approve on my behalf" sub={`${live.length} active`}
        empty={!answer.approval_delegations.length} create={create} />
      {answer.approval_delegations.length ? (
        <p className="idn-dim" data-copy="prose">A delegate key clears the gates you choose while you are away. {NEVER}</p>
      ) : (
        // ListHeader hides `create` while the list is empty: the empty state carries it, or the
        // first grant cannot be made at all (2026-10-28, boss-1: the card showed no button).
        <EmptyState icon={<Stamp size={20} />} title="No delegate approves for you yet" testId="approval-delegations-empty" action={create}>
          A delegate key clears the gates you choose while you are away. {NEVER}
        </EmptyState>
      )}
      {answer.approval_delegations.map((d) => (
        <article key={d.id} className="idn-dg" data-status={d.status} data-testid={`approval-delegation-${d.id}`}>
          <div className="idn-top">
            <Stamp size={22} aria-hidden />
            <span>
              <b data-copy="data">{d.delegate.name ?? "Approval delegate"}</b>
              <small data-copy="data">{delegationLine(d)} · used <Ago iso={d.last_used_at} /></small>
            </span>
            <span className="idn-spacer" />
            <Pill tone={d.status === "active" ? "ok" : "mute"}>{d.status}</Pill>
            {d.status === "active" ? (
              <button type="button" className="idn-btn sm danger" disabled={busy === d.id} aria-label={`Revoke ${d.delegate.name ?? d.id}`}
                onClick={async () => {
                  setBusy(d.id);
                  try {
                    await idPost(`/v1/identity/me/approval-delegations/${encodeURIComponent(d.id)}/revoke`, {});
                    showToast("success", "Revoked · the delegate stops now");
                    await reload();
                  } catch (e) { showToast("error", refusalSentence(e)); }
                  setBusy(null);
                }}>
                <Prohibit size={14} aria-hidden /> Revoke
              </button>
            ) : null}
          </div>
        </article>
      ))}
      {creating ? (
        <CreateSheet kinds={answer.kinds} maxDays={answer.max_days} maxTier={answer.max_risk_tier} projects={projects}
          onClose={() => setCreating(false)}
          onIssued={async (got) => { setCreating(false); setIssued(got); await reload(); }} />
      ) : null}
      {issued ? <ShownOnce issued={issued} account="approval delegate" onClose={() => setIssued(null)} /> : null}
    </section>
  );
}

function CreateSheet({ kinds, maxDays, maxTier, projects, onClose, onIssued }: {
  kinds: DelegationKind[]; maxDays: number; maxTier: number; projects: { id: string; name: string }[];
  onClose: () => void; onIssued: (issued: IssuedSecret) => void;
}) {
  const guarded = useGuardedCall();
  const [chosen, setChosen] = useState<string[]>(["prd"]);
  const [where, setWhere] = useState<string[]>(projects.length === 1 ? [projects[0].id] : []);
  const [days, setDays] = useState(30);
  const [cap, setCap] = useState<string>("");
  const [tier3, setTier3] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  return (
    <Sheet title="Approve on my behalf" icon={<Stamp size={18} aria-hidden />} onClose={onClose} testId="approval-delegation-sheet">
      <p className="idn-lede2" data-copy="prose">The delegate approves or rejects only what you tick, only in these projects, up to tier {maxTier}. {NEVER}</p>
      <fieldset className="idn-field"><legend className="idn-label">Decisions</legend>
        {kinds.map((k) => (
          <label key={k.id} className="idn-check"><input type="checkbox" checked={chosen.includes(k.id)}
            onChange={() => setChosen(toggle(chosen, k.id))} data-testid={`kind-${k.id}`} /> {k.label}</label>
        ))}
      </fieldset>
      <fieldset className="idn-field"><legend className="idn-label">Projects</legend>
        {projects.map((p) => (
          <label key={p.id} className="idn-check"><input type="checkbox" checked={where.includes(p.id)}
            onChange={() => setWhere(toggle(where, p.id))} data-testid={`project-${p.id}`} /> {p.name}</label>
        ))}
      </fieldset>
      <label className="idn-field"><span className="idn-label">Days (at most {maxDays})</span>
        <input className="idn-input" type="number" min={1} max={maxDays} value={days} data-testid="days"
          onChange={(e) => setDays(Math.max(1, Math.min(maxDays, Number(e.target.value) || 1)))} /></label>
      {maxTier < 3 ? (
        <label className="idn-field" title="Tier-3 decisions are the highest a delegate may take. Deploys, releases, budget and identity changes are never delegated.">
          <span><input type="checkbox" checked={tier3} data-testid="tier-3"
                       onChange={() => setTier3(!tier3)} /> Also tier-3 decisions (above tier {maxTier})</span></label>
      ) : null}
      <label className="idn-field"><span className="idn-label">At most this many a day (optional)</span>
        <input className="idn-input" type="number" min={1} value={cap} placeholder="no cap" data-testid="daily-cap"
          onChange={(e) => setCap(e.target.value)} /></label>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={busy || !chosen.length || !where.length} data-testid="approval-delegation-create"
          onClick={async () => {
            setBusy(true); setError(null);
            try {
              const got = await guarded("A delegate will approve for you without asking again.", () =>
                idPost<IssuedSecret>("/v1/identity/me/approval-delegations", {
                  kinds: chosen, projects: where, days, ...(cap ? { daily_cap: Number(cap) } : {}),
                  ...(tier3 ? { extra_risk_tiers: [3] } : {}),
                }));
              onIssued({ ...got, replaces: null });
            } catch (e) {
              if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(refusalSentence(e));
              setBusy(false);
            }
          }}>Create delegate key</button>
      </div>
    </Sheet>
  );
}
