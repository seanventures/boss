"use client";

/**
 * A PASSPORT (IDENTITY.md §7.1, §9.6): the most anyone may delegate to this agent (its ceiling , 
 * delegable permissions only; the rest are shown locked, "never delegable"), its external ceiling
 * on GitHub, spawn rules and depth, whether it may act on its own, its lineage, the grants that
 * name it, and Suspend / Resume / Revoke. A ceiling change is a diff the person saves with
 * step-up, and never more than they hold themselves (§6.11).
 */
import React, { useMemo, useState } from "react";
import { ArrowRight, LockSimple, Pause, Play, Prohibit, ShieldCheck, UserCircle, Warning } from "@phosphor-icons/react";

import { AgentAvatar } from "../../../agent-avatar";
import { CanButton, useGuardedCall, useIdentity } from "../../../../components/identity/identity-provider";
import { Ago, ErrorLine, Face, Perm, Pill, Sheet, shortDate } from "../../../../components/identity/kit";
import { EmptyState } from "../../../../components/identity/states";
import { neverDelegableWhy, permissionName, presetName } from "../../../../lib/identity/words";
import { GITHUB_KEYS, appMax, levelOffered, limitBody, limitChanges, type GithubLevel } from "../../../../lib/identity/github-limit";
import { resourceTitle } from "../../roles/catalogue";
import { plainText } from "../../../../lib/identity/plain";
import { PeoplePick, type PickPerson } from "../../service-accounts/people-pick";
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

/** Assign (or change) the person accountable for an agent: PATCH /v1/identity/agents/{id} {sponsor}. */
function SponsorSheet({ agent, people, onClose, onDone }: { agent: Passport; people: PickPerson[]; onClose: () => void; onDone: (p: Passport) => void }) {
  const { me } = useIdentity();
  const guarded = useGuardedCall();
  const list = me && !people.some((p) => p.id === me.principal_id)
    ? [{ id: me.principal_id, name: me.display_name, hue: 0, email: me.email }, ...people] : people;
  const [pick, setPick] = useState<string[]>(agent.sponsor ? [agent.sponsor.id] : []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title={agent.sponsor ? `Change ${agent.name}'s sponsor` : `Sponsor for ${agent.name}`} icon={<UserCircle size={18} aria-hidden />} onClose={onClose} testId="sponsor-sheet">
      <p className="idn-lede2" data-copy="prose">The sponsor is the person accountable for what this agent does. They are told about its grants and lifecycle.</p>
      <div className="idn-field">
        <span className="idn-label">Sponsor</span>
        <PeoplePick people={list} chosen={pick} onChange={(ids) => setPick(ids.slice(-1))} label="Choose a sponsor" single testId="sponsor-pick" />
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!pick.length || busy || pick[0] === agent.sponsor?.id} data-testid="sponsor-save"
          onClick={async () => {
            setBusy(true); setError(null);
            try {
              const next = await guarded(`A sponsor is accountable for ${agent.name}.`,
                () => idPatch<Passport>(`/v1/identity/agents/${encodeURIComponent(agent.id)}`, { sponsor: pick[0] }));
              showToast("success", "Sponsor set");
              onDone(next);
            } catch (e) {
              if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(e instanceof IdentityError ? e.message : "Not saved");
              setBusy(false);
            }
          }}>Save</button>
      </div>
    </Sheet>
  );
}

