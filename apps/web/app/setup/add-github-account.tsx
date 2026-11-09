"use client";

/**
 * ADD A GITHUB ACCOUNT, GUIDED BY BOSS (BOSSAPP-945; wireframe docs/product/wireframes/
 * agentic-factory/4-add-github-account.html).
 *
 *   1 one input        a user or organisation name, or a github.com link. BOSS looks it up on
 *                      GitHub (a person or an organisation, its id, its public repositories, and
 *                      whether BOSS is connected to it already).
 *   2 Agent actions    the path BOSS picked, as steps, from GitHub's own answers:
 *                        installed already            -> Pick repos
 *                        the app is private, and this
 *                        account is not its owner      -> the one-time "let it install" switch
 *                        otherwise                     -> Install on <account>
 *   3 the switch       a link to the app's Advanced settings; BOSS watches the app's public page,
 *                      never an "I did it" button.
 *   4 install          GitHub's install page aimed at the account, in a new tab; BOSS watches the
 *                      app's installations and moves on when the new one appears.
 *   5 Other ways       a pasted access token and the org's own GitHub App (the steps that were
 *                      here before, kept whole), and Sign in with GitHub where it is set up.
 *   6 Pick repos       the installation's repositories with "used by" tags; Allow runs the
 *                      registry's allow and the access check, and says each result in words.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { ArrowSquareOut, CaretDown, Check, CircleNotch, DownloadSimple, GithubLogo, Key, ListChecks,
         LockOpen, SealCheck, Wrench } from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { fetchSetup } from "../../lib/setup-client";
import { idPost } from "../../lib/identity/api";
import {
  allowRepos, installationRepos, lookable, lookupAccount, MIN_POLL_MS, pollDelay, publicRepos, refusalWords,
  stepsFor, watchAccount,
  type AccountLookup, type AccountPath, type AllowResult, type InstallationRepo, type Step,
} from "../../lib/github-accounts";
import { postManifest } from "./github-access-row";
import { useBoard } from "./parts";
import type { How } from "./add-account-panel";
import "./add-github-account.css";

const PAUSE_MS = 600;
const STEP_ICON: Record<string, React.ReactNode> = {
  found: <Check weight="bold" />, gate: <LockOpen />, create: <Wrench />, install: <DownloadSimple />,
  pick: <ListChecks />, check: <SealCheck />,
};

function Steps({ steps }: { steps: Step[] }) {
  return (
    <ol className="ga-prog" data-testid="ga-steps">
      {steps.map((s) => (
        <li key={s.key} className="ga-ps" data-s={s.state} data-testid={`ga-step-${s.key}`}>
          <span className="ga-ps-c" aria-hidden="true">{s.state === "done" ? <Check weight="bold" /> : STEP_ICON[s.key]}</span>
          <b>{s.label}</b><small>{s.sub}</small>
        </li>
      ))}
    </ol>
  );
}

function Out({ href, children, testid, primary = true }: {
  href: string; children: React.ReactNode; testid: string; primary?: boolean;
}) {
  return (
    <a className={`ga-btn${primary ? " ga-btn-primary" : ""}`} href={href} target="_blank" rel="noreferrer noopener"
       data-testid={testid}>
      <ArrowSquareOut aria-hidden="true" size={14} />{children}
    </a>
  );
}

const TONE: Record<string, string> = { working: "green", read_only: "amber", needs_fix: "red", unreachable: "red",
                                       archived: "red", checking: "accent" };

export function AddGitHubAccount({ onClose, onAdvanced, onOther }: {
  onClose: () => void;
  /** The org's own GitHub App (Advanced settings › GitHub App). */
  onAdvanced: () => void;
  /** A way that was here before: a pasted token, or Sign in with GitHub. */
  onOther: (how: How, login: string | null) => void;
}) {
  const { projectId, apply, signin } = useBoard();
  const guarded = useGuardedCall();
  const [text, setText] = useState("");
  const [looking, setLooking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [found, setFound] = useState<AccountLookup | null>(null);
  const [path, setPath] = useState<AccountPath | null>(null);
  const [sawGate, setSawGate] = useState(false);
  const [installation, setInstallation] = useState<string | null>(null);
  const [watchedAt, setWatchedAt] = useState<number | null>(null);
  const [repos, setRepos] = useState<InstallationRepo[] | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [allowing, setAllowing] = useState(false);
  const [results, setResults] = useState<AllowResult[] | null>(null);
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const asked = useRef("");
  const lastPoll = useRef(0);

  // ---- 1 · look the account up (a pause after typing, or Enter) ----------------------------------
  const lookup = useCallback(async (value: string) => {
    const q = value.trim();
    if (!lookable(q) || q === asked.current) return;
    asked.current = q;
    setLooking(true);
    setError(null);
    try {
      const out = await lookupAccount(q);
      if (asked.current !== q) return;          // typed on meanwhile: the newer answer wins
      setFound(out);
      setPath(out.path);
      setSawGate(out.path === "make_public");
      setInstallation(out.installation?.installation_id ?? null);
      setRepos(null); setResults(null); setPicked([]);
    } catch (e) {
      if (asked.current !== q) return;
      setFound(null); setPath(null);
      setError(refusalWords(e));
    } finally {
      if (asked.current === q) setLooking(false);
    }
  }, []);

  useEffect(() => {
    if (!lookable(text)) return undefined;
    const t = window.setTimeout(() => { void lookup(text); }, PAUSE_MS);
    return () => window.clearTimeout(t);
  }, [text, lookup]);

  // ---- 3/4 · watch GitHub while the person is there -----------------------------------------------
  const login = found?.account.login ?? "";
  const accountId = found?.account.id ?? "";
  const watching = path === "make_public" || path === "install";
  const pollNow = useCallback(async () => {
    if (!login) return;
    lastPoll.current = Date.now();
    try {
      const out = await watchAccount(login, accountId);
      setWatchedAt(Date.now());
      if (out.path === "make_public") setSawGate(true);
      setPath(out.path);
      setFound((f) => (f ? { ...f, app: out.app ?? f.app, installation: out.installation, line: out.line } : f));
      if (out.installation?.installation_id) setInstallation(out.installation.installation_id);
    } catch {
      setWatchedAt(Date.now());                 // a miss: the next poll asks again
    }
  }, [login, accountId]);

  useEffect(() => {
    if (!watching) return undefined;
    let attempt = 0;
    let timer = 0;
    let stopped = false;
    const next = () => {
      timer = window.setTimeout(async () => {
        if (stopped) return;
        await pollNow();
        attempt += 1;
        if (!stopped) next();
      }, pollDelay(attempt));
    };
    next();
    // Back from GitHub's tab: ask at once (never more often than every 5 s).
    const back = () => {
      if (document.visibilityState === "visible" && Date.now() - lastPoll.current >= MIN_POLL_MS) {
        attempt = 0;
        void pollNow();
      }
    };
    window.addEventListener("focus", back);
    document.addEventListener("visibilitychange", back);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      window.removeEventListener("focus", back);
      document.removeEventListener("visibilitychange", back);
    };
  }, [watching, pollNow]);

  // ---- 6 · the installation's repositories --------------------------------------------------------
  useEffect(() => {
    if (path !== "pick" || !installation) return;
    let live = true;
    setRepos(null);
    installationRepos(installation).then((out) => {
      if (!live) return;
      setRepos(out.repos);
      const open = out.repos.filter((r) => !r.allowed);
      setPicked(open.length === 1 ? [open[0].full_name] : []);
    }).catch((e) => { if (live) setError(refusalWords(e)); });
    return () => { live = false; };
  }, [path, installation]);

  const allow = async () => {
    if (!installation || !picked.length) return;
    setAllowing(true);
    setError(null);
    try {
      const out = await allowRepos(projectId, installation, picked);
      setResults(out.results);
      setSummary(out.line);
      try { apply(await fetchSetup(projectId)); } catch { /* the board refreshes on its own */ }
    } catch (e) {
      setError(refusalWords(e));
    } finally {
      setAllowing(false);
    }
  };

  const createApp = async () => {
    setBusy(true);
    setError(null);
    try {
      const owner = found?.account.type === "Organization" ? { owner: found.account.login } : {};
      const out = await guarded("Connecting GitHub lets BOSS write branches and open pull requests on the repos you pick.",
        () => idPost<{ post_url: string; manifest: unknown }>("/v1/identity/github/app/manifest", owner));
      postManifest(out.post_url, out.manifest);
    } catch (e) {
      setError(refusalWords(e));
      setBusy(false);
    }
  };

  const toggle = (name: string) => setPicked((p) => (p.includes(name) ? p.filter((x) => x !== name) : [...p, name]));
  const kind = found?.account.type ?? null;
  const steps = found && path ? stepsFor(path, login, kind, { sawGate, allowing, allowed: !!results }) : [];
  const app = found?.app ?? null;
  const done = !!results;
  const ago = watchedAt ? Math.max(0, Math.round((Date.now() - watchedAt) / 1000)) : null;

  let primary: React.ReactNode;
  if (done) {
    primary = <button type="button" className="ga-btn ga-btn-primary" onClick={onClose} data-testid="ga-done">Done</button>;
  } else if (path === "pick") {
    primary = (
      <GatedButton permission="repository:register" className="ga-btn ga-btn-primary" data-testid="ga-allow"
                   disabled={allowing || !picked.length} onClick={() => void allow()}>
        {allowing ? <CircleNotch className="ga-spin" size={14} aria-hidden /> : null}
        {allowing ? "Checking access…" : picked.length ? `Allow ${picked.length} ${picked.length === 1 ? "repository" : "repositories"}`
          : "Pick a repository"}
      </GatedButton>
    );
  } else if (path === "no_app") {
    primary = (
      <GatedButton permission="github:connect" className="ga-btn ga-btn-primary" data-testid="ga-create-app"
                   disabled={busy} onClick={() => void createApp()}>
        {busy ? <CircleNotch className="ga-spin" size={14} aria-hidden /> : <GithubLogo size={14} aria-hidden />}
        Create the BOSS app
      </GatedButton>
    );
  } else if (watching) {
    primary = (
      <button type="button" className="ga-btn ga-btn-primary" aria-disabled="true" disabled data-testid="ga-waiting"
              title="Continues on its own when GitHub says the app is installed">
        <CircleNotch className="ga-spin" size={14} aria-hidden />Waiting for GitHub…
      </button>
    );
  } else {
    primary = (
      <button type="button" className="ga-btn ga-btn-primary" data-testid="ga-lookup"
              disabled={!lookable(text) || looking} onClick={() => { asked.current = ""; void lookup(text); }}>
        {looking ? <CircleNotch className="ga-spin" size={14} aria-hidden /> : null}Look up
      </button>
    );
  }

  return (
    <div className="ga" data-testid="ga-sheet" data-path={path ?? "none"}>
      {/* 1 · the one input */}
      <label className="ga-in" title="A GitHub user or organisation name, or a link to it.">
        <span className="ga-in-ico" aria-hidden="true"><GithubLogo weight="fill" size={20} /></span>
        <input className="ga-in-text" value={text} placeholder="kaappu" aria-label="GitHub account"
               autoComplete="off" spellCheck={false} maxLength={300} data-testid="ga-input"
               onChange={(e) => setText(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") { asked.current = ""; void lookup(text); } }} />
        <span className="ga-in-foot">a user or organisation name, or a github.com link</span>
      </label>

      {looking ? <p className="ga-detect" data-testid="ga-looking"><CircleNotch className="ga-spin" size={12} aria-hidden />Looking on GitHub…</p> : null}
      {error ? <p className="ga-error" role="alert" data-testid="ga-error">{error}</p> : null}
      {found && !looking ? (
        <p className="ga-detect" data-testid="ga-found">
          {found.account.avatar_url ? <img className="ga-avatar" src={found.account.avatar_url} alt="" width={20} height={20} /> : null}
          <span>Found on GitHub: <b data-user-text>{found.account.login}</b>
            {" · "}{kind === "Organization" ? "organisation" : "person"}
            {publicRepos(found.account.public_repos) ? ` · ${publicRepos(found.account.public_repos)}` : ""}
            {" · "}<span data-testid="ga-connection">{found.connection.covered ? found.connection.words : "not connected yet"}</span></span>
        </p>
      ) : null}

      {/* 2 · Agent actions */}
      {found && path ? (
        <section className="ga-card" data-testid="ga-agent-actions" aria-labelledby="ga-aa-h">
          <header className="ga-card-h">
            <AgentAvatar agent="forge" size={28} aiBadge={false} tip="Forge, BOSS's build agent" />
            <span className="ga-card-t">
              <h3 id="ga-aa-h">Agent actions</h3>
              <small data-testid="ga-line">{found.line}</small>
            </span>
          </header>
          <Steps steps={steps} />
        </section>
      ) : null}

      {/* 3 · the one-time switch */}
      {path === "make_public" && app ? (
        <section className="ga-gate" data-testid="ga-gate-public">
          <div className="ga-gate-h"><span className="ga-hand" aria-hidden="true">✋</span>
            <div><b>One time: let other accounts install the BOSS app</b>
              <small>Your app <code>{app.slug}</code> is private to @{app.owner_login ?? "you"}, and GitHub only lets a private
                app be installed on its owner. Making it installable gives nobody access by itself.</small></div></div>
          <div className="ga-row">
            {app.settings_url ? <Out href={app.settings_url} testid="ga-open-settings">Open the app&apos;s settings on GitHub</Out> : null}
            <span className="ga-faint">Press <b>Make public</b> there. BOSS sees it and moves on.</span>
          </div>
        </section>
      ) : null}

      {/* 4 · install, aimed at the account */}
      {(path === "make_public" || path === "install") && app?.install_url ? (
        <section className="ga-gate" data-dim={path === "make_public" || undefined} data-testid="ga-gate-install">
          <div className="ga-gate-h"><span className="ga-hand" aria-hidden="true">↗</span>
            <div><b>Install on {login}</b>
              <small>Opens GitHub&apos;s install page aimed at {login}. If GitHub shows another account, use your avatar
                menu → Switch account, then press Install.</small></div></div>
          <div className="ga-row">
            {path === "install"
              ? <Out href={app.install_url} testid="ga-install">Install the BOSS app on {login}</Out>
              : <span className="ga-btn" aria-disabled="true" data-testid="ga-install-later">Install the BOSS app on {login}</span>}
            <span className="ga-faint" aria-live="polite" data-testid="ga-watch">
              {path === "install" ? "BOSS watches for the new installation and continues here on its own." : "Next, once the app can install."}
              {ago !== null ? ` Checked ${ago < 5 ? "just now" : `${ago} s ago`}.` : ""}
            </span>
          </div>
        </section>
      ) : null}

      {path === "no_app" ? (
        <section className="ga-gate" data-testid="ga-gate-create">
          <div className="ga-gate-h"><span className="ga-hand" aria-hidden="true">✋</span>
            <div><b>First, this install&apos;s BOSS app</b>
              <small>BOSS creates its own GitHub app on {kind === "Organization" ? login : "your account"} (2 clicks on
                GitHub), then installs it there. Access never expires.</small></div></div>
        </section>
      ) : null}

      {/* 6 · the installation's repositories */}
      {path === "pick" ? (
        <section className="ga-repos" data-testid="ga-repos">
          <header className="ga-repos-h">
            <GithubLogo size={16} aria-hidden="true" />
            <b data-user-text>{login} · installed</b>
            <span className="ga-tag" data-tone="green">connected</span>
            {found?.installation?.settings_url
              ? <a className="ga-link" href={found.installation.settings_url} target="_blank" rel="noreferrer noopener"
                   data-testid="ga-installation-settings">Change on GitHub</a> : null}
          </header>
          {!repos ? <p className="ga-faint"><CircleNotch className="ga-spin" size={12} aria-hidden /> Listing repositories…</p> : null}
          {repos && !repos.length ? <p className="ga-faint" data-testid="ga-no-repos">The app reaches no repositories on {login} yet.
            Add some on GitHub (Change on GitHub).</p> : null}
          {repos?.length ? (
            <ul className="ga-list" aria-label="Repositories">
              {repos.map((r) => {
                const result = results?.find((x) => x.full_name === r.full_name);
                const state = result?.state ?? (r.allowed ? r.state : null);
                return (
                  <li key={r.full_name} className="ga-repo" data-testid={`ga-repo-${r.full_name}`}>
                    <label className="ga-repo-l">
                      <input type="checkbox" checked={r.allowed || picked.includes(r.full_name)} disabled={r.allowed || done}
                             onChange={() => toggle(r.full_name)} data-testid={`ga-pick-${r.full_name}`} />
                      <span className="ga-mono" data-user-text>{r.full_name}</span>
                    </label>
                    <span className="ga-tag">{r.private ? "private" : "public"}</span>
                    {r.used_by.length ? (
                      <span className="ga-tag" data-tone="accent" data-testid={`ga-used-${r.full_name}`}
                            title={`used by ${r.used_by.join(", ")}`}>used by {r.used_by.join(", ")}</span>
                    ) : null}
                    {state ? (
                      <span className="ga-result" data-tone={TONE[state] ?? undefined} data-testid={`ga-result-${r.full_name}`}>
                        {result?.words ?? r.words}</span>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : null}
          {!done ? <p className="ga-faint">Then BOSS checks it can read, branch and open PRs, and says what it found.</p> : null}
        </section>
      ) : null}

      {/* 5 · other ways, collapsed */}
      <details className="ga-other" data-testid="ga-other">
        <summary>Other ways <CaretDown size={12} aria-hidden="true" /></summary>
        <ul>
          <li><button type="button" className="ga-link" onClick={() => onOther("token", found?.account.login ?? null)} data-testid="ga-other-token">
            <Key size={14} aria-hidden="true" /><b>Paste an access token</b></button>
            <span className="ga-faint">for a locked-down org. GitHub&apos;s token page opens with the permissions ticked.</span></li>
          {signin?.configured ? (
            <li><button type="button" className="ga-link" onClick={() => onOther("own", found?.account.login ?? null)} data-testid="ga-other-signin">
              <GithubLogo size={14} aria-hidden="true" /><b>Sign in with GitHub</b></button>
              <span className="ga-faint">your own account, one click.</span></li>
          ) : null}
          <li><button type="button" className="ga-link" onClick={onAdvanced} data-testid="ga-other-own-app">
            <Wrench size={14} aria-hidden="true" /><b>The org has its own GitHub App</b></button>
            <span className="ga-faint">an org admin sets it up in Advanced.</span></li>
        </ul>
      </details>

      <footer className="ga-foot">
        <span className="ga-tag" data-testid="ga-foot-note">{done ? (summary || "access checked") : path === "pick"
          ? "BOSS checks access after" : "≈ 1 minute · 2 clicks on GitHub"}</span>
        <span className="ga-spacer" />
        {done ? null : <button type="button" className="ga-btn" onClick={onClose} data-testid="ga-cancel">Cancel</button>}
        {primary}
      </footer>
    </div>
  );
}
