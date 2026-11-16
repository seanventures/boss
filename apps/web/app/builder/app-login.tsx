"use client";

/**
 * Login, register, and password reset as the built app would show them.
 * The preview edits this copy only. It is not the BOSS console sign-in.
 */
import React, { useState } from "react";

export type BuiltAppPageCopy = {
  headline: string;
  lede: string;
  submit: string;
  emailLabel: string;
  secretLabel?: string;
  nameLabel?: string;
};

export type BuiltAppAuthCopy = {
  login: BuiltAppPageCopy;
  register: BuiltAppPageCopy;
  "password-reset": BuiltAppPageCopy;
};

type Kind = keyof BuiltAppAuthCopy;

const PATHS: Record<Kind, string> = {
  login: "/login",
  register: "/register",
  "password-reset": "/password-reset",
};

const STUB: Record<Kind, string> = {
  login: "Login",
  register: "Register",
  "password-reset": "Reset",
};

const TABS: { kind: Kind; label: string }[] = [
  { kind: "login", label: "Sign in" },
  { kind: "register", label: "Register" },
  { kind: "password-reset", label: "Reset" },
];

export const BUILT_APP_AUTH_DEFAULTS: BuiltAppAuthCopy = {
  login: {
    headline: "Sign in",
    lede: "Use the email and password this app already knows.",
    submit: "Sign in",
    emailLabel: "Email",
    secretLabel: "Password",
  },
  register: {
    headline: "Create an account",
    lede: "This account belongs to the app, not to BOSS.",
    submit: "Create account",
    emailLabel: "Email",
    secretLabel: "Password",
    nameLabel: "Name",
  },
  "password-reset": {
    headline: "Reset password",
    lede: "Ask this app for a reset. It does not sign you in.",
    submit: "Send reset",
    emailLabel: "Email",
  },
};

const MAX = 120;

function clone(src: BuiltAppAuthCopy): BuiltAppAuthCopy {
  return {
    login: { ...src.login },
    register: { ...src.register },
    "password-reset": { ...src["password-reset"] },
  };
}

/**
 * Mineral paper, pine ink, one brass ticket stub. The stub is the door's
 * ticket; the pine bar above it is the preview, not the app.
 */
