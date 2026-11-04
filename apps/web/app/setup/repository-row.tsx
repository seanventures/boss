"use client";

/**
 * ① Repository: where BOSS writes code. Paste a token, pick a repository from the token's own
 * list, and three real checks run — Read, Write, Pull requests — before anything says Working.
 */
import { GatedButton, useGate } from "../../components/gated";
import React, { forwardRef, useEffect, useRef, useState } from "react";
import { CircleNotch, Plus } from "@phosphor-icons/react";

import { idGet } from "../../lib/identity/api";
import { newFineGrainedTokenUrl } from "../../lib/github-urls";
import { fetchRepos, fetchSetup, SetupFailure, setupWrite } from "../../lib/setup-client";
import { ageLabel, daysUntil } from "../../lib/setup-stepper";
import type { CheckResult, RepoEntry, SetupRefusal, SetupView, TokenFound } from "../../lib/setup-types";
import { INSTALL_TIP, installLabel, type InstallNeeded } from "../../lib/identity/github-signin";
import { Drawer } from "./drawer";
import { statusOf } from "./github-access-row";
import { AskBoss, CONSENT_REFUSAL, Consent, RowMenu, toast, useBoard } from "./parts";
import { Out, Pill, Refusal, Tip } from "./pill";
import { RepoPicker } from "./repo-picker";
import { SetupRow } from "./setup-row";

const CHECK_TIPS = {
  read: "Can see the repository and its branches.",
  write: "Can push a branch. BOSS never pushes to your default branch.",
  pull_requests: "Can open a pull request for you to review.",
} as const;
const CHECK_LABELS = { read: "Read", write: "Write", pull_requests: "Pull requests" } as const;
/** An expiry worth saying: within a year (a 26,000-day token says nothing useful). */
const EXPIRY_SHOWN_DAYS = 365;

/** GitHub's new-token page with the two permissions BOSS needs filled in (spec §6.4). */
export function createTokenUrl(host: string): string {
  return newFineGrainedTokenUrl({
    name: `BOSS (${host})`.slice(0, 40),
    description: "BOSS writes branches and opens pull requests",
    expires_in: "90",
    contents: "write",
    pull_requests: "write",
    metadata: "read",
  });
}

export function refusalOf(error: unknown): SetupRefusal {
  if (error instanceof SetupFailure && error.refusal) return error.refusal;
  return { state: "failed", headline: "Check failed", fix: String((error as Error)?.message ?? error) };
}

export type SigninFound = TokenFound & { install_needed?: InstallNeeded[]; read_only?: number };

/** "Install BOSS on <org>": each account of yours where the BOSS app is not installed yet. */
export function InstallNeededLinks({ needed, onCheck, busy }: {
  needed: InstallNeeded[]; onCheck: () => void; busy: boolean;
}) {
  if (!needed.length) return null;
  return (
    <div className="su-line" data-testid="gh-install-needed">
      {needed.map((n) => (n.install_url ? (
        <Out key={n.account} href={n.install_url} testid={`gh-install-${n.account}`}
             tip={`BOSS isn't on ${n.account} yet. ${INSTALL_TIP}`}>{installLabel(n)}</Out>
      ) : null))}
      <button type="button" className="su-link" onClick={onCheck} disabled={busy} data-testid="gh-install-recheck"
              title="Ask GitHub again after installing." data-tip="Ask GitHub again after installing.">
        Check again
      </button>
    </div>
  );
}

