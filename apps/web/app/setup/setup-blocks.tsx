"use client";

/**
 * Setup on one page (BOSSAPP-808 screen 2): three blocks — GitHub accounts, Repositories BOSS can
 * work on, Models — each a short table with one "+ Add" action. Every pill comes from the server
 * (GET /v1/setup `github_accounts`, `repositories`, `models`); nothing here maps a state to a tone.
 * The forms are the existing ones (AddAccountPanel's steps, AddRepo, TokenForm), in drawers.
 *
 * ONE PLACE PER THING (operator, 2026-11-05: "We have two areas to add model … which one is
 * correct?"). Each repository row carries its own actions (Test, Make default, Replace token,
 * Remove); Models is the full list (setup-board.tsx renders ModelsRow as a block after these);
 * Advanced settings holds only what is not shown here.
 */
import React, { useMemo, useState } from "react";
import { Plus } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { fetchSetup, setupWrite } from "../../lib/setup-client";
import { ageLabel } from "../../lib/setup-stepper";
import type { GitHubAccount, Pill as PillT, SetupRepository, SetupView } from "../../lib/setup-types";
import { AddAccountPanel } from "./add-account-panel";
import { Drawer } from "./drawer";
import { RowMenu, toast, useBoard, type MenuItem } from "./parts";
import { Out, Pill } from "./pill";
import { AddRepo, refusalOf, TokenForm } from "./repository-row";
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
  | { kind: "repo"; connectionId: string | null };

/** One repository's actions, on its own row: Test, Make default, Replace token, Remove (asked
 *  first). The same server operations the earlier Repos row used; a repo bound to another
 *  project only (no binding here) has none. */
function RepoActions({ repo, onReplace }: { repo: SetupRepository; onReplace: (connectionId: string | null) => void }) {
  const { projectId, view, apply, checking, setChecking } = useBoard();
  const [confirming, setConfirming] = useState(false);
  const bound = view.repository.repos.filter((r) => r.binding_id);
  const entry = bound.find((r) => r.full_name === repo.full_name) ?? null;
  if (!entry) return null;
  const key = `repository:${entry.binding_id}`;
  const busy = checking.has(key) || checking.has("all");
  const run = async (op: string, body: Record<string, unknown>, label: string, done?: string) => {
    try {
      apply(await setupWrite<SetupView>(projectId, op, body, { label }));
      if (done) toast(done);
    } catch (error) {
      toast(refusalOf(error).headline, "error");
    }
  };
  const test = async () => {
    setChecking(key, true);
    try { await run("checks", { target: key }, "Test"); } finally { setChecking(key, false); }
  };
  const single = bound.length === 1;
  const account = view.github_accounts?.find((a) => a.id === repo.account_id || a.connection_id === repo.account_id);
  const viaApp = account?.kind === "org_app" || /GitHub App/.test(account?.how ?? "");
  const items: MenuItem[] = [
    { label: "Test", onSelect: () => void test(), testid: "su-repo-menu-test", disabled: busy },
    ...(!single && !repo.default ? [{ label: "Make default", testid: "su-repo-menu-default",
      onSelect: () => void run("repo_default", { binding_id: entry.binding_id }, "Make default",
                               `${repo.full_name} is the default`) }] : []),
    // A GitHub App connection has no token to replace: its access renews itself.
    ...(!viaApp ? [{ label: "Replace token", testid: "su-repo-menu-replace", onSelect: () => onReplace(repo.account_id) }] : []),
    ...(!single ? [{ label: "Remove", danger: true, testid: "su-repo-menu-remove", onSelect: () => setConfirming(true) }] : []),
  ];
  return (
    <span className="su-line su-row-actions">
      {repo.pill?.action === "fix_link" && repo.pill.fix_url ? (
        <Out href={repo.pill.fix_url} testid="su-repo-fix-github">Fix on GitHub</Out>
      ) : null}
      {repo.pill && (repo.pill.tone === "red" || repo.pill.tone === "amber") && repo.pill.action === "replace" ? (
        <GatedButton permission="github:connect" type="button" className="su-btn su-btn-sm"
                     data-testid="su-repo-replace" onClick={() => onReplace(repo.account_id)}>Replace token</GatedButton>
      ) : null}
      <RowMenu label={`${repo.full_name} actions`} testid="su-repo-row-menu" items={items} />
      {confirming ? (
        <span className="su-confirm" role="alertdialog" data-testid="su-repo-row-remove-confirm">
          <strong>Remove {repo.full_name}?</strong>
          <GatedButton permission="repository:manage" type="button" className="su-btn su-btn-sm su-btn-danger"
                       data-testid="su-repo-row-remove-yes"
                       onClick={() => { setConfirming(false);
                                        void run("repo_remove", { binding_id: entry.binding_id }, "Remove", "Removed"); }}>
            Remove
          </GatedButton>
          <button type="button" className="su-link" onClick={() => setConfirming(false)}>Keep</button>
        </span>
      ) : null}
    </span>
  );
}

