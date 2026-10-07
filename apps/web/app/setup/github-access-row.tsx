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
 * GitHub's redirects land back here with `?github=app_connected|app_failed`.
 */
import React, { useCallback, useEffect, useState } from "react";
import { CircleNotch, GithubLogo } from "@phosphor-icons/react";

import { IdentityError, idDelete, idGet, idPost } from "../../lib/identity/api";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { SectionLabel } from "../section-label";
import { toast, useBoard } from "./parts";
import { Dot, Out, Tip } from "./pill";

export type GitHubMode = "app" | "token" | "as_me" | "none";
export type GitHubStatus = {
  mode: GitHubMode;
  /** The row's pill, decided by the server. */
  verdict: { tone: Tone; label: string; tip: string };
  app: { configured: boolean; installed: boolean; slug: string | null; html_url: string | null;
         account: string | null; source: "env" | "setup" | null };
  token: { repos: string[]; login: string | null; migrated: boolean } | null;
  people_connected: number;
  env_token: { present: boolean; read_by_builds: boolean };
};
type Installation = { id: string; account: string | null; repository_selection: string | null };
type Tone = "green" | "amber" | "red" | "grey";

const SANDBOX_TIP = "Egress adds the credential to each request. The build's sandbox never holds it.";
const FAILED_WORDS: Record<string, string> = {
  bad_state: "The link expired or was used. Start again.",
  state_expired: "That took over 10 minutes. Start again.",
  state_replayed: "That link was already used.",
  manifest_conversion_failed: "GitHub did not hand over the App. Start again.",
  not_this_apps_installation: "That installation belongs to another App.",
};

/** Only a whole answer renders; anything else (an older API, a proxy page) reads as "Not reported". */
export function statusOf(raw: unknown): GitHubStatus | null {
  const s = raw as Partial<GitHubStatus> | null;
  if (!s || typeof s !== "object" || !["app", "token", "as_me", "none"].includes(String(s.mode))
      || !s.app || typeof s.app !== "object" || !s.env_token || typeof s.env_token !== "object"
      || !s.verdict || typeof s.verdict.label !== "string") return null;
  return { ...(s as GitHubStatus), people_connected: Number(s.people_connected) || 0,
           token: s.token && Array.isArray(s.token.repos) ? s.token : null };
}

function sentence(error: unknown): string {
  if (error instanceof IdentityError) {
    const code = String(error.body.error ?? error.code);
    return FAILED_WORDS[code] ?? (typeof error.body.message === "string" ? error.body.message : error.message);
  }
  return "That didn't work";
}

/** POST the manifest to GitHub as a form (GitHub's manifest flow): the browser leaves for GitHub. */
function postManifest(postUrl: string, manifest: unknown): void {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = postUrl;
  form.style.display = "none";
  const field = document.createElement("input");
  field.type = "hidden";
  field.name = "manifest";
  field.value = JSON.stringify(manifest);
  form.append(field);
  document.body.append(form);
  form.submit();
}

function AppCard({ status, reload }: { status: GitHubStatus; reload: () => void }) {
  const guarded = useGuardedCall();
  const [owner, setOwner] = useState("");
  const [busy, setBusy] = useState(false);
  const [picker, setPicker] = useState<Installation[] | null>(null);
  const [confirming, setConfirming] = useState(false);
  const app = status.app;

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
          <span className="su-field-label">GitHub org (blank for your account)</span>
          <input value={owner} onChange={(e) => setOwner(e.target.value.replace(/[^A-Za-z0-9-]/g, ""))}
                 placeholder="acme" aria-label="GitHub org" data-testid="gh-app-owner" maxLength={100} />
        </label>
        <button type="button" className="su-btn su-btn-primary" onClick={create} disabled={busy}
                data-testid="gh-app-create" title="Opens GitHub with the App's permissions filled in."
                data-tip="Opens GitHub with the App's permissions filled in.">
          {busy ? <CircleNotch className="su-spin" size={12} aria-hidden /> : <GithubLogo size={14} aria-hidden />}
          Create the App on GitHub
        </button>
      </>
    );
  } else if (!app.installed) {
    body = (
      <>
        <span className="su-line"><Dot tone="amber" />Created{app.slug ? <span className="su-mono" data-user-text>{app.slug}</span> : null} · not installed</span>
        <div className="su-line">
          {app.html_url ? <Out href={`${app.html_url}/installations/new`} testid="gh-app-install"
                               tip="Pick the repos it may touch.">Install on GitHub</Out> : null}
          <button type="button" className="su-btn su-btn-sm" onClick={loadPicker} disabled={busy}
                  data-testid="gh-app-pick">Pick installation</button>
        </div>
      </>
    );
  } else {
    body = (
      <>
        <span className="su-line"><Dot tone="green" />Connected{app.account ? <span className="su-mono" data-user-text>@{app.account}</span> : null}</span>
        <div className="su-line">
          {app.html_url ? <Out href={app.html_url} tip="Change its repos on GitHub.">Manage on GitHub</Out> : null}
          {app.source === "env" ? <Tip tip="Set by BOSS_GITHUB_APP_ID in the server's environment." className="su-chip">From install</Tip>
            : <button type="button" className="su-link" onClick={() => setConfirming(true)} data-testid="gh-app-disconnect">Disconnect</button>}
        </div>
      </>
    );
  }
  return (
    <div className="su-way" data-testid="gh-way-app" data-active={status.mode === "app" || undefined}>
      <div className="su-line"><strong>GitHub App</strong><span className="su-chip su-chip-good">Recommended</span></div>
      <p className="su-way-text">A token per build, only for its repos, gone when it ends.</p>
      {body}
      {picker ? (
        <ul className="su-picker-list" data-testid="gh-app-installations">
          {picker.length === 0 ? <li className="su-faint">Not installed anywhere yet.</li> : picker.map((i) => (
            <li key={i.id}>
              <button type="button" className="su-option" onClick={() => choose(i.id)} disabled={busy}>
                <span className="su-mono" data-user-text>@{i.account ?? i.id}</span>
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
      <div className="su-line"><strong>As me</strong></div>
      <p className="su-way-text">Runs you approve “as me on GitHub” use your own connection.</p>
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
        <Tip tip="Long-lived and acts as whoever made it. Egress still applies every rule."
             className="su-chip su-chip-metered">Less safe</Tip></div>
      <p className="su-way-text">Kept in the vault, used only by egress, only for these repos.</p>
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

export function GitHubAccessRow({ initial = null }: { initial?: GitHubStatus | null }) {
  const { view } = useBoard();
  const [status, setStatus] = useState<GitHubStatus | null>(initial);
  const [failed, setFailed] = useState<string | null>(null);
  const repos = view.repository.repos.map((r) => r.full_name);

  const reload = useCallback(() => {
    idGet<unknown>("/v1/identity/github")
      .then((raw) => {
        const s = statusOf(raw);
        setStatus(s);
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
      if (said === "app_connected") toast("GitHub App connected");
      else toast(FAILED_WORDS[params.get("reason") ?? ""] ?? "GitHub App not connected", "error");
      params.delete("github"); params.delete("reason");
      window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
    } catch { /* no window */ }
  }, []);

  const verdict = status?.verdict ?? null;
  return (
    <section className="su-row" data-testid="su-row-github-access" aria-label="GitHub access" id="su-github-access">
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
            <div className="su-ways">
              <AppCard status={status} reload={reload} />
              <AsMeCard status={status} />
              <TokenCard status={status} reload={reload} />
            </div>
          </>
        )}
      </div>
    </section>
  );
}
