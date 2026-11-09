"use client";

/**
 * ② Repos, first run: ONE "Connect GitHub" (BOSSAPP-815). It creates this install's own BOSS
 * GitHub App from a manifest and installs it on the repos you pick, two clicks on GitHub, no
 * token to make, copy or renew. The App's key never expires; egress mints a fresh access token
 * before each GitHub call (identity/github_app.py), so a connected repo never stops building
 * because access ran out. Self-hosted tools do the same (Coder, Atlantis, Backstage); a pasted
 * token stays under "Other ways" for GitHub Enterprise without Apps or offline installs.
 *
 *   no App      Where are your repos? (my account | an organisation) → Connect GitHub
 *   created     Install it on your repos → (GitHub) → back here
 *   installed   its repos, pick one → Connect → the three checks
 */
import React, { useCallback, useEffect, useState } from "react";
import { CircleNotch, GithubLogo } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { idGet, idPost } from "../../lib/identity/api";
import { setupWrite } from "../../lib/setup-client";
import type { SetupRefusal, SetupView, TokenFound } from "../../lib/setup-types";
import { postManifest, sentence, statusOf, type GitHubStatus } from "./github-access-row";
import { toast, useBoard } from "./parts";
import { Out, Refusal } from "./pill";
import { RepoPicker } from "./repo-picker";

export const HELP_URL = "https://github.com/seanventures/boss/blob/main/docs/community/CONNECT_GITHUB.md";
type Where = "me" | "org";

/** What happens next, in the words a first-timer needs (shown before GitHub opens). */
export const NEXT_STEPS = [
  "GitHub opens with BOSS's app filled in. Click Create.",
  "Pick the repos BOSS may work on. Click Install.",
  "You're back here. Pick your repo. Done.",
] as const;

function refusalOf(error: unknown): SetupRefusal {
  return { state: "failed", headline: sentence(error), fix: "" };
}

