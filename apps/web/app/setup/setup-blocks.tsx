"use client";

/**
 * Setup on one page (BOSSAPP-808 screen 2): three blocks — GitHub accounts, Repositories BOSS can
 * work on, Models — each a short table with one "+ Add" action. Every pill comes from the server
 * (GET /v1/setup `github_accounts`, `repositories`, `models`); nothing here maps a state to a tone.
 * The forms are the existing ones (AddAccountPanel's steps, AddRepo, AddModelPanel, TokenForm), in
 * drawers; everything else stays under Advanced settings.
 */
import React, { useMemo, useState } from "react";
import { Plus } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { fetchSetup } from "../../lib/setup-client";
import { defaultAdd } from "../../lib/setup-stepper";
import type { GitHubAccount, Pill as PillT, SetupRepository, SetupView } from "../../lib/setup-types";
import { AddAccountPanel } from "./add-account-panel";
import { AddModelPanel } from "./add-model-drawer";
import { Drawer } from "./drawer";
import { usedForLabel, usedForTip } from "./models-row";
import { useBoard } from "./parts";
import { Pill, Tip } from "./pill";
import { AddRepo, TokenForm } from "./repository-row";
import type { SigninStatus } from "../../lib/identity/github-signin";

const CHECK_NAMES = { read: "Read", write: "Write", pull_requests: "PRs" } as const;

/** The accounts the server listed; an older API (no `github_accounts`) shows its one connection. */
export function accountsOf(view: SetupView, signin?: SigninStatus | null): GitHubAccount[] {
  let rows: GitHubAccount[] = view.github_accounts ? [...view.github_accounts] : [];
  if (!view.github_accounts && view.repository.connection) {
    const c = view.repository.connection;
    const pill = view.repository.repos[0]?.pill
      ?? { state: "no_repos", tone: "grey" as const, label: "No repos yet", tip: "" };
    rows = [{
      id: c.id, connection_id: c.source === "connected" ? c.id : null,
      scope: c.source === "install" ? "install" : "project",
      kind: c.auth === "app" ? "org_app" : c.source === "install" ? "install" : "token",
      name: c.account_login ?? "GitHub", login: c.account_login, how: c.auth === "app" ? "BOSS GitHub App" : "Access token",
      personal: null, repo_count: view.repository.repos.length, expires_at: c.expires_at, pill,
    }];
  }
  // "Sign in with GitHub" done but no repo picked through it yet: the person's account is a row.
  const me = signin?.me?.status === "active" ? signin.me : null;
  if (me?.login && !rows.some((r) => (r.login ?? "").toLowerCase() === me.login!.toLowerCase())) {
    rows.push({
      id: `signin:${me.id}`, connection_id: null, scope: "project", kind: "personal_signin",
      name: me.login, login: me.login, how: "Personal · signed in with GitHub", personal: true,
      repo_count: 0, expires_at: null,
      pill: { state: "signed_in", tone: "green", label: "Connected", tip: "Signed in. Add a repo to build in." },
    });
  }
  return rows;
}

/** The repositories the server listed; an older API shows this project's repos. */
export function reposOf(view: SetupView): SetupRepository[] {
  if (view.repositories) return view.repositories;
  return view.repository.repos.map((r) => ({
    full_name: r.full_name, binding_id: r.binding_id, in_project: true, default: r.default,
    account_id: view.repository.connection?.id ?? null, via: view.repository.connection?.account_login ?? null,
    projects: [], checks: r.checks, pill: r.pill,
  }));
}

/** "Read · Write · PRs ✓", or the checks that did not pass ("Write ✗"). Text only, no tone. */
export function accessLine(checks: SetupRepository["checks"]): string {
  if (!checks) return "Not checked";
  const keys = ["read", "write", "pull_requests"] as const;
  if (keys.every((k) => checks[k].ok === true)) return "Read · Write · PRs ✓";
  const failed = keys.filter((k) => checks[k].ok === false).map((k) => `${CHECK_NAMES[k]} ✗`);
  if (failed.length) return failed.join(" · ");
  return "Not checked";
}

function BlockHead({ title, tip, action }: { title: string; tip: string; action: React.ReactNode }) {
  return (
    <header className="su-block-head">
      <h2 className="su-block-title" title={tip} data-tip={tip}>{title}</h2>
      <span className="su-spacer" />
      {action}
    </header>
  );
}

