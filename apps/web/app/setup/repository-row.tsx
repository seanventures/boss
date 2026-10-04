"use client";

/**
 * ① Repository: where BOSS writes code. Paste a token, pick a repository from the token's own
 * list, and three real checks run — Read, Write, Pull requests — before anything says Working.
 */
import React, { forwardRef, useEffect, useRef, useState } from "react";
import { CircleNotch, Plus } from "@phosphor-icons/react";

import { fetchRepos, SetupFailure, setupWrite } from "../../lib/setup-client";
import { ageLabel, daysUntil } from "../../lib/setup-stepper";
import type { CheckResult, RepoEntry, SetupRefusal, SetupView, TokenFound } from "../../lib/setup-types";
import { Drawer } from "./drawer";
import { AskBoss, Consent, RowMenu, toast, useBoard } from "./parts";
import { Out, Pill, Refusal, Tip } from "./pill";
import { RepoPicker } from "./repo-picker";
import { SetupRow } from "./setup-row";

const CHECK_TIPS = {
  read: "Can see the repository and its branches.",
  write: "Can push a branch. BOSS never pushes to your default branch.",
  pull_requests: "Can open a pull request for you to review.",
} as const;
const CHECK_LABELS = { read: "Read", write: "Write", pull_requests: "Pull requests" } as const;

/** GitHub's new-token page with the two permissions BOSS needs filled in (spec §6.4). */
export function createTokenUrl(host: string): string {
  const params = new URLSearchParams({
    name: `BOSS (${host})`.slice(0, 40),
    description: "BOSS writes branches and opens pull requests",
    expires_in: "90",
    contents: "write",
    pull_requests: "write",
    metadata: "read",
  });
  return `https://github.com/settings/personal-access-tokens/new?${params.toString()}`;
}

function refusalOf(error: unknown): SetupRefusal {
  if (error instanceof SetupFailure && error.refusal) return error.refusal;
  return { state: "failed", headline: "Check failed", fix: String((error as Error)?.message ?? error) };
}

/** Paste a token → signed in → pick a repository → Connect. `connectionId` means Replace. */
export function TokenForm({ connectionId, onDone, makeDefault = true }: {
  connectionId?: string | null; onDone?: () => void; makeDefault?: boolean;
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

  const submit = async (value: string) => {
    if (!value.trim() || (needsConsent && !consent)) return;
    if (timer.current) clearTimeout(timer.current);
    setPhase("checking");
    setRefusal(null);
    try {
      const answer = await setupWrite<TokenFound>(projectId, "repo_token", {
        token: value.trim(), connection_id: connectionId ?? undefined,
        accept_database_fallback: needsConsent ? consent : undefined,
      }, { label: "Connect GitHub" });
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
    if (value.trim().length >= 20) timer.current = setTimeout(() => void submit(value), 400);
  };
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const connect = async (fullName: string) => {
    if (!found) return;
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
          <span>Signed in as <span className="su-mono" data-user-text>@{found?.account_login}</span></span>
          {days !== null ? (
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
                      exclude={view.repository.repos.map((r) => r.full_name)} />
        )}
        {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
      </div>
    );
  }
  return (
    <div className="su-form" data-testid="su-token-form">
      <label className="su-field">
        <span className="su-sr">Token</span>
        <input type="password" autoComplete="off" spellCheck={false} value={token}
               placeholder="Paste token" aria-label="Paste token" data-testid="su-token-input"
               title="Stored in BOSS's secret store. Never shown again."
               disabled={phase === "checking"}
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
        <Tip tip="Org repo: pick the org as owner. Blocked? A classic token with repo scope works."
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

function RepoLine({ repo, onReplace }: { repo: RepoEntry; onReplace: () => void }) {
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
        {days !== null && repo.pill.state !== "expired" ? (
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
            <button type="button" className="su-btn su-btn-sm" onClick={onReplace}
                    data-testid="su-replace-token">Replace token</button>
          ) : null}
          <button type="button" className="su-btn su-btn-sm" onClick={test} disabled={busy}
                  data-testid="su-repo-test">Test</button>
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
        ...(!repo.default && repo.binding_id ? [{
          label: "Make default", testid: "su-menu-default",
          onSelect: () => run("repo_default", { binding_id: repo.binding_id }, "Make default") }] : []),
        ...(repo.binding_id ? [{
          label: "Remove", danger: true, testid: "su-menu-remove",
          onSelect: () => run("repo_remove", { binding_id: repo.binding_id }, "Remove") }] : []),
      ]} />
    </div>
  );
}

function AddRepo({ connectionId, onDone }: { connectionId: string; onDone: () => void }) {
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
                exclude={view.repository.repos.map((r) => r.full_name)}
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
  const { view } = useBoard();
  const repos = view.repository.repos;
  const connection = view.repository.connection;
  const [drawer, setDrawer] = useState<null | "connect" | "replace">(null);
  const [adding, setAdding] = useState(false);
  const first = repos.find((r) => r.default) ?? repos[0];
  const close = () => setDrawer(null);
  const replaceId = connection?.source === "connected" ? connection.id : null;

  let body: React.ReactNode;
  if (mode === "done" && first) {
    body = <span className="su-line"><span className="su-mono" data-user-text>{first.full_name}</span></span>;
  } else if (mode === "waiting") {
    body = <span className="su-faint">Not connected</span>;
  } else if (repos.length === 0) {
    body = mode === "current" ? (
      <div>
        <span className="su-row-title">Connect GitHub</span>
        <TokenForm />
      </div>
    ) : (
      <div className="su-line">
        <Pill pill={{ state: "not_connected", tone: "grey", label: "Not connected",
                      tip: "BOSS needs a repository to open pull requests." }} />
        <button type="button" className="su-btn su-btn-primary" data-testid="su-connect-github"
                onClick={() => setDrawer("connect")}>Connect GitHub</button>
      </div>
    );
  } else {
    body = (
      <div className="su-stack">
        {repos.map((repo) => (
          <RepoLine key={repo.binding_id ?? repo.full_name} repo={repo}
                    onReplace={() => setDrawer("replace")} />
        ))}
        {mode === "current" && drawer === "replace" ? (
          <TokenForm connectionId={replaceId} onDone={close} />
        ) : null}
        {connection && connection.source === "connected" ? (
          adding ? <AddRepo connectionId={connection.id} onDone={() => setAdding(false)} /> : (
            <button type="button" className="su-link su-add" data-testid="su-add-repo"
                    title="BOSS builds in the default repo unless a run picks another."
                    data-tip="BOSS builds in the default repo unless a run picks another."
                    onClick={() => setAdding(true)}>
              <Plus size={12} aria-hidden="true" />Add repo
            </button>
          )
        ) : null}
      </div>
    );
  }
  return (
    <SetupRow ref={ref} row="repository" label="Repository" step={step} mode={mode}
              done={mode === "done"} focused={focused}
              tip="Where BOSS writes code and opens pull requests.">
      {body}
      {drawer && mode !== "current" ? (
        <Drawer title="Connect GitHub" onClose={close} testid="su-github-drawer">
          <TokenForm connectionId={drawer === "replace" ? replaceId : null} onDone={close} />
        </Drawer>
      ) : null}
    </SetupRow>
  );
});
