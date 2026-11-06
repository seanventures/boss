"use client";

/**
 * HOW BUILDS REACH GITHUB (GET /v1/identity/github). Under enforce a build's sandbox holds no GitHub
 * credential: boss-egress injects one per request and applies every rule (run branch only, no merges,
 * no admin). This row says which credential egress uses and sets one up:
 *
 *   GitHub App        recommended: created from a manifest, short-lived tokens per build, its repos only
 *   As me             a person's own connection, for runs whose grant says "as me on GitHub"
 *   Fine-grained token less safe: long-lived, acts as whoever made it; kept in the vault, only for
 *                     the repos picked in Repository above (pasting one there puts it here)
 *
 * An install that still carries BOSS_GITHUB_TOKEN gets one button that moves it into the vault.
 * GitHub's redirects land back here with `?github=app_connected|app_updated|app_failed`.
 * An update may also name `lost` repositories that an app still uses.
 */
import { GatedButton } from "../../components/gated";
import React, { forwardRef, useCallback, useEffect, useRef, useState } from "react";
import { CircleNotch, GithubLogo } from "@phosphor-icons/react";

import { IdentityError, idDelete, idGet, idPost } from "../../lib/identity/api";
import { HANDOFF_KEY, HANDOFF_PATH } from "../../lib/identity/github-manifest";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { HelpTip } from "../help-tip";
import { SectionLabel } from "../section-label";
import { toast, useBoard } from "./parts";
import { useOnScreen } from "../../lib/area-shown";
import { isAborted } from "../../lib/api-write";
import { Dot, Out, Tip } from "./pill";

export type GitHubMode = "app" | "token" | "as_me" | "none";
type AccountType = "User" | "Organization" | null;
/** Every GitHub page for the App, built on the server from the account type GitHub reported. */
export type AppLinks = { app?: string | null; app_settings?: string | null; permissions?: string | null;
                         install?: string | null; installation?: string | null };
/** What GitHub last said the installation was ACTUALLY granted (POST /v1/identity/github/app/verify). */
export type Verified = {
  state: "ok" | "not_installed" | "installation_gone" | "app_gone" | "unreachable" | "no_app";
  ok?: boolean; checked_at?: number;
  account?: { login: string | null; type: AccountType };
  permissions?: { name: string; wanted: string; granted: string | null;
                  state: "accepted" | "waiting" | "app_lacks"; fix_url: string | null }[];
  repos?: { full_name: string; installed: boolean | null; fix_url: string | null }[];
  installed_on?: string[] | null; repository_selection?: string | null;
  links?: AppLinks;
};
export type GitHubStatus = {
  mode: GitHubMode;
  /** The row's pill, decided by the server. */
  verdict: { tone: Tone; label: string; tip: string };
  app: { configured: boolean; installed: boolean; slug: string | null; html_url: string | null;
         account: string | null; source: "env" | "setup" | null;
         account_type?: AccountType; owner?: string | null; owner_type?: AccountType;
         links?: AppLinks; verified?: Verified | null };
  token: { repos: string[]; login: string | null; migrated: boolean } | null;
  people_connected: number;
  env_token: { present: boolean; read_by_builds: boolean };
};
type Installation = { id: string; account: string | null; account_type?: AccountType;
                      repository_selection: string | null };
type Tone = "green" | "amber" | "red" | "grey";

const SANDBOX_TIP = "Egress adds the credential to each request. The build's sandbox never holds it.";
const LOST_REPO = /^[a-z0-9_.-]+\/[a-z0-9_.-]+$/i;

/** Repositories named on the update redirect. Anything else in the query is ignored. */
function lostRepos(raw: string | null): string[] {
  if (!raw) return [];
  const names: string[] = [];
  for (const part of raw.split(",")) {
    const name = part.trim();
    if (LOST_REPO.test(name) && names.length < 20) names.push(name);
  }
  return names;
}