export function ConnectGitHub({ onDone, makeDefault = true, onOtherWays, tokenForm }: {
  onDone?: () => void; makeDefault?: boolean; onOtherWays: () => void;
  /** The token path as before, shown instead when this is not a fresh GitHub setup. */
  tokenForm: React.ReactNode;
}) {
  const { projectId, view, apply } = useBoard();
  const guarded = useGuardedCall();
  const selectGate = useGate("repository:register");
  const [status, setStatus] = useState<GitHubStatus | null>(null);
  const [where, setWhere] = useState<Where>("me");
  const [org, setOrg] = useState("");
  const [busy, setBusy] = useState(false);
  const [found, setFound] = useState<TokenFound | null>(null);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  const [asBefore, setAsBefore] = useState(false);

  // UNREADABLE IS NOT FRESH: if GitHub access can't be read, the token path as before, never a
  // new App over a connection we could not see (github-access-row.tsx, unusedAccess).
  const reload = useCallback(() => {
    idGet<unknown>("/v1/identity/github").then((raw) => {
      const s = statusOf(raw);
      if (s) setStatus(s); else setAsBefore(true);
    }).catch(() => setAsBefore(true));
  }, []);
  useEffect(() => { reload(); }, [reload]);
  // Back from GitHub in another tab (or after Install): ask again when this one is shown.
  useEffect(() => {
    const again = () => { if (document.visibilityState === "visible") reload(); };
    document.addEventListener("visibilitychange", again);
    return () => document.removeEventListener("visibilitychange", again);
  }, [reload]);

  // ONLY A FRESH SETUP GETS A NEW APP: once an App exists, builds use it for every repo
  // (github_app.mint_for_run), so an install already on a token or "as me" stays on it.
  const installed = Boolean(status?.app.installed);
  // INSTALLED: its repos straight away, nothing to press.
  useEffect(() => {
    if (!installed || found) return;
    let live = true;
    setBusy(true);
    setupWrite<TokenFound>(projectId, "repo_app", {}, { label: "List repos", background: true })
      .then((f) => { if (live) setFound(f); })
      .catch((error) => { if (live) setRefusal(refusalOf(error)); })
      .finally(() => { if (live) setBusy(false); });
    return () => { live = false; };
  }, [installed, found, projectId]);

  const create = async () => {
    if (where === "org" && !org.trim()) {
      setRefusal({ state: "none", headline: "Type the organisation's name", fix: "As it appears in github.com/<name>." });
      return;
    }
    setBusy(true);
    setRefusal(null);
    try {
      const out = await guarded("Connecting GitHub lets BOSS write branches and open pull requests on the repos you pick.",
        () => idPost<{ post_url: string; manifest: unknown }>("/v1/identity/github/app/manifest",
                                                              where === "org" ? { owner: org.trim() } : {}));
      postManifest(out.post_url, out.manifest);
    } catch (error) {
      setRefusal(refusalOf(error));
      setBusy(false);
    }
  };

  const connect = async (fullName: string) => {
    if (!found || !selectGate.allowed) return;
    setBusy(true);
    try {
      const next = await setupWrite<SetupView>(projectId, "repo_select", {
        connection_id: found.connection_id, full_name: fullName, make_default: makeDefault,
      }, { label: "Connect" });
      apply(next);
      const row = next.repository.repos.find((r) => r.full_name === fullName);
      if (row?.pill.tone === "green") toast("Connected, Working");
      onDone?.();
    } catch (error) { setRefusal(refusalOf(error)); } finally { setBusy(false); }
  };

  const otherWays = (
    <div className="su-line">
      <a className="su-link" href={HELP_URL} target="_blank" rel="noreferrer" data-testid="gh-connect-help">Need help?</a>
      <button type="button" className="su-link su-faint" onClick={onOtherWays} data-testid="su-use-token"
              title="Paste a token: for GitHub Enterprise without apps, or offline installs."
              data-tip="Paste a token: for GitHub Enterprise without apps, or offline installs.">
        Other ways
      </button>
    </div>
  );

  if (asBefore || (status && !status.app.configured && status.mode !== "none")) return <>{tokenForm}</>;
  let body: React.ReactNode;
  if (!status) {
    body = <span className="su-faint su-line"><CircleNotch className="su-spin" size={12} aria-hidden />Loading…</span>;
  } else if (installed) {
    body = found ? (
      <div className="su-form" data-testid="gh-connect-pick">
        <div className="su-line">
          <span className="su-dot su-tone-green" aria-hidden="true" />
          <span>GitHub connected{status.app.account ? <>, <span className="su-mono" data-user-text>@{status.app.account}</span></> : null}</span>
        </div>
        {found.repos.length ? (
          <RepoPicker repos={found.repos} busy={busy} onConnect={connect}
                      exclude={view.repository.repos.filter((r) => r.binding_id).map((r) => r.full_name)} />
        ) : <span className="su-faint" data-testid="gh-connect-no-repos">No repos picked on GitHub yet.</span>}
        {status.app.links?.installation || status.app.links?.install ? (
          <Out href={(status.app.links.installation ?? status.app.links.install) as string} testid="gh-connect-more"
               tip="Add or remove repos on GitHub, then come back.">Add repos on GitHub</Out>
        ) : null}
      </div>
    ) : <span className="su-faint su-line"><CircleNotch className="su-spin" size={12} aria-hidden />Asking GitHub for your repos…</span>;
  } else if (status.app.configured) {
    body = (
      <div className="su-form" data-testid="gh-connect-install">
        <span>Almost done. Pick the repos BOSS may work on.</span>
        <div className="su-line">
          {status.app.links?.install ? (
            <a className="su-btn su-btn-primary" href={status.app.links.install} data-testid="gh-connect-install-go">
              <GithubLogo size={14} aria-hidden />Install on GitHub
            </a>
          ) : null}
          <button type="button" className="su-link" onClick={reload} data-testid="gh-connect-recheck">I've installed it</button>
        </div>
      </div>
    );
  } else {
    body = (
      <div className="su-form" data-testid="gh-connect-start">
        <div className="su-radio-cards" role="radiogroup" aria-label="Where are your repos?">
          {([["me", "My GitHub account", "Repos under your own name."],
             ["org", "An organisation", "You need to be an owner of it, or ask one to do this step."]] as const)
            .map(([key, title, line]) => (
              <label key={key} className="su-radio-card" data-checked={where === key || undefined} data-testid={`gh-where-${key}`}>
                <input type="radio" name="gh-where" value={key} checked={where === key}
                       onChange={() => { setWhere(key); setRefusal(null); }} />
                <span className="su-stack su-tight">
                  <span className="su-strong">{title}</span>
                  <span className="su-faint">{line}</span>
                </span>
              </label>
            ))}
        </div>
        {where === "org" ? (
          <input className="su-input" value={org} placeholder="organisation name, e.g. acme" aria-label="Organisation name"
                 maxLength={100} data-testid="gh-connect-org"
                 onChange={(e) => setOrg(e.target.value.replace(/[^A-Za-z0-9-]/g, ""))}
                 onKeyDown={(e) => { if (e.key === "Enter") void create(); }} />
        ) : null}
        <ol className="su-next-steps" data-testid="gh-connect-steps">
          {NEXT_STEPS.map((s) => <li key={s}>{s}</li>)}
        </ol>
        <div className="su-line">
          <GatedButton permission="github:connect" type="button" className="su-btn su-btn-primary" onClick={() => void create()}
                       disabled={busy} data-testid="gh-connect-go"
                       title="No token to copy, and access never expires." data-tip="No token to copy, and access never expires.">
            {busy ? <CircleNotch className="su-spin" size={12} aria-hidden /> : <GithubLogo size={14} aria-hidden />}
            Connect GitHub
          </GatedButton>
        </div>
      </div>
    );
  }
  return (
    <div className="su-stack" data-testid="gh-connect">
      {body}
      {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
      {otherWays}
    </div>
  );
}