export function PassportScreen({ initial, grants, people = [] }: { initial: Passport; grants: Delegation[] | null; people?: PickPerson[] }) {
  const [agent, setAgent] = useState(initial);
  const [ceiling, setCeiling] = useState<Set<Permission>>(new Set(initial.ceiling));
  const [revoking, setRevoking] = useState(false);
  const [sponsoring, setSponsoring] = useState(false);
  const [saving, setSaving] = useState(false);
  const [onlyOn, setOnlyOn] = useState(true);
  // CHANGING THE LIMIT IS A MODE, not a side effect of clicking a chip: Change limit → switch
  // abilities → read what changes → Save limit (with step-up) or Cancel.
  const [editing, setEditing] = useState(false);
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
      const next = await guarded(`Changing ${agent.name}'s access limit changes what it can be given.`,
        () => idPatch<Passport>(`/v1/identity/agents/${encodeURIComponent(agent.id)}/passport`, { ceiling: [...ceiling] }));
      setAgent(next);
      setCeiling(new Set(next.ceiling));
      setEditing(false);
      showToast("success", "Access limit saved");
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
          <h1>{agent.name} <Pill tone={statusTone(agent.status)}>{agent.status}</Pill>{agent.sealed ? <span className="idn-tag" title="Its access limit changes only through a Passport edit">sealed</span> : null}</h1>
          <span className="idn-dim" title={`Agent ${agent.id}`}>{agent.title}</span>
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
            <h2 id="idn-ceiling-h" className="idn-h3" title="Nobody can delegate more than this to the agent">Access limit</h2>
            <span className="idn-sub idn-dim">{ceiling.size} allowed{agent.ceiling_preset ? `, from ${presetName(agent.ceiling_preset)}` : ""}</span>
            <span className="idn-spacer" />
            <button type="button" className="idn-fchip" aria-pressed={onlyOn} onClick={() => setOnlyOn((v) => !v)}>Only what it has</button>
            {!editing ? (
              <CanButton permission="agent_passport:manage" className="idn-btn sm primary" disabled={revoked} onClick={() => setEditing(true)}
                tip="Switch abilities on or off, then review and save" data-testid="edit-ceiling">Change limit</CanButton>
            ) : null}
          </div>
          <p className="idn-dim idn-small idn-mx-lede" data-copy="prose">
            {editing ? "Switch abilities on or off. Nothing changes until you save." : "The most anyone may let this agent do. A grant never gives it more."}
          </p>
          <div className="idn-mx-legend">
            <span><i className="idn-lg-on" />on</span>
            <span title="Agents never get these. A person merges, approves and changes access."><LockSimple size={11} aria-hidden /> only a person</span>
            {editing ? <span>dashed: you don't hold it</span> : null}
          </div>
          {visible.map((g) => (
            <div key={g.category}>
              <div className="idn-mx-cat">{g.category}</div>
              {g.resources.map((r) => (
                <div key={r.resource} className="idn-mx-row">
                  <span className="idn-res" title={r.resource}>{resourceTitle(r.resource)}</span>
                  <span className="idn-acts2">
                    {r.perms.map((p) => {
                      const meta = PERMISSION_META[p];
                      const on = ceiling.has(p);
                      const locked = !editing || !meta.delegable || !held(p) || revoked;
                      const change = on && !agent.ceiling.includes(p) ? "add" : !on && agent.ceiling.includes(p) ? "remove" : undefined;
                      return (
                        <button key={p} type="button" className="idn-tg" aria-pressed={on} data-locked={locked || undefined} data-change={change}
                          aria-disabled={locked || undefined} data-testid={`ceil-${p}`}
                          data-never={!meta.delegable || undefined}
                          title={!meta.delegable ? neverDelegableWhy(p, meta.title) : !editing ? meta.title : !held(p) ? "You don't hold this, so you can't give it" : meta.title}
                          aria-label={`${meta.title}${!meta.delegable ? `. ${neverDelegableWhy(p, meta.title)}` : ""}`}
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
          {editing ? (
            <div className="idn-ceil-save" data-testid="ceiling-preview">
              <div className="idn-diff">
                <b className="idn-small">{dirty ? "What changes when you save" : "No changes yet"}</b>
                {added.map((p) => <div key={p} className="idn-dl p"><b>+</b>{permissionName(p)}</div>)}
                {removed.map((p) => <div key={p} className="idn-dl m"><b>−</b>{permissionName(p)}</div>)}
                {dirty && removed.length ? <span className="idn-dim idn-small">Grants that gave it a removed ability lose it at once.</span> : null}
              </div>
              <div className="idn-save">
                <CanButton permission="agent_passport:manage" className="idn-btn primary" onClick={save} disabled={saving || !dirty} data-testid="save-ceiling">
                  <ShieldCheck size={15} aria-hidden /> {saving ? "Saving…" : "Save limit"}
                </CanButton>
                <button type="button" className="idn-btn" onClick={() => { setCeiling(new Set(agent.ceiling)); setEditing(false); }} data-testid="cancel-ceiling">Cancel</button>
              </div>
            </div>
          ) : null}
        </section>

        <div className="idn-colr">
          <section className="idn-card" aria-labelledby="idn-pass-h">
            <div className="idn-card-h"><h3 id="idn-pass-h">Passport</h3></div>
            <dl className="idn-kv">
              <dt>Sponsor</dt>
              <dd className="idn-row">
                {agent.sponsor ? <><Face name={agent.sponsor.name} tone={agent.sponsor.hue} size={22} /> {agent.sponsor.name}</> : <span className="idn-warn"><Warning size={13} aria-hidden /> No sponsor</span>}
                {!revoked ? (
                  <CanButton permission="agent_passport:manage" className={`idn-btn sm${agent.sponsor ? " ghost" : ""}`} onClick={() => setSponsoring(true)}
                    tip="The person accountable for this agent" data-testid="assign-sponsor">
                    {agent.sponsor ? "Change" : "Assign"}
                  </CanButton>
                ) : null}
              </dd>
              <dt>Starts others</dt>
              <dd>{agent.can_spawn ? (agent.allowed_children.length ? agent.allowed_children.join(", ") : "any allowed") : "no"}</dd>
              <dt>Max depth</dt><dd>{agent.max_depth}</dd>
              <dt>On its own</dt><dd>{agent.autonomous ? "yes, with its own roles" : "only for a person"}</dd>
              <dt>Last run</dt><dd><Ago iso={agent.last_run_at} /></dd>
            </dl>
          </section>

          <GithubLimit agent={agent} onSaved={setAgent} />

          <section className="idn-card" aria-labelledby="idn-grants-h">
            <div className="idn-card-h"><h3 id="idn-grants-h">Grants naming {agent.name}</h3><span className="idn-sub">{agent.active_grants} active</span></div>
            {grants === null ? (
              <div className="idn-row idn-dim"><LockSimple size={14} aria-hidden /> Needs <Perm p="delegation:read_all" sm /></div>
            ) : grants.length ? grants.map((g) => (
              <a key={g.id} className="idn-dg" href={`/identity/agents/delegations/${g.id}`}>
                <span className="idn-top">
                  <Face name={g.user.name} tone={g.user.hue} size={26} />
                  <span><b>{g.user.name}, {g.label}</b><small>{g.kind === "task" ? "this build only" : g.expires_at ? `until ${shortDate(g.expires_at)}` : "until revoked"}</small></span>
                  <span className="idn-spacer" />
                  <Pill tone={g.status === "active" ? "ok" : "mute"}>{g.status}</Pill>
                  <ArrowRight size={13} className="idn-dim" aria-hidden />
                </span>
              </a>
            )) : <EmptyState compact title="No grants name it" testId="passport-grants-empty">A person lets it act for them from My account › Agents.</EmptyState>}
          </section>

          <section className="idn-card" aria-labelledby="idn-lineage-h">
            <div className="idn-card-h"><h3 id="idn-lineage-h">Lineage</h3></div>
            <ol className="idn-lineage">
              {lineageLines(agent.lineage).map(({ line: l, times }) => (
                <li key={`${l.at}-${l.event}-${l.detail}`} className="idn-mini-ev">
                  <span className="idn-dim">{shortDate(l.at)}</span>
                  <span title={l.detail}><b>{plainText(l.event.replace(/_/g, " "))}</b> <span data-copy="data">{plainText(l.detail)}</span></span>
                  <span className="idn-dim">{times > 1 ? `×${times}` : ""}</span>
                </li>
              ))}
            </ol>
          </section>
        </div>
      </div>
      {sponsoring ? <SponsorSheet agent={agent} people={people} onClose={() => setSponsoring(false)} onDone={(p) => { setAgent(p); setSponsoring(false); }} /> : null}
      {revoking ? <RevokeSheet agent={agent} onClose={() => setRevoking(false)} onDone={(p) => { setAgent(p); setRevoking(false); }} /> : null}
    </>
  );
}

/** The same thing on the same day, said once with a count: a run's two tokens read as one
 *  "Started a run ×2", not two identical lines. */
export function lineageLines<L extends { at: string; event: string; detail: string }>(lines: readonly L[]): { line: L; times: number }[] {
  const out: { line: L; times: number }[] = [];
  for (const l of lines) {
    const last = out[out.length - 1];
    if (last && last.line.event === l.event && plainText(last.line.detail) === plainText(l.detail) && shortDate(last.line.at) === shortDate(l.at)) last.times += 1;
    else out.push({ line: l, times: 1 });
  }
  return out;
}

/**
 * THE AGENT'S GITHUB LIMIT, editable within the API's rules: closed keys, none/read/write, never
 * above what the BOSS GitHub App asks GitHub for (lib/identity/github-limit.ts). Saved with
 * PATCH …/passport {external_ceiling}, which needs Manage agent Passports and is audited.
 */
function GithubLimit({ agent, onSaved }: { agent: Passport; onSaved: (p: Passport) => void }) {
  const current = agent.external_ceiling.github ?? {};
  const levelOf = (k: string) => (current[k] ?? "none") as GithubLevel;
  const [editing, setEditing] = useState(false);
  const [levels, setLevels] = useState<Record<string, GithubLevel>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const draft = Object.fromEntries(GITHUB_KEYS.map(({ key }) => [key, levels[key] ?? levelOf(key)]));
  const changes = limitChanges(current, draft);
  const revoked = agent.status === "revoked";
  const save = async () => {
    setBusy(true); setError(null);
    try {
      const next = await guarded(`Changing ${agent.name}'s GitHub limit changes what its runs can do on GitHub.`,
        () => idPatch<Passport>(`/v1/identity/agents/${encodeURIComponent(agent.id)}/passport`, { external_ceiling: limitBody(current, draft) }));
      onSaved(next);
      setEditing(false); setLevels({});
      showToast("success", "GitHub limit saved");
    } catch (e) {
      if (!(e instanceof IdentityError && e.code === "step_up_cancelled")) setError(e instanceof IdentityError ? e.message : "Not saved");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="idn-card" aria-labelledby="idn-gh-h" data-testid="github-limit">
      <div className="idn-card-h">
        <h3 id="idn-gh-h">GitHub</h3>
        <span className="idn-sub" title="The most any of its runs may do on GitHub">limit</span>
        <span className="idn-spacer" />
        {!editing ? (
          <CanButton permission="agent_passport:manage" className="idn-btn sm" disabled={revoked} onClick={() => setEditing(true)}
            tip="Never above what the BOSS GitHub App can get" data-testid="edit-github-limit">Change</CanButton>
        ) : null}
      </div>
      <dl className="idn-kv">
        {GITHUB_KEYS.map(({ key, label, tip }) => (
          <React.Fragment key={key}>
            <dt title={tip}>{label}</dt>
            <dd>
              {editing ? (
                <span className="idn-segs idn-segs-sm" role="group" aria-label={label}>
                  {(["none", "read", "write"] as const).map((lv) => {
                    const offered = levelOffered(key, lv, levelOf(key));
                    return (
                      <button key={lv} type="button" aria-pressed={draft[key] === lv} disabled={!offered} aria-disabled={!offered || undefined}
                        title={offered ? undefined : `The BOSS GitHub App can get at most ${appMax(key) === "none" ? "nothing" : appMax(key)} here`}
                        onClick={() => setLevels((l) => ({ ...l, [key]: lv }))} data-testid={`gh-${key}-${lv}`}>
                        {lv === "none" ? "No access" : lv === "read" ? "Read" : "Write"}
                      </button>
                    );
                  })}
                </span>
              ) : <span className={levelOf(key) === "none" ? "idn-dim" : undefined}>{levelOf(key) === "none" ? "no access" : levelOf(key)}</span>}
            </dd>
          </React.Fragment>
        ))}
      </dl>
      {editing ? (
        <div className="idn-ceil-save" data-testid="github-limit-preview">
          <div className="idn-diff">
            <b className="idn-small">{changes.length ? "What changes when you save" : "No changes yet"}</b>
            {changes.map((c) => <div key={c} className="idn-dl">{c}</div>)}
          </div>
          <ErrorLine error={error} />
          <div className="idn-save">
            <CanButton permission="agent_passport:manage" className="idn-btn primary sm" disabled={busy || !changes.length} onClick={save} data-testid="save-github-limit">
              <ShieldCheck size={14} aria-hidden /> {busy ? "Saving…" : "Save limit"}
            </CanButton>
            <button type="button" className="idn-btn sm" onClick={() => { setEditing(false); setLevels({}); setError(null); }}>Cancel</button>
          </div>
        </div>
      ) : null}
      <div className="idn-gh-how" data-testid="github-how">
        <b>How this agent reaches GitHub</b>
        <p data-copy="prose">Through the BOSS GitHub App, or the GitHub account of the person it acts for. Never beyond this limit.</p>
        <span className="idn-row">
          <a className="idn-link" href="/?area=setup&focus=repository">Setup › GitHub access</a>
          <a className="idn-link" href="/account/agents">My account › Agents for me</a>
        </span>
      </div>
    </section>
  );
}