const FAILED_WORDS: Record<string, string> = {
  bad_state: "The link expired or was used. Start again.",
  state_expired: "That took over 10 minutes. Start again.",
  state_replayed: "That link was already used.",
  manifest_conversion_failed: "GitHub did not hand over the App. Start again.",
  not_this_apps_installation: "That installation belongs to another App.",
  github_account_not_found: "GitHub has no account by that name. Leave it blank for your own account.",
};

/** Said after "Test all" so the row asks GitHub again (setup-board.tsx dispatches it). */
export const SETUP_TESTED_EVENT = "boss:setup-tested";
/** Said by whichever control just asked GitHub (this row, the CI switch), so every other control
 *  re-reads the ONE stored answer instead of showing an older one. detail.from names the sayer. */
export const GITHUB_VERIFIED_EVENT = "boss:github-verified";
export function sayVerified(from: string): void {
  try { window.dispatchEvent(new CustomEvent(GITHUB_VERIFIED_EVENT, { detail: { from } })); } catch { /* no window */ }
}
/** A stored answer older than this is asked again when the page opens. */
const VERIFY_STALE_S = 600;

const PERMISSION_WORDS: Record<string, string> = {
  contents: "Contents", pull_requests: "Pull requests", workflows: "Workflows", checks: "Checks",
  statuses: "Commit statuses", metadata: "Metadata", issues: "Issues", actions: "Actions",
};
const GONE_WORDS: Record<string, string> = {
  installation_gone: "The App was uninstalled on GitHub. Install it again.",
  app_gone: "GitHub no longer knows this App. Disconnect it and create a new one.",
  unreachable: "GitHub did not answer. Check again in a minute.",
  not_installed: "Not installed on any account yet.",
};
const kindWord = (t: AccountType | undefined) => (t === "User" ? "personal" : t === "Organization" ? "organization" : "");

/** "Verify, don't trust": what GitHub granted, each gap with the page that closes it. */
function VerifiedLines({ v, links, checking, onCheck }: {
  v: Verified | null | undefined; links: AppLinks; checking: boolean; onCheck: () => void;
}) {
  const again = (
    <button type="button" className="su-link" onClick={onCheck} disabled={checking} data-testid="gh-app-verify"
            title="Ask GitHub again what the App was granted." data-tip="Ask GitHub again what the App was granted.">
      {checking ? <CircleNotch className="su-spin" size={12} aria-hidden /> : null}Check again
    </button>
  );
  if (!v) {
    return <span className="su-line su-faint" data-testid="gh-app-verified" data-state="unknown">
      {checking ? "Asking GitHub what it granted…" : "Not checked with GitHub yet."} {again}</span>;
  }
  if (v.state !== "ok") {
    const fix = v.state === "installation_gone" || v.state === "not_installed" ? links.install : null;
    return (
      <span className="su-line" data-testid="gh-app-verified" data-state={v.state}>
        <Dot tone="red" />{GONE_WORDS[v.state] ?? "GitHub did not confirm the App."}
        {fix ? <Out href={fix} testid="gh-app-reinstall">Install on GitHub</Out> : null}{again}
      </span>
    );
  }
  const perms = v.permissions ?? [];
  const gaps = perms.filter((p) => p.state !== "accepted");
  const workflows = perms.find((p) => p.name === "workflows");
  const shown = workflows && workflows.state === "accepted" ? [...gaps, workflows] : gaps;
  const where = v.account?.login ? `@${v.account.login}` : "";
  return (
    <div className="su-stack" data-testid="gh-app-verified" data-state={v.ok ? "ok" : "gaps"}>
      <span className="su-line">
        <Dot tone={v.ok ? "green" : "amber"} />Installed on <span className="su-mono" data-user-text>{where}</span>
        {kindWord(v.account?.type) ? <span className="su-faint">{kindWord(v.account?.type)}</span> : null}
        {v.repository_selection === "all" ? <span className="su-faint">· all repositories</span> : null}
        {again}
      </span>
      {gaps.length === 0 ? (
        <span className="su-line" data-testid="gh-perm-all"><Dot tone="green" /><span>All permissions accepted ✓</span></span>
      ) : null}
      {shown.map((p) => (
        <span key={p.name} className="su-line" data-testid={`gh-perm-${p.name}`} data-state={p.state}>
          <Dot tone={p.state === "accepted" ? "green" : "amber"} />
          <span>{PERMISSION_WORDS[p.name] ?? p.name}: {p.state === "accepted" ? "accepted ✓"
            : p.state === "waiting" ? "waiting for you to accept" : "the App does not ask for it yet"}</span>
          {p.state !== "accepted" && p.fix_url ? (
            <Out href={p.fix_url} testid={`gh-perm-${p.name}-fix`}
                 tip={p.state === "waiting" ? "On the installation's page, press “Review request”, then Accept new permissions. Then Check again."
                      : `On the App's page, set ${PERMISSION_WORDS[p.name] ?? p.name} to “${p.wanted === "write" ? "Read and write" : "Read-only"}” and save. Then accept it on the installation.`}>
              {p.state === "waiting" ? "Accept on GitHub" : "Add on GitHub"}
            </Out>
          ) : null}
        </span>
      ))}
      {(v.repos ?? []).map((r) => (
        <span key={r.full_name} className="su-line" data-testid="gh-repo-verified" data-installed={String(r.installed)}>
          <Dot tone={r.installed ? "green" : r.installed === false ? "red" : "grey"} />
          <span><span className="su-mono" data-user-text>{r.full_name}</span>{" "}
          {r.installed ? "✓" : r.installed === false ? "the App is not installed on it" : "not confirmed"}</span>
          {r.installed === false && r.fix_url ? (
            <Out href={r.fix_url} testid="gh-repo-verified-fix"
                 tip="On the installation's page, add this repository under Repository access, save, then Check again.">
              Add it on GitHub
            </Out>
          ) : null}
        </span>
      ))}
    </div>
  );
}