/** Pick repos after "Sign in with GitHub": only repos you can push to where BOSS is installed. */
function SigninPicker({ onDone, makeDefault }: { onDone?: () => void; makeDefault: boolean }) {
  const { projectId, view, apply } = useBoard();
  const selectGate = useGate("repository:register");
  const [found, setFound] = useState<SigninFound | null>(null);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  const load = async () => {
    setBusy(true);
    setRefusal(null);
    try {
      setFound(await setupWrite<SigninFound>(projectId, "repo_signin", {},
                                             { label: "Pick repos", background: true }));
    } catch (error) { setRefusal(refusalOf(error)); } finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, [projectId]); // eslint-disable-line react-hooks/exhaustive-deps
  const connect = async (fullName: string) => {
    if (!found || !selectGate.allowed) return;
    setBusy(true);
    try {
      const next = await setupWrite<SetupView>(projectId, "repo_select", {
        connection_id: found.connection_id, full_name: fullName, make_default: makeDefault,
      }, { label: "Connect" });
      apply(next);
      const row = next.repository.repos.find((r) => r.full_name === fullName);
      if (row?.pill.tone === "green") toast("Connected · Working");
      onDone?.();
    } catch (error) { setRefusal(refusalOf(error)); } finally { setBusy(false); }
  };
  if (!found) {
    return refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} />
      : <span className="su-faint su-line"><CircleNotch className="su-spin" size={12} />Asking GitHub…</span>;
  }
  const readOnly = found.read_only ?? 0;
  return (
    <div className="su-form" data-testid="su-signin-picker">
      <div className="su-line">
        <span className="su-dot su-tone-green" aria-hidden="true" />
        <span>Your repos as <span className="su-mono" data-user-text>@{found.account_login}</span></span>
        {readOnly ? (
          <Tip tip="BOSS writes branches, so only repos you can push to are listed."
               className="su-faint" testid="su-signin-read-only">{readOnly} read-only hidden</Tip>
        ) : null}
      </div>
      {found.repos?.length ? (
        <RepoPicker repos={found.repos} busy={busy} onConnect={connect}
                    exclude={view.repository.repos.filter((r) => r.binding_id).map((r) => r.full_name)} />
      ) : <span className="su-faint" data-testid="su-signin-no-repos">No repos BOSS can reach yet</span>}
      <InstallNeededLinks needed={found.install_needed ?? []} onCheck={() => void load()} busy={busy} />
      {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
    </div>
  );
}

/** Paste a token → signed in → pick a repository → Connect. `connectionId` means Replace. */
export function TokenForm({ connectionId, onDone, makeDefault = true }: {
  connectionId?: string | null; onDone?: () => void; makeDefault?: boolean;
}) {
  const { signin } = useBoard();
  const [otherWays, setOtherWays] = useState(false);
  // THE ONE SIGN-IN FIRST: with "Sign in with GitHub" offered here, repos come from it; a token
  // and the org's App stay one click away ("Use a token instead").
  if (!connectionId && signin?.configured && !otherWays) {
    const signedIn = signin.me?.status === "active";
    return (
      <div className="su-stack">
        {signedIn ? <SigninPicker onDone={onDone} makeDefault={makeDefault} />
          : <span className="su-faint" data-testid="su-signin-first">Sign in with GitHub above</span>}
        <button type="button" className="su-link" onClick={() => setOtherWays(true)} data-testid="su-use-token"
                title="Paste a fine-grained token, or use the org's own GitHub App."
                data-tip="Paste a fine-grained token, or use the org's own GitHub App.">
          Use a token instead
        </button>
      </div>
    );
  }
  return <PasteTokenForm connectionId={connectionId} onDone={onDone} makeDefault={makeDefault} />;
}

