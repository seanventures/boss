"use client";

/**
 * A gate pass for an in-progress app (BOSSAPP-1251).
 *
 * One named link, a password, and an expiry. An admin can stop a pass or stop
 * new ones. The password is typed into a concealed field and then cleared. It
 * is never written into the page or the console.
 */
import React, { useEffect, useState } from "react";

export type PreviewShareLink = {
  id: string;
  name: string;
  slug: string;
  href: string;
  appKey: string;
  expiresAt: string;
  stopped: boolean;
  expired: boolean;
};

export type ShareDraft = {
  name: string;
  password: string;
  expiresAt: string;
};

export type OpenResult =
  | { ok: true }
  | { ok: false; reason: "expired" | "stopped" | "password" };

export type PreviewShareProps = {
  projectId?: string;
  appKey?: string;
  appName?: string;
  actor?: string;
  admin?: boolean;
  newLinksStopped?: boolean;
  shares?: PreviewShareLink[];
  onCreate?: (input: ShareDraft) => Promise<PreviewShareLink | { error: string }>;
  onStop?: (id: string) => Promise<unknown>;
  onStopNew?: () => Promise<unknown>;
  onOpen?: (slug: string, password: string) => Promise<OpenResult>;
};

const TIP = "Not written to the log.";

function stateOf(row: PreviewShareLink): "Open" | "Stopped" | "Expired" {
  if (row.stopped) return "Stopped";
  if (row.expired) return "Expired";
  return "Open";
}

function refusal(reason: Extract<OpenResult, { ok: false }>["reason"] | string): string {
  if (reason === "expired") return "That pass expired.";
  if (reason === "stopped") return "That pass was stopped.";
  return "Password refused.";
}

function fromApi(row: Record<string, unknown>): PreviewShareLink {
  return {
    id: String(row.id ?? ""),
    name: String(row.name ?? ""),
    slug: String(row.slug ?? ""),
    href: String(row.href ?? ""),
    appKey: String(row.app_key ?? row.appKey ?? ""),
    expiresAt: String(row.expires_at ?? row.expiresAt ?? ""),
    stopped: Boolean(row.stopped),
    expired: Boolean(row.expired),
  };
}