export function SetupBlocks({ onAdvanced, initialOpen = null }: {
  onAdvanced: (focus?: string) => void;
  /** A deep link from Get started: open Add GitHub account at once. */
  initialOpen?: "account" | null;
}) {
  const { view, signin, projectId, apply, checking } = useBoard();
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
            <thead><tr><th>Repository</th><th>Via</th><th>Used by</th><th>Access</th><th><span className="su-sr">Actions</span></th></tr></thead>
            <tbody>
              {repos.map((r) => {
                const entry = view.repository.repos.find((x) => x.binding_id && x.full_name === r.full_name);
                const busy = checking.has("all") || (entry ? checking.has(`repository:${entry.binding_id}`) : false);
                return (
                <tr key={r.full_name} data-testid={`su-repo-row-${r.full_name}`}>
                  <td>
                    <span className="su-mono" data-user-text>{r.full_name}</span>
                    {r.default ? <span className="su-chip">default</span> : null}
                  </td>
                  <td className="su-faint">{r.via ? `via ${r.via}` : "—"}</td>
                  <td className="su-faint" data-user-text>{r.projects.length ? r.projects.map((p) => p.name).join(", ")
                    : r.in_project ? "This project" : "—"}</td>
                  <td>
                    {busy ? <Pill pill={{ state: "checking", tone: "busy", label: "Checking…",
                                          tip: "Asking GitHub what BOSS can do here." }} testid="su-repo-row-pill" />
                      : r.pill && r.pill.tone !== "green" ? <Pill pill={r.pill} testid="su-repo-row-pill" />
                      : <span data-testid="su-repo-access"
                              title={entry?.checked_at ? `Checked ${ageLabel(entry.checked_at)} ago.` : undefined}
                              data-tip={entry?.checked_at ? `Checked ${ageLabel(entry.checked_at)} ago.` : undefined}>
                          {accessLine(r.checks)}</span>}
                  </td>
                  <td className="su-td-actions">
                    <RepoActions repo={r} onReplace={(connectionId) => setOpen({ kind: "replace", connectionId })} />
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        ) : <span className="su-faint" data-testid="su-repos-empty">No repos yet</span>}
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
          {/* EACH JOB IS ITS OWN CHOICE (operator: "we should provide options ... so customers
              can use multiple models"): a chip opens routing on that job. */}
          <button type="button" className="su-chip su-chip-btn" onClick={onChange} data-testid={`su-who-${j.job}`}
                  title={`${j.resolved!.why} Click to choose the model for ${j.label}.`}
                  data-tip={`${j.resolved!.why} Click to choose the model for ${j.label}.`}>
            {j.label} → {j.resolved!.label}
          </button>
          {j.resolved!.pill ? <Pill pill={j.resolved!.pill} testid={`su-who-pill-${j.job}`} /> : null}
        </React.Fragment>
      ))}
      <button type="button" className="su-link" onClick={onChange} data-testid="su-who-change">Change</button>
    </div>
  );
}
