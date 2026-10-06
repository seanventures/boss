"use client";

/**
 * A PASSPORT (IDENTITY.md §7.1, §9.6): the most anyone may delegate to this agent (its ceiling —
 * delegable permissions only; the rest are shown locked, "never delegable"), its external ceiling
 * on GitHub, spawn rules and depth, whether it may act on its own, its lineage, the grants that
 * name it, and Suspend / Resume / Revoke. A ceiling change is a diff the person saves with
 * step-up, and never more than they hold themselves (§6.11).
 */
import React, { useMemo, useState } from "react";
import { ArrowRight, LockSimple, Pause, Play, Prohibit, ShieldCheck, Warning } from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { CanButton, useGuardedCall, useIdentity } from "../../../../components/identity/identity-provider";
import { Ago, ErrorLine, Face, Pill, Sheet, shortDate } from "../../../../components/identity/kit";
import { IdentityError, idPatch, idPost } from "../../../../lib/identity/api";
import { PERMISSIONS, PERMISSION_META, type Permission } from "../../../../lib/identity/permissions.gen";
import type { Delegation, Passport } from "../../../../lib/identity/types";
import { showToast } from "../../../client-forms";
import { statusTone } from "../agents-screen";

type Group = { category: string; resources: { resource: string; perms: Permission[] }[] };

function groups(): Group[] {
  const out: Group[] = [];
  for (const p of PERMISSIONS) {
    const meta = PERMISSION_META[p];
    if (meta.systemOnly) continue;
    let g = out.find((x) => x.category === meta.category);
    if (!g) { g = { category: meta.category, resources: [] }; out.push(g); }
    const res = p.split(":")[0];
    let r = g.resources.find((x) => x.resource === res);
    if (!r) { r = { resource: res, perms: [] }; g.resources.push(r); }
    r.perms.push(p);
  }
  return out;
}

const ALL_GROUPS = groups();

function RevokeSheet({ agent, onClose, onDone }: { agent: Passport; onClose: () => void; onDone: (p: Passport) => void }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  return (
    <Sheet title={`Revoke ${agent.name}?`} icon={<Prohibit size={18} className="idn-bad" aria-hidden />} onClose={onClose} testId="revoke-passport">
      <p className="idn-lede2" data-copy="prose">Revoked is terminal. Grants naming {agent.name} end, and agents it started are suspended for review.</p>
      <div className="idn-f">
        <label htmlFor="revoke-name">Type {agent.name} to confirm</label>
        <input id="revoke-name" className="idn-i" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" data-autofocus />
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger solid" disabled={typed !== agent.name || busy}
          onClick={async () => {
            setBusy(true);
            try {
              onDone(await guarded(`Revoking ${agent.name} is permanent.`, () => idPost<Passport>(`/v1/identity/agents/${encodeURIComponent(agent.id)}/revoke`, {})));
              showToast("success", "Passport revoked");
            } catch (e) {
              setError(e instanceof IdentityError ? e.message : "Not revoked");
              setBusy(false);
            }
          }}>Revoke for good</button>
      </div>
    </Sheet>
  );
}