export function PasteTokenForm({ connectionId, onDone, makeDefault = true, onFound }: {
  connectionId?: string | null; onDone?: () => void; makeDefault?: boolean;
  /** Setup › Add a GitHub account: hand the token's account and repos to the caller's own
   *  "Pick repos" step instead of this form's single picker. */
  onFound?: (found: TokenFound) => void;
}) {
  const { projectId, view, apply } = useBoard();
  const [token, setToken] = useState("");
  const [consent, setConsent] = useState(false);
  const [phase, setPhase] = useState<"idle" | "checking" | "found" | "connecting">("idle");
  const [found, setFound] = useState<TokenFound | null>(null);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const needsConsent = view.secrets.needs_consent;
  const host = typeof window !== "undefined" ? window.location.hostname : "this server";
  // THE APP FIRST: with a GitHub App connected in GitHub access, no token is pasted at all.
  const [appAccount, setAppAccount] = useState<string | null>(null);
  useEffect(() => {
    if (connectionId) return;
    let live = true;
    idGet<unknown>("/v1/identity/github").then((raw) => {
      const s = statusOf(raw);
      if (live && s?.app.installed) setAppAccount(s.app.account ?? "");
    }).catch(() => undefined);
    return () => { live = false; };
  }, [connectionId]);

  const useApp = async () => {
    setPhase("checking");
    setRefusal(null);
    try {
      const app = await setupWrite<TokenFound>(projectId, "repo_app", {}, { label: "Use the GitHub App" });
      if (onFound) { onFound(app); setPhase("idle"); return; }
      setFound(app);
      setPhase("found");
    } catch (error) {
      setRefusal(refusalOf(error));
      setPhase("idle");
    }
  };

  const tokenGate = useGate("github:connect");
  const selectGate = useGate("repository:register");
  const submit = async (value: string) => {
    if (!value.trim() || !tokenGate.allowed) return;
    if (needsConsent && !consent) {
      // NEVER SILENT: the press says what it is waiting for.
      setRefusal(CONSENT_REFUSAL);
      return;
    }
    if (timer.current) clearTimeout(timer.current);
    setPhase("checking");
    setRefusal(null);
    try {
      const answer = await setupWrite<TokenFound>(projectId, "repo_token", {
        token: value.trim(), connection_id: connectionId ?? undefined,
        accept_database_fallback: needsConsent ? consent : undefined,
      }, { label: "Connect GitHub" });
      if (connectionId && answer.rechecked?.length) {
        // Replace token: the bound repositories were re-checked with it; show the result.
        apply(await fetchSetup(projectId));
        onDone?.();
        return;
      }
      if (onFound) { onFound(answer); setPhase("idle"); return; }
      setFound(answer);
      setPhase("found");
    } catch (error) {
      setRefusal(refusalOf(error));
      setPhase("idle");
    } finally {
      // CLEARED EITHER WAY: the field never holds a token after it was sent.
      setToken("");
    }
  };

  const onChange = (value: string) => {
    setToken(value);
    if (timer.current) clearTimeout(timer.current);
    if (value.trim().length >= 20 && !(needsConsent && !consent)) {
      timer.current = setTimeout(() => void submit(value), 400);
    }
  };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const connect = async (fullName: string) => {
    if (!found || !selectGate.allowed) return;
    setPhase("connecting");
    try {
      const next = await setupWrite<SetupView>(projectId, "repo_select", {
        connection_id: found.connection_id, full_name: fullName, make_default: makeDefault,
      }, { label: "Connect" });
      apply(next);
      const row = next.repository.repos.find((r) => r.full_name === fullName);
      if (row?.pill.tone === "green") toast("Connected · Working");
      onDone?.();
    } catch (error) {
      setRefusal(refusalOf(error));
      setPhase("found");
    }
  };

  if (phase === "found" || phase === "connecting") {
    const days = daysUntil(found?.expires_at ?? null);
    return (
      <div className="su-form" data-testid="su-token-found">
        <div className="su-line">
          <span className="su-dot su-tone-green" aria-hidden="true" />
          <span>{found?.source === "github_app" ? "GitHub App on" : "Signed in as"} <span className="su-mono" data-user-text>@{found?.account_login}</span></span>
          {days !== null && days <= EXPIRY_SHOWN_DAYS ? (
            <Tip tip="Builds stop when it expires. Make a new token before then." className="su-faint">
              Expires in {days}d
            </Tip>
          ) : null}
          <button type="button" className="su-link" onClick={() => { setFound(null); setPhase("idle"); }}>
            Replace
          </button>
        </div>
        {phase === "connecting" ? <ChecksRunning /> : (
          <RepoPicker repos={found?.repos ?? []} busy={false} onConnect={connect}
                      preselect={view.repository.repos.find((r) => !r.binding_id)?.full_name}
                      exclude={view.repository.repos.filter((r) => r.binding_id)
                        .map((r) => r.full_name)} />
        )}
        {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
      </div>
    );
  }
  return (
    <div className="su-form" data-testid="su-token-form">
      {appAccount !== null ? (
        <div className="su-line">
          <GatedButton permission="github:connect" type="button" className="su-btn su-btn-primary" onClick={useApp} disabled={phase === "checking"}
                  data-testid="su-use-app" title="No token: the App's short-lived tokens, for its repos."
                  data-tip="No token: the App's short-lived tokens, for its repos.">
            Use the GitHub App{appAccount ? ` · @${appAccount}` : ""}
          </GatedButton>
          <span className="su-faint">or paste a token</span>
        </div>
      ) : null}
      <label className="su-field">
        <span className="su-sr">Token</span>
        <input type="password" autoComplete="off" spellCheck={false} value={token}
               placeholder="Paste token" aria-label="Paste token" data-testid="su-token-input"
               title="Stored in BOSS's secret store. Never shown again."
               {...tokenGate.props}
               disabled={(phase === "checking") || !tokenGate.allowed}
               onChange={(e) => onChange(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter") void submit(token); }} />
      </label>
      {phase === "checking" ? (
        <span className="su-faint su-line"><CircleNotch className="su-spin" size={12} />Checking token…</span>
      ) : null}
      {needsConsent ? <Consent checked={consent} onChange={setConsent} /> : null}
      <div className="su-line">
        <Out href={createTokenUrl(host)} tip="Opens GitHub with the two permissions BOSS needs filled in."
             testid="su-create-token">Create token</Out>
        <Tip tip="Org repo: pick the org as owner. Blocked? A classic repo-scope token works."
             className="su-faint su-tipped" testid="su-other-ways">Other ways</Tip>
      </div>
      {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
    </div>
  );
}

function ChecksRunning() {
  return (
    <div className="su-checks" data-testid="su-checks-running">
      {(["read", "write", "pull_requests"] as const).map((k) => (
        <Tip key={k} tip={CHECK_TIPS[k]} className="su-check">
          {CHECK_LABELS[k]} <CircleNotch className="su-spin" size={11} aria-label="checking" />
        </Tip>
      ))}
    </div>
  );
}

function Checks({ checks }: { checks: RepoEntry["checks"] }) {
  const mark = (c: CheckResult) => (c.ok === true ? "✓" : c.ok === false ? "✗" : "–");
  return (
    <span className="su-checks">
      {(["read", "write", "pull_requests"] as const).map((k) => (
        <Tip key={k} tip={CHECK_TIPS[k]} className="su-check" testid={`su-check-${k}`}>
          {CHECK_LABELS[k]} <span data-ok={String(checks[k].ok)}>{mark(checks[k])}</span>
        </Tip>
      ))}
    </span>
  );
}

function RepoLine({ repo, onReplace, single }: { repo: RepoEntry; onReplace: () => void; single: boolean }) {
  const { projectId, view, apply, checking, setChecking } = useBoard();
  const key = `repository:${repo.binding_id ?? "install"}`;
  const busy = checking.has(key) || checking.has("all");
  const install = view.repository.connection?.source === "install";
  const days = daysUntil(view.repository.connection?.expires_at ?? null);
  const pill = busy ? { state: "checking", tone: "busy" as const, label: "Checking…",
                        tip: "Asking GitHub what this token can do." } : repo.pill;
  const run = async (op: string, body: Record<string, unknown>, label: string) => {
    try {
      apply(await setupWrite<SetupView>(projectId, op, body, { label }));
    } catch (error) {
      toast(refusalOf(error).headline, "error");
    }
  };
  const test = async () => {
    setChecking(key, true);
    try { await run("checks", { target: key }, "Test"); } finally { setChecking(key, false); }
  };
  const red = pill.tone === "red";
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="su-repo" data-testid={`su-repo-${repo.full_name}`}>
      <div className="su-line">
        <Pill pill={pill} testid="su-repo-pill" />
        <span className="su-mono su-strong" data-user-text>{repo.full_name}</span>
        {repo.default ? <span className="su-chip">default</span> : null}
        {repo.default_branch ? <span className="su-faint" data-user-text>{repo.default_branch}</span> : null}
        {repo.private ? <span className="su-chip">Private</span> : null}
        {install ? (
          <Tip tip="Set in the server's settings file. Replace here to manage it in BOSS."
               className="su-chip">From install</Tip>
        ) : null}
      </div>
      <div className="su-line">
        <Checks checks={repo.checks} />
        {/* Once, and only when it means something: the pill already says "Expires in 2d"
            when it is close, and a token valid for decades needs no countdown. */}
        {days !== null && days <= EXPIRY_SHOWN_DAYS
          && !["expired", "expiring"].includes(repo.pill.state) ? (
          <Tip tip="Builds stop when it expires. Make a new token before then." className="su-faint">
            Expires in {days}d
          </Tip>
        ) : null}
      </div>
      {red || pill.tone === "amber" ? (
        <div className="su-line su-fix">
          {repo.pill.action === "fix_link" && repo.pill.fix_url ? (
            <Out href={repo.pill.fix_url} testid="su-fix-github">Fix on GitHub</Out>
          ) : null}
          {repo.pill.action === "replace" || repo.pill.action === "fix_link" ? (
            <GatedButton permission="github:connect" type="button" className="su-btn su-btn-sm" onClick={onReplace}
                    data-testid="su-replace-token">Replace token</GatedButton>
          ) : null}
          <GatedButton permission="setup:manage" type="button" className="su-btn su-btn-sm" onClick={test} disabled={busy}
                  data-testid="su-repo-test">Test</GatedButton>
          {red ? <AskBoss row="Repository" pillLabel={repo.pill.label} tip={repo.pill.tip} /> : null}
        </div>
      ) : null}
      {repo.checked_at ? (
        <Tip tip={`Last checked ${ageLabel(repo.checked_at)} ago.`} className="su-faint su-age">
          Checked {ageLabel(repo.checked_at)}
        </Tip>
      ) : null}
      <RowMenu label={`${repo.full_name} actions`} testid="su-repo-menu" items={[
        { label: "Test", onSelect: test, testid: "su-menu-test" },
        { label: "Replace token", onSelect: onReplace, testid: "su-menu-replace" },
        ...(!single && !repo.default && repo.binding_id ? [{
          label: "Make default", testid: "su-menu-default",
          onSelect: () => run("repo_default", { binding_id: repo.binding_id }, "Make default") }] : []),
        ...(!single && repo.binding_id ? [{
          label: "Remove", danger: true, testid: "su-menu-remove",
          onSelect: () => setConfirming(true) }] : []),
      ]} />
      {confirming ? (
        <div className="su-confirm" role="alertdialog" data-testid="su-repo-remove-confirm">
          <strong>Remove {repo.full_name}?</strong>
          <GatedButton permission="repository:manage" type="button" className="su-btn su-btn-sm su-btn-danger"
                  data-testid="su-repo-remove-yes"
                  onClick={() => { setConfirming(false);
                                   void run("repo_remove", { binding_id: repo.binding_id }, "Remove"); }}>
            Remove
          </GatedButton>
          <button type="button" className="su-link" onClick={() => setConfirming(false)}>Keep</button>
        </div>
      ) : null}
    </div>
  );
}

export function AddRepo({ connectionId, onDone }: { connectionId: string; onDone: () => void }) {
  const { projectId, view, apply } = useBoard();
  const [found, setFound] = useState<TokenFound | null>(null);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  useEffect(() => {
    let live = true;
    fetchRepos(projectId, connectionId).then((f) => { if (live) setFound(f); })
      .catch((error) => { if (live) setRefusal(refusalOf(error)); });
    return () => { live = false; };
  }, [projectId, connectionId]);
  if (refusal) return <Refusal headline={refusal.headline} fix={refusal.fix} />;
  if (!found) return <span className="su-faint su-line"><CircleNotch className="su-spin" size={12} />Checking token…</span>;
  return (
    <RepoPicker repos={found.repos} busy={busy}
                exclude={view.repository.repos.filter((r) => r.binding_id)
                  .map((r) => r.full_name)}
                onConnect={async (fullName) => {
                  setBusy(true);
                  try {
                    apply(await setupWrite<SetupView>(projectId, "repo_select", {
                      connection_id: connectionId, full_name: fullName, make_default: false,
                    }, { label: "Connect" }));
                    onDone();
                  } catch (error) {
                    setRefusal(refusalOf(error));
                  } finally {
                    setBusy(false);
                  }
                }} />
  );
}

export const RepositoryRow = forwardRef<HTMLElement, {
  mode: string; step: number | null; focused: boolean;
}>(function RepositoryRow({ mode, step, focused }, ref) {
  const { view, signin } = useBoard();
  const repos = view.repository.repos;
  const connection = view.repository.connection;
  const [drawer, setDrawer] = useState<null | "connect" | "replace">(null);
  const [adding, setAdding] = useState(false);
  const close = () => setDrawer(null);
  const replaceId = connection?.source === "connected" ? connection.id : null;

  let body: React.ReactNode;
  if (mode === "waiting") {
    body = <span className="su-faint">Not connected</span>;
  } else if (repos.length === 0) {
    body = mode === "current" ? (
      <div>
        <span className="su-row-title">{signin?.configured ? "Pick repos" : "Connect GitHub"}</span>
        <TokenForm />
      </div>
    ) : (
      <div className="su-line">
        <Pill pill={{ state: "not_connected", tone: "grey", label: "Not connected",
                      tip: "BOSS needs a repository to open pull requests." }} />
        <GatedButton permission="github:connect" type="button" className="su-btn su-btn-primary" data-testid="su-connect-github"
                onClick={() => setDrawer("connect")}>Connect GitHub</GatedButton>
      </div>
    );
  } else {
    body = (
      <div className="su-stack">
        {repos.map((repo) => (
          <RepoLine key={repo.binding_id ?? repo.full_name} repo={repo} single={repos.length === 1}
                    onReplace={() => setDrawer("replace")} />
        ))}
        {mode === "current" && drawer === "replace" ? (
          <TokenForm connectionId={replaceId} onDone={close} />
        ) : null}
        {connection && connection.source === "connected" ? (
          adding ? <AddRepo connectionId={connection.id} onDone={() => setAdding(false)} /> : (
            <GatedButton permission="repository:register" type="button" className="su-link su-add" data-testid="su-add-repo"
                    title="BOSS builds in the default repo unless a run picks another."
                    data-tip="BOSS builds in the default repo unless a run picks another."
                    onClick={() => setAdding(true)}>
              <Plus size={12} aria-hidden="true" />Add repo
            </GatedButton>
          )
        ) : null}
      </div>
    );
  }
  return (
    <SetupRow ref={ref} row="repository" label="Repos" step={step} mode={mode}
              done={mode === "done"} focused={focused}
              tip="Pick the repos BOSS writes code in and opens pull requests on.">
      {body}
      {connection?.builds && connection.builds.ok === false ? (
        <p className="su-builds-warn" data-testid="su-repo-builds" role="status">
          {connection.builds.message}{connection.builds.next ? ` ${connection.builds.next}` : ""}
        </p>
      ) : null}
      {drawer && mode !== "current" ? (
        <Drawer title="Connect GitHub" onClose={close} testid="su-github-drawer">
          <TokenForm connectionId={drawer === "replace" ? replaceId : null} onDone={close} />
        </Drawer>
      ) : null}
    </SetupRow>
  );
});