const CSS = `
.baa{--paper:#e4ebe6;--card:#f4f7f5;--ink:#1a2e26;--muted:#4e655b;--line:#c5d1c9;
--brass:#9a6430;--pine:#24362e;--slip:#f3e2cf;color:var(--ink);
font:16px/1.45 "Avenir Next","Trebuchet MS","Segoe UI",sans-serif}
.baa *{box-sizing:border-box}
.baa-chrome{display:flex;flex-wrap:wrap;align-items:center;gap:12px 16px;
background:var(--pine);color:var(--slip);padding:12px 16px}
.baa-kicker{margin:0;font:600 11px/1 "Avenir Next Condensed","Arial Narrow",sans-serif;
letter-spacing:.16em;text-transform:uppercase;color:#c9a27a}
.baa-app{font:700 16px/1 "Avenir Next Condensed","Arial Narrow",sans-serif;letter-spacing:.02em}
.baa-tabs{display:flex;gap:6px;margin-left:auto}
.baa-tab,.baa-edit{font:600 13px/1 "Avenir Next","Trebuchet MS",sans-serif;border:1px solid transparent;
background:transparent;color:var(--slip);padding:8px 10px;cursor:pointer}
.baa-tab[aria-selected="true"]{border-color:#9a6430;color:#fffaf4}
.baa-edit{border-color:#9a6430}
.baa-slip{display:grid;gap:10px;padding:14px 16px;background:#efe6d6;border-bottom:1px solid var(--line)}
.baa-slip label{display:grid;gap:4px;font-size:13px;font-weight:600}
.baa-slip input,.baa-slip textarea{width:100%;font:inherit;color:var(--ink);background:#fffaf4;
border:1px solid var(--line);padding:8px 10px}
.baa-slip textarea{min-height:64px;resize:vertical}
.baa-slip-actions{display:flex;gap:8px}
.baa-slip-actions button{font:600 14px/1 inherit;padding:10px 14px;cursor:pointer;border:1px solid var(--line);
background:transparent;color:var(--ink)}
.baa-slip-actions button[type="submit"]{background:var(--brass);border-color:var(--brass);color:#fffaf4}
.baa-err{margin:0;color:#8f2d2d;font-weight:600}
.baa-stage{display:grid;place-items:center;background:
radial-gradient(circle at 12px 12px, transparent 10px, var(--paper) 11px) 0 0/24px 24px,
var(--paper);padding:28px 16px}
.baa-card{display:flex;width:min(460px,100%);background:var(--card);border:1px solid var(--line);
box-shadow:8px 10px 0 #24362e14}
.baa-stub{display:flex;flex-direction:column;justify-content:space-between;align-items:center;gap:12px;
width:48px;padding:18px 0;background:var(--pine);color:var(--slip);
font:600 12px/1 "Avenir Next Condensed","Arial Narrow",sans-serif;letter-spacing:.18em;text-transform:uppercase}
.baa-stub i{flex:1;width:0;border-left:1px dashed var(--brass)}
.baa-main{flex:1;padding:28px 26px 22px}
.baa-brand{margin:0 0 14px;font:600 13px/1 "Avenir Next Condensed","Arial Narrow",sans-serif;
letter-spacing:.08em;text-transform:uppercase;color:var(--brass)}
.baa-main h1{margin:0 0 8px;font:700 34px/1.05 "Avenir Next Condensed","Arial Narrow",sans-serif;
letter-spacing:-.02em}
.baa-main p{margin:0 0 18px;color:var(--muted)}
.baa-main label{display:block;margin:0 0 6px;font-size:13px;font-weight:600}
.baa-main input{width:100%;margin:0 0 14px;padding:10px 12px;border:1px solid var(--line);
background:#fff;color:var(--ink);font:inherit}
.baa-main button[type="submit"]{width:100%;padding:12px 14px;border:0;background:var(--brass);
color:#fffaf4;font:600 15px/1 "Avenir Next","Trebuchet MS",sans-serif;cursor:pointer}
.baa-links{display:flex;justify-content:space-between;gap:12px;margin-top:14px;font-size:14px}
.baa-links a{color:var(--brass)}
.baa-note{margin:12px 0 0;color:var(--ink);font-weight:600}
.baa :focus-visible{outline:2px solid var(--brass);outline-offset:2px}
.baa [hidden]{display:none !important}
@media (max-width:480px){
.baa-card{flex-direction:column}
.baa-stub{flex-direction:row;width:auto;padding:10px 14px}
.baa-stub i{width:auto;height:0;border-left:0;border-top:1px dashed var(--brass)}
.baa-tabs{margin-left:0}
}
@media (prefers-reduced-motion:reduce){.baa *{transition:none}}
`;