type Opening = { kind: "account" } | { kind: "replace"; connectionId: string | null }
  | { kind: "repo"; connectionId: string | null } | { kind: "model" };

export function SetupBlocks({ onAdvanced, initialOpen = null }: {
  onAdvanced: (focus?: string) => void;
  /** A deep link from Get started: open Add GitHub account / Add model at once. */
  initialOpen?: "account" | "model" | null;
}) {
  const { view, signin, projectId, apply } = useBoard();
  const [open, setOpen] = useState<Opening | null>(initialOpen ? { kind: initialOpen } : null);
  // CLOSING RE-READS THE BOARD (as Models does): a sign-in or check that finished shows at once.
  const close = () => { setOpen(null); void fetchSetup(projectId).then(apply).catch(() => undefined); };
  const accounts = useMemo(() => accountsOf(view, signin), [view, signin]);
  const repos = reposOf(view);
  const connectable = accounts.filter((a) => a.connection_id);

  const fix = (a: GitHubAccount) => {
    const action = a.pill.action;
    if (a.kind === "org_app" || action === "fix_link") { onAdvanced("github_app"); return; }
    if (action === "sign_in") { setOpen({ kind: "account" }); return; }
    setOpen({ kind: "replace", connectionId: a.connection_id });
  };

  return (
    <>
      {/* ── GitHub accounts ── */}
      <section className="su-block" data-testid="su-accounts" aria-label="GitHub accounts">
        <BlockHead title="GitHub accounts" tip="Each account BOSS uses to read code and open pull requests."
                   action={(
                     <GatedButton permission="github:connect" type="button" className="su-btn su-btn-primary"
                                  data-testid="su-add-account-open" onClick={() => setOpen({ kind: "account" })}>
                       <Plus size={12} weight="bold" aria-hidden="true" />Add GitHub account
                     </GatedButton>
                   )} />
        {accounts.length ? (
          <table className="su-table">
            <thead><tr><th>Account</th><th>How</th><th>Repos</th><th>Status</th></tr></thead>
            <tbody>
              {accounts.map((a) => (
                <tr key={a.id} data-testid={`su-account-${a.id}`}>
                  <td><span className="su-strong" data-user-text>{a.name}</span></td>
                  <td className="su-faint">{a.how}</td>
                  <td>{a.repo_count ?? "—"}</td>
                  <td>
                    <span className="su-line">
                      <Pill pill={a.pill as PillT} testid="su-account-pill" />
                      {a.pill.tone === "red" || a.pill.tone === "amber" ? (
                        <button type="button" className="su-link" data-testid="su-account-fix" onClick={() => fix(a)}>
                          Fix
                        </button>
                      ) : null}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <span className="su-faint" data-testid="su-accounts-empty">No GitHub account yet</span>}
      </section>

      {/* ── Repositories ── */}
      <section className="su-block" data-testid="su-repos" aria-label="Repositories BOSS can work on">
        <BlockHead title="Repositories BOSS can work on" tip="BOSS writes branches and opens pull requests here."
                   action={(
                     <GatedButton permission="repository:register" type="button" className="su-btn"
                                  data-testid="su-add-repo-open"
                                  onClick={() => setOpen(connectable.length ? { kind: "repo", connectionId: connectable[0].connection_id }
                                                                            : { kind: "account" })}>
                       <Plus size={12} weight="bold" aria-hidden="true" />Add repo
                     </GatedButton>
                   )} />
        {repos.length ? (
          <table className="su-table">
            <thead><tr><th>Repository</th><th>Via</th><th>Used by</th><th>Access</th></tr></thead>
            <tbody>
              {repos.map((r) => (
                <tr key={r.full_name} data-testid={`su-repo-row-${r.full_name}`}>
                  <td>
                    <span className="su-mono" data-user-text>{r.full_name}</span>
                    {r.default ? <span className="su-chip">default</span> : null}
                  </td>
                  <td className="su-faint">{r.via ? `via ${r.via}` : "—"}</td>
                  <td className="su-faint" data-user-text>{r.projects.length ? r.projects.map((p) => p.name).join(", ")
                    : r.in_project ? "This project" : "—"}</td>
                  <td>
                    {r.pill && r.pill.tone !== "green" ? <Pill pill={r.pill} testid="su-repo-row-pill" />
                      : <span data-testid="su-repo-access">{accessLine(r.checks)}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <span className="su-faint" data-testid="su-repos-empty">No repos yet</span>}
      </section>

      {/* ── Models ── */}
      <section className="su-block" data-testid="su-models-block" aria-label="Models">
        <BlockHead title="Models" tip="What BOSS thinks with: subscriptions, API keys and local models."
                   action={(
                     <GatedButton permission="model_provider:manage" type="button" className="su-btn"
                                  data-testid="su-add-model-block" onClick={() => setOpen({ kind: "model" })}>
                       <Plus size={12} weight="bold" aria-hidden="true" />Add model
                     </GatedButton>
                   )} />
        {view.models.length ? (
          <div className="su-cards">
            {view.models.map((m) => (
              <article key={m.source_id} className="su-card" data-testid={`su-model-card-${m.source_id}`}>
                <span className="su-strong" data-user-text>{m.label}</span>
                <span className="su-faint">{m.kind === "subscription"
                  ? (m.pill.tone === "green" ? "Subscription · signed in" : "Subscription")
                  : m.kind === "local" ? "Local" : "API key"}</span>
                <Pill pill={m.pill} testid="su-model-card-pill" />
                <Tip tip={usedForTip(m.used_for)} className="su-faint">Used for: {usedForLabel(m.used_for)}</Tip>
              </article>
            ))}
          </div>
        ) : <span className="su-faint" data-testid="su-models-empty">No model yet</span>}
        <WhoDoesWhat view={view} onChange={() => onAdvanced("jobs")} />
      </section>

      {open?.kind === "account" ? (
        <AddAccountPanel onClose={close} onAdvanced={() => { close(); onAdvanced("github_app"); }} />
      ) : null}
      {open?.kind === "replace" ? (
        <Drawer title="Fix GitHub access" onClose={close} testid="su-fix-account">
          <TokenForm connectionId={open.connectionId} onDone={close} replace />
        </Drawer>
      ) : null}
      {open?.kind === "repo" ? (
        <Drawer title="Add repo" onClose={close} testid="su-add-repo-drawer">
          <div className="su-stack">
            {connectable.length > 1 ? (
              <div className="su-chips" role="radiogroup" aria-label="Account">
                {connectable.map((a) => (
                  <button key={a.id} type="button" role="radio" aria-checked={open.connectionId === a.connection_id}
                          className="su-chip" data-testid={`su-add-repo-via-${a.id}`}
                          onClick={() => setOpen({ kind: "repo", connectionId: a.connection_id })}>
                    {a.name}
                  </button>
                ))}
              </div>
            ) : null}
            {open.connectionId ? <AddRepo key={open.connectionId} connectionId={open.connectionId} onDone={close} /> : null}
            <button type="button" className="su-link" data-testid="su-add-repo-new-account"
                    onClick={() => setOpen({ kind: "account" })}>From another account</button>
          </div>
        </Drawer>
      ) : null}
      {open?.kind === "model" ? (
        <Drawer title="Add a model" onClose={close} testid="su-add-model-block-drawer">
          <AddModelPanel initialTile={defaultAdd(view).tile} initialProvider={defaultAdd(view).provider} onDone={close} />
        </Drawer>
      ) : null}
    </>
  );
}

/** "Who does what:" one chip per main job, "Build → Claude Code", and Change (routing). */
export function WhoDoesWhat({ view, onChange }: { view: SetupView; onChange: () => void }) {
  const jobs = view.jobs.filter((j) => !j.more && j.resolved);
  if (!jobs.length) return null;
  return (
    <div className="su-line su-who" data-testid="su-who-does-what">
      <span className="su-faint">Who does what:</span>
      {jobs.map((j) => (
        <React.Fragment key={j.job}>
          <Tip tip={j.resolved!.why} className="su-chip" testid={`su-who-${j.job}`}>
            {j.label} → {j.resolved!.label}
          </Tip>
          {j.resolved!.pill ? <Pill pill={j.resolved!.pill} testid={`su-who-pill-${j.job}`} /> : null}
        </React.Fragment>
      ))}
      <button type="button" className="su-link" onClick={onChange} data-testid="su-who-change">Change</button>
    </div>
  );
}