export function PassportScreen({ initial, grants }: { initial: Passport; grants: Delegation[] | null }) {
  const [agent, setAgent] = useState(initial);
  const [ceiling, setCeiling] = useState<Set<Permission>>(new Set(initial.ceiling));
  const [revoking, setRevoking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [onlyOn, setOnlyOn] = useState(true);
  const { org } = useIdentity();
  const guarded = useGuardedCall();
  const revoked = agent.status === "revoked";

  const added = [...ceiling].filter((p) => !agent.ceiling.includes(p));
  const removed = agent.ceiling.filter((p) => !ceiling.has(p));
  const dirty = added.length + removed.length > 0;
  const held = (p: Permission) => !org || org.permissions.has(p);

  const visible = useMemo(() => ALL_GROUPS.map((g) => ({
    ...g,
    resources: g.resources.filter((r) => !onlyOn || r.perms.some((p) => ceiling.has(p) || agent.ceiling.includes(p))),
  })).filter((g) => g.resources.length), [onlyOn, ceiling, agent.ceiling]);

  const lifecycle = async (action: "suspend" | "resume") => {
    try {
      const next = await guarded(`${action === "suspend" ? "Suspending halts" : "Resuming restarts"} ${agent.name}'s runs.`,
        () => idPost<Passport>(`/v1/identity/agents/${encodeURIComponent(agent.id)}/${action}`, {}));
      setAgent(next);
      showToast("success", action === "suspend" ? "Suspended" : "Resumed");
    } catch (e) {
      showToast("error", e instanceof IdentityError ? e.message : "Not changed");
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      const next = await guarded(`Changing ${agent.name}'s ceiling changes what it can be given.`,
        () => idPatch<Passport>(`/v1/identity/agents/${encodeURIComponent(agent.id)}/passport`, { ceiling: [...ceiling] }));
      setAgent(next);
      setCeiling(new Set(next.ceiling));
      showToast("success", "Ceiling saved");
    } catch (e) {
      showToast("error", e instanceof IdentityError ? e.message : "Not saved");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <header className="idn-dh">
        <AgentAvatar agent={agent.role_key} size={48} standalone state={agent.status === "active" ? "idle" : agent.status === "suspended" ? "paused" : "retired"} />
        <div>
          <nav className="idn-crumb" aria-label="Breadcrumb"><a href="/identity/agents">Agents</a><span aria-hidden>›</span><span aria-current="page">Passport</span></nav>
          <h1>{agent.name} <Pill tone={statusTone(agent.status)}>{agent.status}</Pill>{agent.sealed ? <span className="idn-tag" title="Its ceiling changes only through a Passport edit">sealed</span> : null}</h1>
          <span className="idn-dim">{agent.title} · <code className="idn-mono">{agent.id}</code></span>
        </div>
        <span className="idn-spacer" />
        <div className="idn-acts">
          {agent.status === "suspended" ? (
            <CanButton permission="agent_passport:suspend" className="idn-btn" onClick={() => lifecycle("resume")} disabled={revoked}><Play size={15} aria-hidden /> Resume</CanButton>
          ) : (
            <CanButton permission="agent_passport:suspend" className="idn-btn" onClick={() => lifecycle("suspend")} disabled={revoked} tip="Halts its runs and pauses its grants"><Pause size={15} aria-hidden /> Suspend</CanButton>
          )}
          <CanButton permission="agent_passport:revoke" className="idn-btn danger" onClick={() => setRevoking(true)} disabled={revoked} tip="Terminal: re-issue creates a new Passport"><Prohibit size={15} aria-hidden /> Revoke</CanButton>
        </div>
      </header>

      <div className="idn-cols">
        <section className="idn-mx" aria-labelledby="idn-ceiling-h">
          <div className="idn-mx-top">
            <h2 id="idn-ceiling-h" className="idn-h3">Ceiling</h2>
            <span className="idn-sub idn-dim">{ceiling.size} delegable{agent.ceiling_preset ? ` · from ${agent.ceiling_preset}` : ""}</span>
            <span className="idn-spacer" />
            <button type="button" className="idn-fchip" aria-pressed={onlyOn} onClick={() => setOnlyOn((v) => !v)}>Only what it has</button>
          </div>
          <div className="idn-mx-legend">
            <span><i className="idn-lg-on" />on</span><span><LockSimple size={11} aria-hidden /> never delegable</span><span>dashed: you don't hold it</span>
          </div>
          {visible.map((g) => (
            <div key={g.category}>
              <div className="idn-mx-cat">{g.category}</div>
              {g.resources.map((r) => (
                <div key={r.resource} className="idn-mx-row">
                  <span className="idn-res">{r.resource.replace(/_/g, " ")}<small>{r.resource}</small></span>
                  <span className="idn-acts2">
                    {r.perms.map((p) => {
                      const meta = PERMISSION_META[p];
                      const on = ceiling.has(p);
                      const locked = !meta.delegable || !held(p) || revoked;
                      const change = on && !agent.ceiling.includes(p) ? "add" : !on && agent.ceiling.includes(p) ? "remove" : undefined;
                      return (
                        <button key={p} type="button" className="idn-tg" aria-pressed={on} data-locked={locked || undefined} data-change={change}
                          aria-disabled={locked || undefined} data-testid={`ceil-${p}`}
                          title={!meta.delegable ? "Never delegable" : !held(p) ? "You don't hold this" : meta.title}
                          aria-label={`${p}${!meta.delegable ? ", never delegable" : ""}`}
                          onClick={() => {
                            if (locked) return;
                            const next = new Set(ceiling);
                            if (on) next.delete(p); else next.add(p);
                            setCeiling(next);
                          }}>
                          <span className="idn-sw" aria-hidden />{p.split(":")[1].replace(/_/g, " ")}
                          {!meta.delegable ? <LockSimple size={11} aria-hidden /> : null}
                        </button>
                      );
                    })}
                  </span>
                  <span className="idn-cnt">{r.perms.filter((p) => ceiling.has(p)).length}/{r.perms.length}</span>
                </div>
              ))}
            </div>
          ))}
          {dirty ? (
            <div className="idn-ceil-save">
              <div className="idn-diff">
                {added.map((p) => <div key={p} className="idn-dl p"><b>+</b>{p}</div>)}
                {removed.map((p) => <div key={p} className="idn-dl m"><b>−</b>{p}</div>)}
              </div>
              <div className="idn-save">
                <CanButton permission="agent_passport:manage" className="idn-btn primary" onClick={save} disabled={saving} data-testid="save-ceiling">
                  <ShieldCheck size={15} aria-hidden /> {saving ? "Saving…" : "Review and save"}
                </CanButton>
                <button type="button" className="idn-btn" onClick={() => setCeiling(new Set(agent.ceiling))}>Discard</button>
              </div>
            </div>
          ) : null}
        </section>

        <div className="idn-colr">
          <section className="idn-card" aria-labelledby="idn-pass-h">
            <div className="idn-card-h"><h3 id="idn-pass-h">Passport</h3></div>
            <dl className="idn-kv">
              <dt>Sponsor</dt>
              <dd>{agent.sponsor ? <><Face name={agent.sponsor.name} tone={agent.sponsor.hue} size={22} /> {agent.sponsor.name}</> : <span className="idn-warn"><Warning size={13} aria-hidden /> none · needs one</span>}</dd>
              <dt>GitHub</dt>
              <dd>{Object.entries(agent.external_ceiling.github ?? {}).map(([k, v]) => <span key={k} className="idn-chip">{k.replace(/_/g, " ")}: {v}</span>)}</dd>
              <dt>Starts others</dt>
              <dd>{agent.can_spawn ? (agent.allowed_children.length ? agent.allowed_children.join(", ") : "any allowed") : "no"}</dd>
              <dt>Max depth</dt><dd>{agent.max_depth}</dd>
              <dt>On its own</dt><dd>{agent.autonomous ? "yes, with its own roles" : "only for a person"}</dd>
              <dt>Last run</dt><dd><Ago iso={agent.last_run_at} /></dd>
            </dl>
          </section>

          <section className="idn-card" aria-labelledby="idn-grants-h">
            <div className="idn-card-h"><h3 id="idn-grants-h">Grants naming {agent.name}</h3><span className="idn-sub">{agent.active_grants} active</span></div>
            {grants === null ? (
              <div className="idn-row idn-dim"><LockSimple size={14} aria-hidden /> Needs <code className="idn-perm">delegation:read_all</code></div>
            ) : grants.length ? grants.map((g) => (
              <a key={g.id} className="idn-dg" href={`/identity/agents/delegations/${g.id}`}>
                <span className="idn-top">
                  <Face name={g.user.name} tone={g.user.hue} size={26} />
                  <span><b>{g.user.name} · {g.label}</b><small>{g.kind === "task" ? "this build only" : g.expires_at ? `until ${shortDate(g.expires_at)}` : "until revoked"}</small></span>
                  <span className="idn-spacer" />
                  <Pill tone={g.status === "active" ? "ok" : "mute"}>{g.status}</Pill>
                  <ArrowRight size={13} className="idn-dim" aria-hidden />
                </span>
              </a>
            )) : <span className="idn-dim">No grants name it</span>}
          </section>

          <section className="idn-card" aria-labelledby="idn-lineage-h">
            <div className="idn-card-h"><h3 id="idn-lineage-h">Lineage</h3></div>
            <ol className="idn-lineage">
              {agent.lineage.map((l) => (
                <li key={`${l.at}-${l.event}-${l.detail}`} className="idn-mini-ev">
                  <span className="idn-dim">{shortDate(l.at)}</span>
                  <span><b>{l.event.replace(/_/g, " ")}</b> <span className="idn-mono" data-copy="data">{l.detail}</span></span>
                  <span />
                </li>
              ))}
            </ol>
          </section>
        </div>
      </div>
      {revoking ? <RevokeSheet agent={agent} onClose={() => setRevoking(false)} onDone={(p) => { setAgent(p); setRevoking(false); }} /> : null}
    </>
  );
}
