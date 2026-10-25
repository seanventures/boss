"use client";

/**
 * MY ACCOUNT › AGENTS ACTING FOR ME (IDENTITY.md §7.3, §9.6): the grants you gave — which agents,
 * which abilities, where, until when, last used — each revocable, with the cascade said first.
 * "Let an agent act for me" creates a standing grant: never more than you hold, never a
 * permission no agent may hold. Plus your connections (GitHub) the vault keeps for you.
 */
import React, { useEffect, useMemo, useState } from "react";
import { ArrowsClockwise, Clock, GithubLogo, Plus, Prohibit, Robot, Warning } from "@phosphor-icons/react";

import { AgentAvatar } from "../../agent-avatar";
import { IdentityError, errorSentence, idDelete, idGet, idPost, refusalSentence } from "../../../lib/identity/api";
import { EmptyState } from "../../../components/identity/states";
import { ListHeader } from "../../../components/identity/list-header";
import { plainText } from "../../../lib/identity/plain";
import { PERMISSION_META, type Permission } from "../../../lib/identity/permissions.gen";
import type { CascadePreview, Connection, Delegation, Ref } from "../../../lib/identity/types";
import { CanButton, useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { Ago, ErrorLine, Perm, Pill, Sheet, shortDate, useNow } from "../../../components/identity/kit";
import { showToast } from "../../client-forms";
import { fetchSignin } from "../../../lib/identity/github-signin";
import { ConsentSheet } from "../../../components/identity/consent-sheet";
import type { ConsentRequest } from "../../../lib/identity/consent";

/** ONE SURFACE PER JOB: with "Sign in with GitHub" offered (the published BOSS app), connecting
 *  or reconnecting your GitHub here goes to that one sign-in in Repo and models. */
export const SIGNIN_HREF = "/?area=setup&focus=github";

/** Plain-English abilities, each a set of delegable permissions (§7.3 "abilities"). */
export const ABILITIES: { id: string; label: string; tip: string; permissions: Permission[] }[] = [
  // Everything a build needs to run (engine identity/consent.py CODE_TASK_PERMISSIONS), so a grant
  // made here covers the build it was made for: the build's agents also read the repository and
  // push its branch.
  { id: "build", label: "Build and steer code", tip: "Start builds, steer and retry them", permissions: ["code_task:read", "code_task:create", "code_task:steer", "code_task:retry", "repository:read", "github:read", "github:sync"] },
  { id: "previews", label: "Run previews", tip: "Start and stop preview apps", permissions: ["preview:read", "preview:manage"] },
  { id: "github", label: "Sync with GitHub", tip: "Push branches and open pull requests", permissions: ["repository:read", "github:read", "github:sync"] },
  { id: "workflows", label: "Run workflows", tip: "Start workflow runs and read them", permissions: ["workflow:read", "workflow_run:read", "workflow_run:start"] },
  { id: "read", label: "Read the factory", tip: "See builds, artifacts and runs", permissions: ["code_task:read", "artifact:read", "workflow_run:read"] },
];

const FALLBACK_AGENTS: Ref[] = [
  { id: "agt_atlas", name: "Atlas" }, { id: "agt_forge", name: "Forge" }, { id: "agt_vera", name: "Vera" },
  { id: "agt_juno", name: "Juno" }, { id: "agt_rook", name: "Rook" }, { id: "agt_harbor", name: "Harbor" },
];

const roleKey = (id: string) => id.replace(/^agt_/, "");

function said(e: unknown): string {
  return refusalSentence(e);
}

function statusPill(d: Delegation, now: number) {
  if (d.status === "revoked") return <Pill tone="mute">revoked</Pill>;
  if (d.status === "expired" || (d.expires_at && Date.parse(d.expires_at) < now)) return <Pill tone="mute">expired</Pill>;
  if (d.status === "paused") return <Pill tone="warn">paused</Pill>;
  return <Pill tone="ok">active</Pill>;
}

/** What GitHub's redirect back said (`?connected=github` or `?connect_error=<code>`). */
export type ConnectOutcome = { connected: string | null; error: string | null };

const CONNECT_ERRORS: Record<string, string> = {
  state_invalid: "The approval expired. Connect again.",
  access_denied: "GitHub approval was cancelled.",
  people_only: "Only a person can connect GitHub.",
  not_configured: "GitHub isn't set up on this BOSS yet. Ask an Admin.",
  github_not_configured: "GitHub isn't set up on this BOSS yet. Ask an Admin.",
};

export function connectOutcomeLine(o: ConnectOutcome | undefined): { tone: "ok" | "bad"; text: string } | null {
  if (!o) return null;
  if (o.error) return { tone: "bad", text: CONNECT_ERRORS[o.error] ?? "GitHub did not connect. Try again." };
  if (o.connected) return { tone: "ok", text: o.connected.toLowerCase() === "github" ? "GitHub connected." : "Connected." };
  return null;
}

export function AgentsForMe({ delegations: initial, agents, connections, untilRevokedAllowed, openNew, connectOutcome, connectionsFailed = false,
  consentAsk = null, consentLink = "none" }: {
  delegations: Delegation[]; agents: Ref[] | null; connections: Connection[]; untilRevokedAllowed: boolean; openNew: boolean;
  connectOutcome?: ConnectOutcome;
  /** Your connections could not be read: said, never shown as "not connected". */
  connectionsFailed?: boolean;
  /** Opened from a consent link (/account/agents?consent=<id>): the request an agent turn, Metatron
   *  or a key met, shown in the same consent sheet the console opens in place. */
  consentAsk?: ConsentRequest | null;
  /** "gone": the link's request expired, was not yours, or never existed. */
  consentLink?: "none" | "open" | "gone";
}) {
  const now = useNow();
  const [delegations, setDelegations] = useState(initial);
  const [granting, setGranting] = useState(false);
  const [revoking, setRevoking] = useState<Delegation | null>(null);
  const [asking, setAsking] = useState<ConsentRequest | null>(consentAsk);
  const [consented, setConsented] = useState<"granted" | "declined" | null>(null);
  const { check } = useIdentity();

  useEffect(() => { if (openNew && check("delegation:grant").allowed) setGranting(true); }, [openNew, check]);
  const reload = async () => setDelegations((await idGet<{ delegations: Delegation[] }>("/v1/identity/me/delegations")).delegations);
  const live = delegations.filter((d) => d.status === "active" || d.status === "paused");
  const past = delegations.filter((d) => !(d.status === "active" || d.status === "paused"));
  const grant = (
    <CanButton permission="delegation:grant" className="idn-btn primary sm" onClick={() => setGranting(true)} tip="Choose agents, abilities and an expiry" data-testid="new-grant">
      <Plus size={14} weight="bold" aria-hidden /> Let an agent act for me
    </CanButton>
  );

  const linkLine = consented === "granted"
    ? "Consent given. Go back to where you asked and run it again: in Metatron, /pick the same choice."
    : consented === "declined" ? "Nothing was granted. The agents won't act for you on it."
      : consentLink === "gone" ? "That consent link has expired or isn't yours. Ask again where you started it." : null;

  return (
    <div className="idn-cols">
      {linkLine ? (
        <div className={consented === "granted" ? "idn-ok idn-row idn-full" : "idn-dim idn-row idn-full"} role="status" data-testid="consent-link-outcome">{linkLine}</div>
      ) : null}
      <section className="idn-card" aria-labelledby="afm-h">
        <ListHeader level={2} id="afm-h" title="Agents acting for me" sub={`${live.length} active`} empty={!delegations.length} create={grant} />
        {!delegations.length ? (
          <EmptyState icon={<Robot size={20} />} title="No agent acts for you yet" testId="grants-empty" action={grant}>
            Let agents build, preview or sync for you, with part of your access, until a date you choose.
          </EmptyState>
        ) : null}
        {[...live, ...past].map((d) => (
          <article key={d.id} className="idn-dg" data-status={d.status} data-testid={`grant-${d.id}`}>
            <div className="idn-top">
              <span className="idn-stack" aria-hidden>{d.agents.slice(0, 4).map((a) => <AgentAvatar key={a.id} agent={roleKey(a.id)} size={30} />)}</span>
              <span>
                <b data-copy="data">{d.agents.map((a) => a.name).join(", ")} · {d.label}</b>
                <small data-copy="data">
                  {d.kind === "task" ? "this build only" : d.expires_at ? `until ${shortDate(d.expires_at)}` : "until revoked"}
                  {d.projects.length ? ` · ${d.projects.join(", ")}` : ""} · used <Ago iso={d.last_used_at} />
                </small>
              </span>
              <span className="idn-spacer" />
              {statusPill(d, now)}
              {d.status === "active" || d.status === "paused" ? (
                <button type="button" className="idn-btn sm danger" onClick={() => setRevoking(d)} aria-label={`Revoke ${d.label}`}>
                  <Prohibit size={14} aria-hidden /> Revoke
                </button>
              ) : null}
            </div>
            <div className="idn-perms">
              {d.permissions.slice(0, 6).map((p) => <Perm key={p} p={p} sm />)}
              {d.permissions.length > 6 ? <span className="idn-chip">+{d.permissions.length - 6}</span> : null}
              {d.github ? <span className="idn-chip"><GithubLogo size={12} aria-hidden /> {d.github.repos[0]}{d.github.repos.length > 1 ? ` +${d.github.repos.length - 1}` : ""}{d.github.ref_rule ? ` · ${d.github.ref_rule}` : ""}</span> : null}
            </div>
          </article>
        ))}
      </section>

      <div className="idn-colr">
        <Connections connections={connections} outcome={connectOutcome} failed={connectionsFailed} />
        <section className="idn-card">
          <div className="idn-card-h"><h2>What agents never get</h2></div>
          <p className="idn-dim idn-prose" data-copy="prose">Approvals, identity, secrets, production deploys and gate waivers. Whatever you grant, they stay yours.</p>
        </section>
      </div>

      {granting ? (
        <GrantSheet agents={agents ?? FALLBACK_AGENTS} untilRevokedAllowed={untilRevokedAllowed}
          onClose={() => setGranting(false)} onDone={() => { setGranting(false); reload().catch(() => undefined); }} />
      ) : null}
      {asking ? (
        <ConsentSheet
          request={{ ...asking, onDecided: (id) => { setConsented(id ? "granted" : "declined"); if (id) reload().catch(() => undefined); } }}
          onClose={() => setAsking(null)} />
      ) : null}
      {revoking ? <RevokeSheet grant={revoking} onClose={() => setRevoking(null)} onDone={() => { setRevoking(null); reload().catch(() => undefined); }} /> : null}
    </div>
  );
}

function Connections({ connections: initial, outcome, failed = false }: { connections: Connection[]; outcome?: ConnectOutcome; failed?: boolean }) {
  const guarded = useGuardedCall();
  const [connections, setConnections] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const outcomeLine = connectOutcomeLine(outcome);
  const github = connections.find((c) => c.provider.toLowerCase() === "github" && c.status !== "revoked");
  const [deviceFlow, setDeviceFlow] = useState(false);
  useEffect(() => {
    let live = true;
    fetchSignin().then((s) => { if (live) setDeviceFlow(Boolean(s?.configured)); }).catch(() => undefined);
    return () => { live = false; };
  }, []);
  const connect = () => deviceFlow ? Promise.resolve(window.location.assign(SIGNIN_HREF))
    : guarded("Connecting GitHub stores a token for you.", () => idPost<{ redirect: string }>("/v1/identity/me/connections", { provider: "github" }))
      .then((r) => window.location.assign(r.redirect)).catch((e) => setError(said(e)));
  const disconnect = async (c: Connection) => {
    setBusy(true); setError(null);
    try {
      await guarded("Disconnecting removes the GitHub token BOSS keeps for you.", () => idDelete(`/v1/identity/me/connections/${encodeURIComponent(c.id)}`));
      setConnections((xs) => xs.map((x) => (x.id === c.id ? { ...x, status: "revoked" as const } : x)));
      showToast("success", "GitHub disconnected");
    } catch (e) { setError(said(e)); }
    finally { setBusy(false); }
  };
  return (
    <section className="idn-card" aria-labelledby="conn-h">
      <div className="idn-card-h"><h2 id="conn-h">Connections</h2><span className="idn-sub">Kept in the vault</span></div>
      <div className="idn-fac">
        <span className="idn-d" data-kind="sso"><GithubLogo size={16} aria-hidden /></span>
        <span>
          <b>GitHub</b>
          <small data-copy="data">{github ? <>{github.account} · linked <Ago iso={github.linked_at} /></> : "For “as me on GitHub” grants"}</small>
        </span>
        {failed ? <Pill tone="warn" tip="Your connections could not be read">unknown</Pill>
          : github ? (
            <span className="idn-row idn-nowrap">
              {github.status === "needs_reauth" ? (
                <CanButton permission="connection:use_own" className="idn-btn sm primary" data-testid="reconnect-github" tip="GitHub asked to approve BOSS again" onClick={() => void connect()}>
                  Reconnect
                </CanButton>
              ) : <Pill tone="ok">connected</Pill>}
              <button type="button" className="idn-btn sm ghost" disabled={busy} data-testid="disconnect-github" title="Removes the token BOSS keeps for you" onClick={() => void disconnect(github)}>
                Disconnect
              </button>
            </span>
          ) : (
            <CanButton permission="connection:use_own" className="idn-btn sm" data-testid="connect-github" tip="Opens GitHub to approve" onClick={() => void connect()}>
              Connect GitHub
            </CanButton>
          )}
      </div>
      {github?.status === "needs_reauth" ? <p className="idn-warn idn-small" data-copy="prose">GitHub needs your approval again before agents can act as you there.</p> : null}
      {failed ? <p className="idn-dim idn-small" data-copy="prose">Your connections could not be read. Reload to try again.</p> : null}
      {outcomeLine ? <div className={outcomeLine.tone === "ok" ? "idn-ok idn-row" : "idn-bad idn-row"} role="status" data-testid="connect-outcome">{outcomeLine.text}</div> : null}
      <ErrorLine error={error} />
    </section>
  );
}

/** A project the person may pick (GET /v1/projects). */
type ProjectChoice = { id: string; name: string };
/** The GitHub access a build or a sync needs on its repositories (engine identity/consent.py
 *  CONSENT_GITHUB_NEEDS): push to boss/* branches and open pull requests. */
export const GITHUB_NEEDS = { contents: "write", pull_requests: "write" } as const;
/** Abilities whose agents push to GitHub, so the grant names repositories. */
const GITHUB_ABILITIES = new Set(["build", "github"]);

/**
 * The grant body the sheet sends: project IDS (picked by name), and for an ability that pushes,
 * the picked repositories with the access they need as `github` (grants._validate_github). The
 * Projects field was free text split on spaces, so "NorthPeak Bank" posted ["NorthPeak","Bank"]
 * and the API refused it; it never named a repository, so a build it covered could not push.
 */
export function grantBody(input: { agents: string[]; permissions: Permission[]; projects: string[]; repos: string[];
  pushes: boolean; days: number | null; purpose: string }) {
  return {
    agents: input.agents, permissions: input.permissions, projects: input.projects, kind: "standing" as const,
    days: input.days, purpose: input.purpose,
    ...(input.pushes && input.repos.length ? { github: { repos: input.repos, ...GITHUB_NEEDS } } : {}),
  };
}

function GrantSheet({ agents, untilRevokedAllowed, onClose, onDone }: {
  agents: Ref[]; untilRevokedAllowed: boolean; onClose: () => void; onDone: () => void;
}) {
  const { org, projectId } = useIdentity();
  const guarded = useGuardedCall();
  const held = org?.permissions;
  const [picked, setPicked] = useState<string[]>([]);
  const [abilities, setAbilities] = useState<string[]>(["build"]);
  const [projects, setProjects] = useState<ProjectChoice[] | null>(null);
  const [projectsFailed, setProjectsFailed] = useState(false);
  const [chosen, setChosen] = useState<string[]>(projectId ? [projectId] : []);
  // Each picked project's registered repositories; a repo is checked unless the person unticks it.
  const [repos, setRepos] = useState<Record<string, string[] | "failed">>({});
  const [unticked, setUnticked] = useState<string[]>([]);
  const [days, setDays] = useState<number | null>(30);
  const [purpose, setPurpose] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    idGet<{ items: { id: string; name: string; archived_at?: string | null }[] }>("/v1/projects")
      .then((r) => {
        if (!live) return;
        const list = (r.items ?? []).filter((p) => !p.archived_at).map((p) => ({ id: p.id, name: p.name || p.id }));
        setProjects(list);
        // The current project by default; with only one project, that one.
        setChosen((c) => (c.length ? c.filter((id) => list.some((p) => p.id === id)) : list.length === 1 ? [list[0].id] : []));
      })
      .catch(() => { if (live) setProjectsFailed(true); });
    return () => { live = false; };
  }, []);

  const pushes = abilities.some((a) => GITHUB_ABILITIES.has(a));
  useEffect(() => {
    if (!pushes) return;
    let live = true;
    for (const id of chosen.filter((p) => !(p in repos))) {
      idGet<{ repositories: { full_name?: string; archived?: boolean }[] }>(`/v1/projects/${encodeURIComponent(id)}/repositories`)
        .then((r) => { if (live) setRepos((m) => ({ ...m, [id]: (r.repositories ?? []).flatMap((x) => (x.full_name && !x.archived ? [x.full_name] : [])) })); })
        .catch(() => { if (live) setRepos((m) => ({ ...m, [id]: "failed" })); });
    }
    return () => { live = false; };
  }, [pushes, chosen]); // eslint-disable-line react-hooks/exhaustive-deps

  const usable = (p: Permission) => PERMISSION_META[p].delegable && (!held || held.has(p));
  const permissions = useMemo(() => [...new Set(ABILITIES.filter((a) => abilities.includes(a.id)).flatMap((a) => a.permissions))].filter(usable),
    [abilities, held]); // eslint-disable-line react-hooks/exhaustive-deps
  const nameOf = (id: string) => projects?.find((p) => p.id === id)?.name ?? id;
  const offered = [...new Set(chosen.flatMap((id) => (Array.isArray(repos[id]) ? repos[id] as string[] : [])))];
  const selectedRepos = offered.filter((r) => !unticked.includes(r));
  const ready = picked.length > 0 && permissions.length > 0 && chosen.length > 0;
  const first = ABILITIES.find((a) => abilities.includes(a.id));
  const placeholder = `${first ? first.label : "Act for me"}${chosen.length ? ` in ${nameOf(chosen[0])}` : ""}`;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await guarded("Letting agents act for you.", () => idPost("/v1/identity/me/delegations", grantBody({
        agents: picked, permissions, projects: chosen, repos: selectedRepos, pushes, days, purpose: purpose.trim() || placeholder,
      })));
      showToast("success", "Grant created");
      onDone();
    } catch (e) {
      // The refusal names the project by id; the person picked it by name.
      setError(e instanceof IdentityError && typeof e.body.project === "string"
        ? errorSentence(e.status, e.code, e.permission, { ...e.body, project_name: nameOf(e.body.project) })
        : said(e));
      setBusy(false);
    }
  };

  return (
    <Sheet title="Let an agent act for me" icon={<Robot size={18} aria-hidden />} onClose={onClose} wide testId="grant-sheet">
      <p className="idn-lede2" data-copy="prose">They act with your access, never more, and every action says it was them, for you.</p>
      <div className="idn-field">
        <span className="idn-label" id="grant-agents">Agents</span>
        <div className="idn-segs" role="group" aria-labelledby="grant-agents">
          {agents.map((a) => (
            <button key={a.id} type="button" aria-pressed={picked.includes(a.id)} className="idn-agent-pick"
              onClick={() => setPicked((xs) => (xs.includes(a.id) ? xs.filter((x) => x !== a.id) : [...xs, a.id]))}>
              <AgentAvatar agent={roleKey(a.id)} size={22} /> {a.name}
            </button>
          ))}
        </div>
      </div>
      <div className="idn-field">
        <span className="idn-label" id="grant-abilities">Abilities</span>
        <div className="idn-segs" role="group" aria-labelledby="grant-abilities">
          {ABILITIES.map((a) => {
            const ok = a.permissions.some(usable);
            return (
              <button key={a.id} type="button" aria-pressed={abilities.includes(a.id)} aria-disabled={!ok || undefined}
                title={ok ? a.tip : "You don't hold this"}
                onClick={() => { if (ok) setAbilities((xs) => (xs.includes(a.id) ? xs.filter((x) => x !== a.id) : [...xs, a.id])); }}>
                {a.label}
              </button>
            );
          })}
        </div>
        <div className="idn-need" aria-label="Permissions granted">{permissions.map((p) => <Perm key={p} p={p} sm />)}</div>
      </div>
      <div className="idn-field">
        <span className="idn-label" id="grant-projects">Projects</span>
        {projects ? (
          projects.length ? (
            <div className="idn-segs" role="group" aria-labelledby="grant-projects" data-testid="grant-projects">
              {projects.map((p) => (
                <button key={p.id} type="button" aria-pressed={chosen.includes(p.id)} data-project={p.id}
                  onClick={() => setChosen((xs) => (xs.includes(p.id) ? xs.filter((x) => x !== p.id) : [...xs, p.id]))}>
                  {p.name}
                </button>
              ))}
            </div>
          ) : <p className="idn-dim idn-small" data-copy="prose">No project yet. Create one first.</p>
        ) : projectsFailed ? (
          <p className="idn-bad idn-small" role="status" data-testid="grant-projects-failed">Your projects could not be read. Reload to try again.</p>
        ) : <div className="idn-row idn-dim" role="status"><span className="idn-blink" aria-hidden /> Loading projects…</div>}
      </div>
      {pushes && chosen.length ? (
        <div className="idn-field" data-testid="grant-repos">
          <span className="idn-label" id="grant-repos-h">Repositories</span>
          {chosen.map((id) => {
            const list = repos[id];
            if (list === undefined) return <div key={id} className="idn-row idn-dim" role="status"><span className="idn-blink" aria-hidden /> Reading {nameOf(id)}…</div>;
            if (list === "failed") return <p key={id} className="idn-bad idn-small" role="status">{nameOf(id)}: its repositories could not be read.</p>;
            if (!list.length) return <p key={id} className="idn-dim idn-small" data-copy="prose">{nameOf(id)} has no repository yet, so agents can&apos;t push there.</p>;
            return (
              <div key={id} className="idn-repos" role="group" aria-labelledby="grant-repos-h">
                {list.map((r) => (
                  <label key={r} className="idn-check">
                    <input type="checkbox" checked={!unticked.includes(r)} data-repo={r}
                      onChange={(e) => setUnticked((xs) => (e.target.checked ? xs.filter((x) => x !== r) : [...xs, r]))} />
                    <GithubLogo size={14} aria-hidden /> <span data-user-text>{r}</span>
                  </label>
                ))}
              </div>
            );
          })}
          <div className="idn-need" aria-label="GitHub access granted">
            <span className="idn-chip" title="Push to boss/* branches only"><GithubLogo size={12} aria-hidden /> Code: write</span>
            <span className="idn-chip"><GithubLogo size={12} aria-hidden /> Pull requests: write</span>
          </div>
        </div>
      ) : null}
      <div className="idn-form">
        <div className="idn-field">
          <span className="idn-label" id="grant-expiry">Expires</span>
          <div className="idn-segs" role="radiogroup" aria-labelledby="grant-expiry">
            {[7, 30, 90].map((n) => (
              <button key={n} type="button" role="radio" aria-checked={days === n} aria-pressed={days === n} onClick={() => setDays(n)}>{n} days</button>
            ))}
            <button type="button" role="radio" aria-checked={days === null} aria-pressed={days === null} aria-disabled={!untilRevokedAllowed || undefined}
              title={untilRevokedAllowed ? "Until you revoke it" : "Policy needs an expiry"} onClick={() => { if (untilRevokedAllowed) setDays(null); }}>
              Until revoked
            </button>
          </div>
        </div>
        <div className="idn-field idn-full">
          <label htmlFor="grant-purpose">Purpose</label>
          <input id="grant-purpose" className="idn-input" placeholder={placeholder} value={purpose} maxLength={120} onChange={(e) => setPurpose(e.target.value)} />
        </div>
      </div>
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-dim"><Clock size={14} aria-hidden /> {days ? `Reminder ${days > 3 ? "3 days before" : "the day before"}` : "No expiry"}</span>
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!ready || busy} onClick={submit} data-testid="grant-submit">{busy ? "Granting…" : "Grant"}</button>
      </div>
    </Sheet>
  );
}