/** Only a whole answer renders; anything else (an older API, a proxy page) reads as "Not reported". */
export function statusOf(raw: unknown): GitHubStatus | null {
  const s = raw as Partial<GitHubStatus> | null;
  if (!s || typeof s !== "object" || !["app", "token", "as_me", "none"].includes(String(s.mode))
      || !s.app || typeof s.app !== "object" || !s.env_token || typeof s.env_token !== "object"
      || !s.verdict || typeof s.verdict.label !== "string") return null;
  return { ...(s as GitHubStatus), people_connected: Number(s.people_connected) || 0,
           token: s.token && Array.isArray(s.token.repos) ? s.token : null };
}

/** Missing fields must never turn an unknown connection into fresh setup. */
function unusedAccess(raw: unknown): boolean {
  const s = raw as GitHubStatus | null;
  return Boolean(s && s.mode === "none" && s.verdict?.tone === "red" && s.app?.configured === false && s.app.installed === false
    && s.app.source === null && s.app.slug === null && s.app.html_url === null && s.app.account === null
    && (!s.app.verified || s.app.verified.state === "no_app")
    && s.token === null && s.people_connected === 0
    && s.env_token?.present === false && s.env_token.read_by_builds === false);
}

export function sentence(error: unknown): string {
  if (error instanceof IdentityError) {
    const code = String(error.body.error ?? error.code);
    return FAILED_WORDS[code] ?? (typeof error.body.message === "string" ? error.body.message : error.message);
  }
  return "That didn't work";
}

/** Hand the manifest to /github-app, the one page whose CSP lets a form go to GitHub
 *  (lib/identity/github-manifest.ts); it posts it there (GitHub's manifest flow). */
export function postManifest(postUrl: string, manifest: unknown): void {
  try {
    window.sessionStorage.setItem(HANDOFF_KEY, JSON.stringify({ post_url: postUrl, manifest }));
  } catch {
    toast("This browser blocks site storage, so GitHub cannot be opened from here.", "error");
    return;
  }
  window.location.assign(HANDOFF_PATH);
}