export function BuiltAppAuthPreview({
  appName = "This app",
  pages,
  onChange,
}: {
  appName?: string;
  pages?: BuiltAppAuthCopy;
  onChange?: (next: BuiltAppAuthCopy) => void;
}) {
  const [local, setLocal] = useState<BuiltAppAuthCopy>(() => clone(BUILT_APP_AUTH_DEFAULTS));
  const current = pages ?? local;
  const [selected, setSelected] = useState<Kind>("login");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ headline: "", lede: "", submit: "" });
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const name = appName.trim() || "This app";

  function commit(next: BuiltAppAuthCopy) {
    if (!pages) setLocal(next);
    onChange?.(clone(next));
  }

  function openEdit() {
    const page = current[selected];
    setDraft({ headline: page.headline, lede: page.lede, submit: page.submit });
    setError(null);
    setEditing((open) => !open);
  }

  function save(event: React.FormEvent) {
    event.preventDefault();
    const headline = draft.headline.trim();
    const lede = draft.lede.trim();
    const submit = draft.submit.trim();
    if (!headline || !lede || !submit) {
      setError("Write something to save.");
      return;
    }
    if (headline.length > MAX || lede.length > MAX || submit.length > MAX) {
      setError("That line is too long.");
      return;
    }
    const next = clone(current);
    next[selected] = { ...next[selected], headline, lede, submit };
    commit(next);
    setEditing(false);
    setError(null);
  }

  function onPreviewSubmit(event: React.FormEvent) {
    event.preventDefault();
    setNote("Preview only. Nothing was sent.");
  }

  function pick(kind: Kind) {
    setSelected(kind);
    setEditing(false);
    setError(null);
  }

  return (
    <section className="baa" data-testid="built-app-auth" data-owner="built-app">
      <style>{CSS}</style>
      <header className="baa-chrome">
        <div>
          <p className="baa-kicker">Built app</p>
          <div className="baa-app">{name}</div>
        </div>
        <div className="baa-tabs" role="tablist" aria-label="App pages">
          {TABS.map((tab) => (
            <button
              key={tab.kind}
              type="button"
              className="baa-tab"
              role="tab"
              aria-selected={selected === tab.kind}
              data-testid={`built-app-tab-${tab.kind}`}
              onClick={() => pick(tab.kind)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="baa-edit"
          data-testid="built-app-edit"
          title="Edits this app only"
          aria-pressed={editing}
          onClick={openEdit}
        >
          Edit copy
        </button>
      </header>
      {editing ? (
        <form className="baa-slip" data-testid="built-app-edit-panel" onSubmit={save}>
          <label>
            Headline
            <input
              data-testid="built-app-edit-headline"
              value={draft.headline}
              maxLength={MAX}
              onChange={(e) => setDraft({ ...draft, headline: e.target.value })}
            />
          </label>
          <label>
            Line under it
            <textarea
              data-testid="built-app-edit-lede"
              value={draft.lede}
              maxLength={MAX}
              onChange={(e) => setDraft({ ...draft, lede: e.target.value })}
            />
          </label>
          <label>
            Button
            <input
              data-testid="built-app-edit-submit"
              value={draft.submit}
              maxLength={MAX}
              onChange={(e) => setDraft({ ...draft, submit: e.target.value })}
            />
          </label>
          {error ? <p className="baa-err" role="alert" data-testid="built-app-edit-error">{error}</p> : null}
          <div className="baa-slip-actions">
            <button type="submit" data-testid="built-app-edit-save">Save words</button>
            <button type="button" data-testid="built-app-edit-cancel" onClick={() => { setEditing(false); setError(null); }}>
              Cancel
            </button>
          </div>
        </form>
      ) : null}
      <div className="baa-stage">
        {(Object.keys(PATHS) as Kind[]).map((kind) => (
          <AppDoor
            key={kind}
            kind={kind}
            appName={name}
            copy={current[kind]}
            hidden={kind !== selected}
            note={kind === selected ? note : null}
            onSubmit={onPreviewSubmit}
            onNavigate={pick}
          />
        ))}
      </div>
    </section>
  );
}

function AppDoor({
  kind, appName, copy, hidden, note, onSubmit, onNavigate,
}: {
  kind: Kind;
  appName: string;
  copy: BuiltAppPageCopy;
  hidden: boolean;
  note: string | null;
  onSubmit: (event: React.FormEvent) => void;
  onNavigate: (kind: Kind) => void;
}) {
  const path = PATHS[kind];
  return (
    <article
      className="baa-card"
      data-testid={`built-app-page-${kind}`}
      data-owner="built-app"
      data-page={kind}
      data-path={path}
      hidden={hidden}
    >
      <div className="baa-stub" aria-hidden="true"><span>{STUB[kind]}</span><i /></div>
      <div className="baa-main">
        <div className="baa-brand">{appName}</div>
        <h1 data-testid="built-app-headline">{copy.headline}</h1>
        <p data-testid="built-app-lede">{copy.lede}</p>
        <form method="post" action={path} onSubmit={onSubmit}>
          {kind === "register" ? (
            <label>
              {copy.nameLabel}
              <input name="name" type="text" autoComplete="name" required />
            </label>
          ) : null}
          <label>
            {copy.emailLabel}
            <input name="email" type="email" autoComplete="username" required />
          </label>
          {kind === "password-reset" ? null : (
            <label>
              {copy.secretLabel}
              <input
                name="password"
                type="password"
                autoComplete={kind === "register" ? "new-password" : "current-password"}
                required
              />
            </label>
          )}
          <button type="submit" data-testid="built-app-submit">{copy.submit}</button>
          {note ? <p className="baa-note" role="status" data-testid="built-app-preview-note">{note}</p> : null}
        </form>
        <div className="baa-links">
          {kind === "login" ? (
            <>
              <a href={PATHS.register} onClick={(e) => { e.preventDefault(); onNavigate("register"); }}>Create an account</a>
              <a href={PATHS["password-reset"]} onClick={(e) => { e.preventDefault(); onNavigate("password-reset"); }}>Reset password</a>
            </>
          ) : null}
          {kind === "register" ? (
            <a href={PATHS.login} onClick={(e) => { e.preventDefault(); onNavigate("login"); }}>Sign in</a>
          ) : null}
          {kind === "password-reset" ? (
            <a href={PATHS.login} onClick={(e) => { e.preventDefault(); onNavigate("login"); }}>Back to sign in</a>
          ) : null}
        </div>
      </div>
    </article>
  );
}
