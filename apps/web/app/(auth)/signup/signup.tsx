"use client";

/**
 * SELF SIGN-UP (IDENTITY.md §4.15, D32): off by default. When an Owner turns it on, a name and
 * email get a verification link; following it creates the person, with no access role until an
 * admin binds one. The answer is the same words for every address, allowed domain or not.
 */
import React, { useEffect, useRef, useState } from "react";
import { EnvelopeSimple, ShieldCheck } from "@phosphor-icons/react";

import { idPost } from "../../../lib/identity/api";
import { AuthStage, Live, Logo, fragmentToken } from "../auth-parts";
import { sayAuth } from "../auth-flow";

export function SignUp({ host, org, enabled }: { host: string; org: string; enabled: boolean }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!enabled) {
    return (
      <AuthStage label="Create account">
        <Logo host={host} />
        <h1>Ask an admin to invite you</h1>
        <p className="idn-s" data-copy="prose" data-testid="signup-off">People join {org} by invitation. An admin can send you one.</p>
        <a className="idn-big s" href="/login">Back to sign in</a>
      </AuthStage>
    );
  }
  return (
    <AuthStage label="Create account">
      <Logo host={host} />
      {sent ? (
        <div className="idn-sent" data-testid="signup-sent">
          <div className="idn-env" aria-hidden><EnvelopeSimple size={22} /></div>
          <h1>Check your email</h1>
          <p className="idn-s" data-copy="prose">If that address can join, we sent a link to finish. It works once, for 10 minutes, in this browser.</p>
          <a className="idn-big s" href="/login">Back to sign in</a>
        </div>
      ) : (
        <>
          <h1>Create your account</h1>
          <p className="idn-s" data-copy="prose">You&apos;ll start with no access. An admin gives you an access role.</p>
          <form onSubmit={(e) => {
            e.preventDefault();
            setBusy(true); setError(null);
            idPost("/v1/auth/signup", { name: name.trim(), email: email.trim() }).then(() => setSent(true))
              .catch((err) => setError(sayAuth(err))).finally(() => setBusy(false));
          }}>
            <div className="idn-f">
              <label htmlFor="su-name">Your name</label>
              <input id="su-name" className="idn-i" autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="idn-f">
              <label htmlFor="su-email">Work email</label>
              <input id="su-email" className="idn-i" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            {error ? <div className="idn-err" role="alert">{error}</div> : null}
            <button type="submit" className="idn-big p" disabled={busy || !name.trim() || !email.includes("@")}>{busy ? "Sending…" : "Email me a link"}</button>
          </form>
          <div className="idn-foot">Have an account? <a href="/login">Sign in</a></div>
        </>
      )}
      <Live text={sent ? "Sent" : ""} />
    </AuthStage>
  );
}

/** `/signup/verify#t=…`: following the emailed link creates the person. */
export function SignUpVerify({ host }: { host: string }) {
  const [state, setState] = useState<"checking" | "ok" | "failed">("checking");
  // S1 signs the new person in on verify ({next: "enrol" | "done"}): go on to add a second
  // factor rather than asking them to sign in again.
  const [next, setNext] = useState<"enrol" | "done" | null>(null);
  const once = useRef(false);
  useEffect(() => {
    if (once.current) return;
    once.current = true;
    const token = fragmentToken();
    try { window.history.replaceState(null, "", window.location.pathname); } catch { /* fine */ }
    if (!token) { setState("failed"); return; }
    idPost<{ next?: string } | null>("/v1/auth/signup/verify", { token })
      .then((r) => { setNext(r?.next === "enrol" || r?.next === "done" ? r.next : null); setState("ok"); })
      .catch(() => setState("failed"));
  }, []);
  return (
    <AuthStage label="Verify email">
      <Logo host={host} />
      {state === "checking" ? (
        <>
          <h1 className="idn-center">Checking your link</h1>
          <div className="idn-wait" role="status"><span className="idn-blink" aria-hidden /> One moment…</div>
        </>
      ) : state === "ok" ? (
        <div className="idn-sent">
          <div className="idn-env" aria-hidden><ShieldCheck size={22} /></div>
          <h1>Your account is ready</h1>
          {next ? (
            <>
              <p className="idn-s" data-copy="prose">{next === "enrol" ? "Add a passkey next. An admin will give you access." : "You're signed in. An admin will give you access."}</p>
              <a className="idn-big p" href={next === "enrol" ? "/enrol?next=%2F" : "/"}>{next === "enrol" ? "Add a passkey" : "Continue"}</a>
            </>
          ) : (
            <>
              <p className="idn-s" data-copy="prose">Sign in and add a passkey. An admin will give you access.</p>
              <a className="idn-big p" href="/login">Sign in</a>
            </>
          )}
        </div>
      ) : (
        <>
          <h1>This link didn&apos;t work</h1>
          <p className="idn-s" data-copy="prose">It has expired, was used already, or was opened in another browser.</p>
          <a className="idn-big s" href="/signup">Start again</a>
        </>
      )}
    </AuthStage>
  );
}