function AppCard({ status, reload, back = false, repos = [] }: {
  status: GitHubStatus; reload: () => void; back?: boolean; repos?: string[];
}) {
  const guarded = useGuardedCall();
  const [owner, setOwner] = useState("");
  const [busy, setBusy] = useState(false);
  const [picker, setPicker] = useState<Installation[] | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [checking, setChecking] = useState(false);
  const [verified, setVerified] = useState<Verified | null>(status.app.verified ?? null);
  const app = status.app;
  const links: AppLinks = { ...(app.links ?? {}), ...(verified?.links ?? {}) };
  useEffect(() => { setVerified(status.app.verified ?? null); }, [status.app.verified]);

  // `quiet`: an ask nobody pressed for (on open, a repository change, coming back to the tab).
  // Its failure stays in the card's own lines — never a toast over whatever area is on screen
  // (KG round 6: "Reload and retry" over the Knowledge Graph came from this card, mounted hidden
  // in Setup, asking GitHub on its own). An aborted request is never a failure.
  const verify = useCallback(async (quiet = false) => {
    setChecking(true);
    try {
      setVerified(await idPost<Verified>("/v1/identity/github/app/verify", {}));
      sayVerified("access");
    } catch (error) {
      if (!quiet && !isAborted(error)) toast(sentence(error), "error");
    } finally { setChecking(false); }
  }, []);
  // Asked on its own only while Setup is the area on screen (every area stays mounted hidden).
  const onScreen = useOnScreen();
  // ASKED AGAIN when GitHub sends the person back, when Test all runs, and once when the App is
  // installed but GitHub was never asked — never trusted from what BOSS requested.
  const installed = app.installed && app.source === "setup";
  // Asked on open when GitHub was never asked, when the stored answer has a gap (it may have been
  // closed on GitHub since) or is older than VERIFY_STALE_S — never shown stale as if current.
  const stored = status.app.verified;
  const needsAsking = !stored || stored.ok === false
    || !stored.checked_at || Date.now() / 1000 - stored.checked_at > VERIFY_STALE_S;
  const asked = useRef(false);
  useEffect(() => {
    if (!installed || !onScreen) return;
    if (back || (needsAsking && !asked.current)) { asked.current = true; void verify(!back); }
    const again = () => { void verify(); };
    const other = (e: Event) => { if ((e as CustomEvent<{ from?: string }>).detail?.from !== "access") reload(); };
    window.addEventListener(SETUP_TESTED_EVENT, again);
    window.addEventListener(GITHUB_VERIFIED_EVENT, other);
    return () => { window.removeEventListener(SETUP_TESTED_EVENT, again); window.removeEventListener(GITHUB_VERIFIED_EVENT, other); };
  }, [installed, onScreen, verify, needsAsking, back, reload]);
  // A REPOSITORY REGISTERED OR REMOVED: whether the App is installed on it is asked at once.
  const repoKey = repos.join(",");
  const lastRepos = useRef(repoKey);
  useEffect(() => {
    if (!installed || !onScreen || lastRepos.current === repoKey) return;
    lastRepos.current = repoKey;
    void verify(true);
  }, [installed, onScreen, repoKey, verify]);
  // A GAP IS FIXED ON GITHUB, in another tab: coming back to this one asks again (at most every 10s).
  const gap = installed && verified !== null && verified.ok === false;
  useEffect(() => {
    if (!gap) return;
    let last = Date.now();
    const onFocus = () => {
      if (document.visibilityState !== "visible" || Date.now() - last < 10_000) return;
      last = Date.now();
      void verify(true);
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => { window.removeEventListener("focus", onFocus); document.removeEventListener("visibilitychange", onFocus); };
  }, [gap, verify]);

  const create = async () => {
    setBusy(true);
    try {
      const out = await guarded("Creating a GitHub App gives BOSS write access to the repos you pick.",
        () => idPost<{ post_url: string; manifest: unknown }>("/v1/identity/github/app/manifest",
                                                              owner.trim() ? { owner: owner.trim() } : {}));
      postManifest(out.post_url, out.manifest);
    } catch (error) {
      toast(sentence(error), "error");
      setBusy(false);
    }
  };
  const loadPicker = async () => {
    setBusy(true);
    try {
      const out = await guarded("Choosing the installation decides which repos builds reach.",
        () => idGet<{ installations: Installation[] }>("/v1/identity/github/app/installations"));
      setPicker(out.installations);
    } catch (error) {
      toast(sentence(error), "error");
    } finally { setBusy(false); }
  };
  const choose = async (id: string) => {
    setBusy(true);
    try {
      await guarded("Choosing the installation decides which repos builds reach.",
        () => idPost("/v1/identity/github/app/installation", { installation_id: id }));
      setPicker(null);
      toast("GitHub App connected");
      reload();
    } catch (error) {
      toast(sentence(error), "error");
    } finally { setBusy(false); }
  };
  const disconnect = async () => {
    setConfirming(false);
    try {
      await guarded("Disconnecting the App stops builds that use it.", () => idDelete("/v1/identity/github/app"));
      toast("GitHub App disconnected");
      reload();
    } catch (error) { toast(sentence(error), "error"); }
  };

  let body: React.ReactNode;
  if (!app.configured) {
    body = (
      <>
        <label className="su-field">
          <Tip tip="Blank creates the App on your own GitHub account. Type an organization's name to create it there (you must be an owner of it)."
               className="su-field-label">Organization (optional)</Tip>
          <input value={owner} onChange={(e) => setOwner(e.target.value.replace(/[^A-Za-z0-9-]/g, ""))}
                 placeholder="blank = your account" aria-label="GitHub organization" data-testid="gh-app-owner" maxLength={100} />
        </label>
        <GatedButton permission="github:connect" type="button" className="su-btn su-btn-primary" onClick={create} disabled={busy}
                data-testid="gh-app-create" title="Opens GitHub with the App's permissions filled in."
                data-tip="Opens GitHub with the App's permissions filled in.">
          {busy ? <CircleNotch className="su-spin" size={12} aria-hidden /> : <GithubLogo size={14} aria-hidden />}
          Create GitHub App
        </GatedButton>
      </>
    );
  } else if (!app.installed) {
    body = (
      <>
        <span className="su-line"><Dot tone="amber" />Created{app.slug ? <span className="su-mono" data-user-text>{app.slug}</span> : null} · not installed</span>
        <div className="su-line">
          {links.install ? <Out href={links.install} testid="gh-app-install"
                               tip="Pick the repos it may touch.">Install on GitHub</Out> : null}
          <button type="button" className="su-btn su-btn-sm" onClick={loadPicker} disabled={busy}
                  data-testid="gh-app-pick">Pick installation</button>
        </div>
      </>
    );
  } else {
    body = (
      <>
        <span className="su-line"><Dot tone={verified && verified.state !== "ok" ? "red" : verified?.ok === false ? "amber" : "green"} />Connected{app.account ? <span className="su-mono" data-user-text>@{app.account}</span> : null}</span>
        {app.source === "setup" ? <VerifiedLines v={verified} links={links} checking={checking} onCheck={() => void verify()} /> : null}
        <div className="su-line">
          {links.installation || links.app || app.html_url ? (
            <Out href={(links.installation ?? links.app ?? app.html_url) as string} testid="gh-app-manage"
                 tip="Change which repositories it may reach, on GitHub.">Manage on GitHub</Out>
          ) : null}
          {app.source === "env" ? <Tip tip="Set by BOSS_GITHUB_APP_ID in the server's environment." className="su-chip">From install</Tip>
            : <button type="button" className="su-link" onClick={() => setConfirming(true)} data-testid="gh-app-disconnect">Disconnect</button>}
        </div>
      </>
    );
  }
  return (
    <div className="su-way" data-testid="gh-way-app" data-active={status.mode === "app" || undefined}>
      <div className="su-line"><strong>GitHub App</strong>
        <HelpTip label="GitHub App" text="A token per build, only for its repos, gone when it ends." />
        <span className="su-chip su-chip-good">Recommended</span></div>
      {body}
      {picker ? (
        <ul className="su-picker-list" data-testid="gh-app-installations">
          {picker.length === 0 ? <li className="su-faint">Not installed anywhere yet.</li> : picker.map((i) => (
            <li key={i.id}>
              <button type="button" className="su-option" onClick={() => choose(i.id)} disabled={busy}>
                <span className="su-mono" data-user-text>@{i.account ?? i.id}</span>
                {kindWord(i.account_type) ? <span className="su-faint">{kindWord(i.account_type)}</span> : null}
                <span className="su-faint">{i.repository_selection === "all" ? "all repos" : "selected repos"}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {confirming ? (
        <div className="su-confirm" role="alertdialog" data-testid="gh-app-disconnect-confirm">
          <strong>Disconnect the App?</strong>
          <button type="button" className="su-btn su-btn-sm su-btn-danger" onClick={disconnect}>Disconnect</button>
          <button type="button" className="su-link" onClick={() => setConfirming(false)}>Keep</button>
        </div>
      ) : null}
    </div>
  );
}

function AsMeCard({ status }: { status: GitHubStatus }) {
  const n = status.people_connected;
  return (
    <div className="su-way" data-testid="gh-way-as-me" data-active={status.mode === "as_me" || undefined}>
      <div className="su-line"><strong>As me</strong>
        <HelpTip label="As me" text="Runs you approve “as me on GitHub” use your own connection." />
      </div>
      <span className="su-line su-faint">{n === 0 ? "Nobody connected" : n === 1 ? "1 person connected" : `${n} people connected`}</span>
      <a className="su-link" href="/account/agents" data-testid="gh-as-me-link">My GitHub connection</a>
    </div>
  );
}

function TokenCard({ status, reload }: { status: GitHubStatus; reload: () => void }) {
  const guarded = useGuardedCall();
  const [confirming, setConfirming] = useState(false);
  const token = status.token;
  const remove = async () => {
    setConfirming(false);
    try {
      await guarded("Removing the token stops builds that use it.", () => idDelete("/v1/identity/github/token"));
      toast("Token removed");
      reload();
    } catch (error) { toast(sentence(error), "error"); }
  };
  return (
    <div className="su-way" data-testid="gh-way-token" data-active={status.mode === "token" || undefined}>
      <div className="su-line"><strong>Fine-grained token</strong>
        <HelpTip label="Fine-grained token" text="Kept in the vault, used only by egress, only for these repos." />
        <Tip tip="Long-lived and acts as whoever made it. Egress still applies every rule."
             className="su-chip su-chip-metered">Less safe</Tip></div>
      {token ? (
        <>
          <span className="su-line">
            <Dot tone="amber" />
            {token.repos.map((r) => <span key={r} className="su-mono" data-user-text>{r}</span>)}
          </span>
          <span className="su-line su-faint">
            {token.login ? <>as <span className="su-mono" data-user-text>@{token.login}</span></> : null}
            {token.migrated ? " · moved from BOSS_GITHUB_TOKEN" : ""}
          </span>
          <button type="button" className="su-link" onClick={() => setConfirming(true)} data-testid="gh-token-remove">Remove</button>
        </>
      ) : <span className="su-line su-faint">None. A token pasted in Repository lands here.</span>}
      {confirming ? (
        <div className="su-confirm" role="alertdialog">
          <strong>Remove the token?</strong>
          <button type="button" className="su-btn su-btn-sm su-btn-danger" onClick={remove}>Remove</button>
          <button type="button" className="su-link" onClick={() => setConfirming(false)}>Keep</button>
        </div>
      ) : null}
    </div>
  );
}

function MoveEnvToken({ repos, reload }: { repos: string[]; reload: () => void }) {
  const guarded = useGuardedCall();
  const [busy, setBusy] = useState(false);
  const move = async () => {
    setBusy(true);
    try {
      await guarded("Moving the token into the vault changes how builds reach GitHub.",
        () => idPost("/v1/identity/github/token/migrate-env", { repos }));
      toast("Moved into the vault");
      reload();
    } catch (error) { toast(sentence(error), "error"); } finally { setBusy(false); }
  };
  return (
    <div className="su-line su-way-env" role="status" data-testid="gh-env-token">
      <Dot tone="amber" />
      <span>BOSS_GITHUB_TOKEN is set on the server.</span>
      <button type="button" className="su-btn su-btn-sm su-btn-primary" onClick={move}
              disabled={busy || repos.length === 0} data-testid="gh-env-move"
              title={repos.length ? `Kept for ${repos.join(", ")}. Then the variable is not read.` : "Connect a repository first."}
              data-tip={repos.length ? `Kept for ${repos.join(", ")}. Then the variable is not read.` : "Connect a repository first."}>
        Move it into the vault
      </button>
    </div>
  );
}

export const GitHubAccessRow = forwardRef<HTMLElement, { initial?: GitHubStatus | null; focused?: boolean }>(function GitHubAccessRow({ initial = null, focused }, ref) {
  const { view } = useBoard();
  const [status, setStatus] = useState<GitHubStatus | null>(initial);
  const [unused, setUnused] = useState(unusedAccess(initial));
  const [otherWaysOpen, setOtherWaysOpen] = useState(false);
  const [redirectFailed, setRedirectFailed] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const [back, setBack] = useState(false);
  const repos = view.repository.repos.map((r) => r.full_name);

  const reload = useCallback(() => {
    idGet<unknown>("/v1/identity/github")
      .then((raw) => {
        const s = statusOf(raw);
        setStatus(s);
        setUnused(Boolean(s && unusedAccess(raw)));
        setFailed(s ? null : "Not reported");
      })
      .catch((error) => setFailed(error instanceof IdentityError ? error.message : "Not readable now"));
  }, []);
  useEffect(() => { if (!initial) reload(); }, [initial, reload]);
  useEffect(() => {
    // GitHub's redirect back: say what happened once, then drop it from the address.
    try {
      const params = new URLSearchParams(window.location.search);
      const said = params.get("github");
      if (!said) return;
      if (said === "app_connected") { toast("GitHub App connected"); setBack(true); }
      else if (said === "app_updated") {
        toast("GitHub repository access updated");
        setBack(true);
        const lost = lostRepos(params.get("lost"));
        if (lost.length === 1) toast(`${lost[0]} lost access and an app still uses it`, "error");
        else if (lost.length > 1) toast(`${lost.join(", ")} lost access and an app still uses them`, "error");
      }
      else { setRedirectFailed(true); toast(FAILED_WORDS[params.get("reason") ?? ""] ?? "GitHub App not connected", "error"); }
      params.delete("github"); params.delete("reason"); params.delete("lost");
      window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
    } catch { /* no window */ }
  }, []);

  const verdict = status?.verdict ?? null;
  const hideAlternatives = unused && !redirectFailed && !view.repository.connection && !repos.length;
  return (
    <section ref={ref} className="su-row" data-testid="su-row-github-access" data-focused={focused || undefined} aria-label="GitHub access" id="su-github-access">
      <div className="su-row-label">
        <SectionLabel as="span" tip={SANDBOX_TIP}>GitHub access</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        {failed ? <span className="su-faint">{failed}</span> : !status || !verdict ? (
          <span className="su-faint su-line"><CircleNotch className="su-spin" size={12} aria-hidden />Loading…</span>
        ) : (
          <>
            <span className={`su-pill su-tone-${verdict.tone}`} data-testid="gh-mode" data-mode={status.mode}
                  title={verdict.tip} data-tip={verdict.tip}>
              <Dot tone={verdict.tone} />{verdict.label}
            </span>
            {status.env_token.present ? <MoveEnvToken repos={repos} reload={reload} /> : null}
            <AppCard status={status} reload={reload} back={back} repos={repos} />
            <details className="su-other-ways" data-testid="gh-other-ways"
                     open={!hideAlternatives || otherWaysOpen}
                     onToggle={(e) => { if (hideAlternatives) setOtherWaysOpen(e.currentTarget.open); }}>
              <summary hidden={!hideAlternatives}>Other ways</summary>
              <div className="su-ways su-ways-alternatives">
                <AsMeCard status={status} />
                <TokenCard status={status} reload={reload} />
              </div>
            </details>
          </>
        )}
      </div>
    </section>
  );
});
