// The forms for the React/Vite scaffold: forgot password, choose a new password (the page the
// reset link opens), invite someone, and accept an invite (the page the invite link opens).
// Adapt the markup to the app's own components and design tokens; keep the behaviour: the reset
// request always says the same thing (whether an account exists is never shown), the token comes
// from the link's ?token=, and the invite form sends the app's CSRF token.
import { useState } from "react";

function tokenFromUrl() {
  return new URLSearchParams(window.location.search).get("token") || "";
}

async function postJson(path, body, headers = {}) {
  const r = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body),
  });
  const out = r.status === 204 ? {} : await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body: out };
}

export function ForgotPasswordForm() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState("idle");
  async function submit(e) {
    e.preventDefault();
    setState("sending");
    const r = await postJson("/api/email/password-reset", { email });
    setState(r.status === 400 ? "bad" : "done");
  }
  if (state === "done") {
    return <p role="status">If an account uses {email}, a link to reset its password is on its way.</p>;
  }
  return (
    <form onSubmit={submit} aria-label="Forgot password">
      <label>Email <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
      {state === "bad" && <p role="alert">That is not an email address.</p>}
      <button type="submit" disabled={state === "sending"}>Send reset link</button>
    </form>
  );
}

export function ResetPasswordForm({ onDone = () => window.location.assign("/") }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  async function submit(e) {
    e.preventDefault();
    const r = await postJson("/api/email/password-reset/confirm", { token: tokenFromUrl(), password });
    if (r.ok) return onDone();
    setError(r.body.error === "weak_password" ? "Use at least 8 characters."
      : "This link has expired or was already used. Ask for a new one.");
  }
  return (
    <form onSubmit={submit} aria-label="Choose a new password">
      <label>New password <input type="password" autoComplete="new-password" minLength={8} required
                                 value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      {error && <p role="alert">{error}</p>}
      <button type="submit">Save password</button>
    </form>
  );
}

// csrfToken: the app's own (login-oauth's /api/me returns it as csrf_token).
export function InviteForm({ csrfToken, roles = ["member"] }) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState(roles[0]);
  const [result, setResult] = useState(null);
  async function submit(e) {
    e.preventDefault();
    const r = await postJson("/api/email/invites", { email, role }, { "X-CSRF-Token": csrfToken || "" });
    setResult(r.ok ? { ok: true, text: `Invite sent to ${email}.` }
      : { ok: false, text: r.status === 403 ? "You cannot invite people here."
        : r.body.error === "email_not_sent" ? "The invite was not sent: email is not working. Try again later."
          : "Check the address and try again." });
  }
  return (
    <form onSubmit={submit} aria-label="Invite someone">
      <label>Email <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
      {roles.length > 1 && (
        <label>Role <select value={role} onChange={(e) => setRole(e.target.value)}>
          {roles.map((r) => <option key={r} value={r}>{r}</option>)}
        </select></label>
      )}
      <button type="submit">Send invite</button>
      {result && <p role={result.ok ? "status" : "alert"}>{result.text}</p>}
    </form>
  );
}

export function AcceptInvite({ onAccepted = () => window.location.assign("/") }) {
  const [state, setState] = useState("idle");
  async function accept() {
    setState("working");
    const r = await postJson("/api/email/invites/accept", { token: tokenFromUrl() });
    if (r.ok) return onAccepted(r.body);
    setState("invalid");
  }
  if (state === "invalid") return <p role="alert">This invite has expired or was already used.</p>;
  return <button type="button" onClick={accept} disabled={state === "working"}>Accept the invite</button>;
}