function RevokeSheet({ grant, onClose, onDone }: { grant: Delegation; onClose: () => void; onDone: () => void }) {
  const guarded = useGuardedCall();
  const [preview, setPreview] = useState<CascadePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    idPost<CascadePreview>(`/v1/identity/me/delegations/${encodeURIComponent(grant.id)}/revoke`, { dry_run: true })
      .then(setPreview).catch((e) => setError(said(e)));
  }, [grant.id]);
  const halts = preview?.halts ?? 0;
  return (
    <Sheet title="Revoke this grant?" icon={<Warning size={18} className="idn-bad" aria-hidden />} onClose={onClose} testId="revoke-sheet">
      <p className="idn-lede2" data-copy="prose">Their authority leaves every agent in the chain within 5 seconds. Work stops where it is; you can grant again to resume.</p>
      {preview ? (
        <ul className="idn-plain" aria-label="What revoking does" data-testid="cascade">
          {preview.items.map((i, n) => (
            <li key={`${i.kind}-${n}`} className="idn-cx">
              <span className="idn-i" aria-hidden>{i.kind === "runs" ? <Prohibit size={14} /> : <ArrowsClockwise size={14} />}</span>
              <span><span>{i.label}</span><small data-copy="data" title={i.detail}>{plainText(i.detail)}</small></span>
              <span className="idn-n">{i.count}</span>
            </li>
          ))}
        </ul>
      ) : !error ? <div className="idn-row idn-dim" role="status"><span className="idn-blink" aria-hidden /> Working out what stops…</div> : null}
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn danger solid" disabled={!preview || busy} data-testid="revoke-confirm"
          onClick={async () => {
            setBusy(true);
            try {
              await guarded("Revoking a grant halts its agents.", () => idPost(`/v1/identity/me/delegations/${encodeURIComponent(grant.id)}/revoke`, {}));
              showToast("success", "Grant revoked");
              onDone();
            } catch (e) { setError(said(e)); setBusy(false); }
          }}>
          <Prohibit size={14} aria-hidden /> {halts ? `Revoke and halt ${halts} ${halts === 1 ? "run" : "runs"}` : "Revoke"}
        </button>
      </div>
    </Sheet>
  );
}