export function PreviewShare(props: PreviewShareProps) {
  const controlled = props.shares !== undefined;
  const [rows, setRows] = useState<PreviewShareLink[]>(props.shares ?? []);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [expiry, setExpiry] = useState("");
  const [openPassword, setOpenPassword] = useState("");
  const [openError, setOpenError] = useState("");
  const [issueError, setIssueError] = useState("");
  const [stoppedNew, setStoppedNew] = useState(Boolean(props.newLinksStopped));

  useEffect(() => {
    if (controlled) setRows(props.shares ?? []);
  }, [controlled, props.shares]);

  useEffect(() => {
    setStoppedNew(Boolean(props.newLinksStopped));
  }, [props.newLinksStopped]);

  useEffect(() => {
    if (controlled || !props.projectId) return;
    let live = true;
    const projectId = props.projectId;
    fetch(`/v1/preview-shares?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store" })
      .then((response) => response.json())
      .then((body) => {
        if (!live) return;
        const shares = Array.isArray(body?.shares) ? body.shares.map(fromApi) : [];
        setRows(shares);
      })
      .catch(() => {
        if (live) setIssueError("The pass list did not load.");
      });
    return () => {
      live = false;
    };
  }, [controlled, props.projectId]);

  async function issue() {
    if (stoppedNew || !name.trim() || !password || !expiry) return;
    const draft = { name: name.trim(), password, expiresAt: expiry };
    setIssueError("");
    const made = props.onCreate
      ? await props.onCreate(draft)
      : await postShare(props, draft);
    setPassword("");
    if (!made || !("href" in made) || !made.href) {
      setIssueError("error" in (made || {}) ? String((made as { error: string }).error) : "Pass was not issued.");
      return;
    }
    setRows((prev) => [...prev, made]);
    setName("");
    setExpiry("");
  }

  async function stopOne(id: string) {
    if (props.onStop) await props.onStop(id);
    else await postJson(`/v1/preview-shares/${encodeURIComponent(id)}/stop`, actorBody(props));
    setRows((prev) => prev.map((row) => (row.id === id ? { ...row, stopped: true } : row)));
  }

  async function stopNew() {
    if (props.onStopNew) await props.onStopNew();
    else await postJson("/v1/preview-shares/stop-new", actorBody(props));
    setStoppedNew(true);
  }

  async function open(row: PreviewShareLink) {
    const typed = openPassword;
    setOpenPassword("");
    const result = props.onOpen
      ? await props.onOpen(row.slug, typed)
      : await postOpen(props, row.slug, typed);
    if (!result.ok) setOpenError(refusal(result.reason));
    else setOpenError("");
  }

  return (
    <section className="ps1251" data-testid="preview-share" aria-label="Preview share">
      <style>{CSS}</style>
      <header className="ps1251-head">
        <span className="ps1251-kicker">Gate pass</span>
        <h1>{props.appName || "Unfinished app"}</h1>
      </header>
      <p className="ps1251-lead">This pass opens the unfinished app until it expires.</p>
      <ol className="ps1251-passes">
        {rows.map((row) => {
          const state = stateOf(row);
          const live = state === "Open";
          return (
            <li key={row.id} className="ps1251-pass" data-state={state.toLowerCase()} data-testid="share-row">
              <div>
                <strong>{row.name}</strong>
                <code>{row.href}</code>
                <span className="ps1251-app">{row.appKey}</span>
              </div>
              <div className="ps1251-stamp" data-testid="share-state">{state}</div>
              {live && props.admin ? (
                <button type="button" data-testid="stop-pass" onClick={() => void stopOne(row.id)}>
                  Stop this pass
                </button>
              ) : null}
              {live ? (
                <div className="ps1251-open">
                  <label>
                    Password
                    <input
                      data-testid="open-password"
                      type="password"
                      title={TIP}
                      value={openPassword}
                      autoComplete="off"
                      onChange={(event) => setOpenPassword(event.target.value)}
                    />
                  </label>
                  <button type="button" data-testid="open-pass" onClick={() => void open(row)}>
                    Open pass
                  </button>
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      {openError ? <p className="ps1251-error" role="alert">{openError}</p> : null}
      {stoppedNew ? <p className="ps1251-error">New passes are stopped.</p> : null}
      {issueError ? <p className="ps1251-error" role="alert">{issueError}</p> : null}
      <form className="ps1251-issue" onSubmit={(event) => { event.preventDefault(); void issue(); }}>
        <label>
          Name
          <input
            data-testid="share-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label>
          Password
          <input
            data-testid="share-password"
            type="password"
            title={TIP}
            value={password}
            autoComplete="new-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <label>
          Expires
          <input
            data-testid="share-expiry"
            type="datetime-local"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
          />
        </label>
        <button type="button" data-testid="issue-pass" disabled={stoppedNew} onClick={() => void issue()}>
          Issue pass
        </button>
      </form>
      {props.admin ? (
        <button type="button" className="ps1251-stop-new" data-testid="stop-new" onClick={() => void stopNew()}>
          Stop new passes
        </button>
      ) : null}
    </section>
  );
}

function actorBody(props: PreviewShareProps) {
  return { project_id: props.projectId ?? "", actor: props.actor ?? "" };
}

async function postJson(url: string, body: unknown) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return response.json();
}

async function postShare(props: PreviewShareProps, draft: ShareDraft) {
  const body = await postJson("/v1/preview-shares", {
    project_id: props.projectId ?? "",
    app_key: props.appKey ?? "",
    name: draft.name,
    password: draft.password,
    expires_at: draft.expiresAt,
    actor: props.actor ?? "",
  });
  if (body?.detail) return { error: String(body.detail) };
  return fromApi(body);
}

async function postOpen(props: PreviewShareProps, slug: string, password: string): Promise<OpenResult> {
  const response = await fetch(`/v1/preview-shares/${encodeURIComponent(slug)}/open`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project_id: props.projectId ?? "", password }),
  });
  if (response.ok) return { ok: true };
  const body = await response.json().catch(() => ({}));
  const reason = String(body?.detail || "password");
  if (reason === "expired" || reason === "stopped" || reason === "password") {
    return { ok: false, reason };
  }
  return { ok: false, reason: "password" };
}

const CSS = `
.ps1251 {
  --tide: #14343a;
  --foam: #e7f3f0;
  --brass: #c9852a;
  --ink: #1a2422;
  --refuse: #8e2e2e;
  --chalk: #f4fbfa;
  background: var(--tide);
  color: var(--chalk);
  font-family: Manrope, "Avenir Next", "Segoe UI", sans-serif;
  padding: 28px 22px 36px;
}
.ps1251-kicker {
  display: block;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-size: 0.72rem;
  color: var(--brass);
}
.ps1251 h1 {
  font-family: Sora, "Avenir Next", sans-serif;
  font-weight: 640;
  letter-spacing: -0.04em;
  font-size: clamp(1.8rem, 4vw, 2.6rem);
  margin: 4px 0 0;
  line-height: 1.05;
}
.ps1251-lead {
  max-width: 34rem;
  margin: 14px 0 0;
  line-height: 1.5;
  color: #d5e6e2;
}
.ps1251-passes {
  list-style: none;
  margin: 18px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.ps1251-pass {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px 16px;
  background: var(--foam);
  color: var(--ink);
  border-radius: 16px;
  border-left: 10px dashed var(--brass);
  padding: 16px 16px 14px 18px;
}
.ps1251-pass strong {
  font-family: Sora, "Avenir Next", sans-serif;
  font-size: 1.25rem;
  letter-spacing: -0.03em;
}
.ps1251-pass code {
  display: block;
  margin-top: 6px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.86rem;
}
.ps1251-app {
  display: block;
  margin-top: 4px;
  font-size: 0.8rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.ps1251-stamp {
  width: 86px;
  height: 86px;
  border: 3px solid var(--brass);
  color: var(--brass);
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-family: Sora, "Avenir Next", sans-serif;
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  transform: rotate(-8deg);
  justify-self: end;
}
.ps1251-pass[data-state="expired"] .ps1251-stamp,
.ps1251-pass[data-state="stopped"] .ps1251-stamp {
  border-color: var(--refuse);
  color: var(--refuse);
}
.ps1251-open, .ps1251-issue {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
}
.ps1251-open { grid-column: 1 / -1; }
.ps1251 label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.ps1251-pass label { color: var(--ink); }
.ps1251 input {
  background: transparent;
  border: none;
  border-bottom: 1px solid currentColor;
  color: inherit;
  font-family: Manrope, "Avenir Next", sans-serif;
  font-size: 1rem;
  letter-spacing: 0;
  text-transform: none;
  padding: 6px 0;
  min-width: 12rem;
}
.ps1251 button {
  background: var(--brass);
  color: #1a1408;
  border: none;
  border-radius: 999px;
  padding: 10px 16px;
  font-family: Sora, "Avenir Next", sans-serif;
  font-weight: 600;
  cursor: pointer;
}
.ps1251 button:disabled { opacity: 0.45; cursor: not-allowed; }
.ps1251 button:focus-visible, .ps1251 input:focus-visible {
  outline: 2px solid var(--brass);
  outline-offset: 3px;
}
.ps1251-issue {
  margin-top: 22px;
  padding-top: 16px;
  border-top: 1px dashed rgba(244, 251, 250, 0.35);
}
.ps1251-stop-new {
  margin-top: 14px;
  background: transparent;
  color: var(--chalk);
  box-shadow: inset 0 0 0 1px var(--brass);
}
.ps1251-error { margin: 12px 0 0; color: #f0c7b8; }
@media (max-width: 640px) {
  .ps1251 { padding: 20px 14px 28px; }
  .ps1251-pass { grid-template-columns: 1fr; }
  .ps1251-stamp { justify-self: start; }
}
@media (prefers-reduced-motion: reduce) {
  .ps1251-stamp { transform: none; }
}
`;
