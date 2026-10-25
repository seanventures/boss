"use client";

/**
 * Add model › Subscription › Sign in (BOSSAPP-769): the ONE supported way to connect Codex,
 * Claude Code or Grok. The provider's own login runs on the BOSS server; the person opens the URL
 * it printed, enters the one-time code it printed, and approves. Nothing is pasted, dropped or
 * imported — no token, no auth.json, no Replace.
 *
 * Start → the session (URL + code) → poll every 2 s → (a code box when the CLI asks for one) →
 * Signed in, and the drawer's Connect proves it. Cancel — or closing the drawer — kills the login.
 */
import React, { useEffect, useRef, useState } from "react";
import { ArrowSquareOut, CircleNotch, Copy } from "@phosphor-icons/react";
import type { SignInSession, SignInState } from "@boss/api-client";

import { GatedButton } from "../../components/gated";
import { SetupFailure, fetchSignIn, setupWrite, signInLive } from "../../lib/setup-client";
import type { SetupRefusal, Tone } from "../../lib/setup-types";
import { toast, useBoard } from "./parts";
import { Dot, Out, Tip } from "./pill";

/** The subscriptions whose login BOSS runs itself. Any other subscription is detected only. */
export const SIGN_IN_PROVIDERS: ReadonlySet<string> = new Set(["codex", "claude_code", "grok"]);

/** How often a waiting sign-in is re-read. Tests set it to 0. */
export const signInTiming = { pollMs: 2000 };

/** A sign-in that stopped, in the console's words. The headline and fix are the server's. */
const STOPPED_TONE: Partial<Record<SignInState, Tone>> = {
  failed: "red", expired: "red", cancelled: "grey",
};

function refusalOf(error: unknown): SetupRefusal {
  if (error instanceof SetupFailure && error.refusal) return error.refusal;
  return { state: "failed", headline: "Sign-in did not start",
           fix: String((error as Error)?.message ?? error) };
}

/** A headline pill with its fix beside it — the fix is the next step, so it stays readable. */
function Stopped({ tone, headline, fix, state }: {
  tone: Tone; headline: string; fix: string; state: string;
}) {
  return (
    <span className="su-signin-stopped" data-testid="su-signin-stopped" data-state={state}>
      <span className={`su-pill su-tone-${tone}`} role="alert" title={fix} data-tip={fix}>
        <Dot tone={tone} />{headline}
      </span>
      {fix ? <span className="su-faint su-signin-fix" title={fix} data-testid="su-signin-fix">{fix}</span> : null}
    </span>
  );
}

export function SubscriptionSignIn({ provider, label, autoStart = false, fresh = false, onSession }: {
  provider: string; label: string;
  /** Opened from a row's Sign in: start at once (one press, one effect). */
  autoStart?: boolean;
  /** Replace the CLI's current session: BOSS signs it out and starts a new login even if the
   *  CLI says it is signed in (a refused login — "Login expired" — or "Sign in again"). */
  fresh?: boolean;
  onSession: (session: SignInSession | null) => void;
}) {
  const { projectId } = useBoard();
  const [session, setSession] = useState<SignInSession | null>(null);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState("");
  const [sending, setSending] = useState(false);
  const alive = useRef(true);
  const latest = useRef<SignInSession | null>(null);
  latest.current = session;
  const tell = useRef(onSession);
  tell.current = onSession;

  useEffect(() => { tell.current(session); }, [session]);

  const begin = async () => {
    setBusy(true);
    setRefusal(null);
    setSession(null);
    try {
      const started = await setupWrite<SignInSession>(projectId, "sign_in_start",
                                                      fresh ? { provider, fresh: true } : { provider },
                                                      { label: "Sign in" });
      if (alive.current) setSession(started);
    } catch (error) {
      if (alive.current) setRefusal(refusalOf(error));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  // CLOSING THE DRAWER CANCELS: a login left running would hold the provider's one session.
  useEffect(() => {
    alive.current = true;
    if (autoStart) void begin();
    return () => {
      alive.current = false;
      const open = latest.current;
      if (signInLive(open)) {
        void setupWrite(projectId, "sign_in_cancel", { sign_in_id: open!.sign_in_id },
                        { label: "Cancel", background: true }).catch(() => undefined);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // POLL WHILE IT WAITS: a new object every answer re-arms the timer.
  useEffect(() => {
    if (!signInLive(session)) return;
    const id = session!.sign_in_id;
    const timer = window.setTimeout(async () => {
      try {
        const next = await fetchSignIn(projectId, id);
        if (!alive.current || latest.current?.sign_in_id !== id) return;
        setSession({ ...next });
        if (next.state === "signed_in") toast(`${label} signed in · Connect to check it`);
      } catch (error) {
        if (!alive.current) return;
        const said = refusalOf(error);
        setSession((s) => s && { ...s, state: "failed", headline: said.headline, fix: said.fix });
      }
    }, Math.max(signInTiming.pollMs, 0));
    return () => window.clearTimeout(timer);
  }, [session, projectId, label]);

  const cancel = async () => {
    const open = session;
    if (!open) return;
    try {
      setSession(await setupWrite<SignInSession>(projectId, "sign_in_cancel",
                                                 { sign_in_id: open.sign_in_id },
                                                 { label: "Cancel", background: true }));
    } catch (error) {
      const said = refusalOf(error);
      setSession({ ...open, state: "cancelled", headline: said.headline, fix: said.fix });
    }
  };

  const submit = async () => {
    const open = session;
    const typed = code.trim();
    setCode("");                                   // the provider's code is never kept here
    if (!open || !typed) return;
    setSending(true);
    try {
      setSession(await setupWrite<SignInSession>(projectId, "sign_in_code",
                                                 { sign_in_id: open.sign_in_id, code: typed },
                                                 { label: "Submit" }));
    } catch (error) {
      const said = refusalOf(error);
      setRefusal(said);
    } finally {
      setSending(false);
    }
  };

  const start = (word: string) => (
    <GatedButton permission="model_provider:manage" type="button" className="su-btn"
                 disabled={busy} data-testid="su-signin-start" onClick={() => void begin()}
                 title={`Runs ${label}'s own sign-in on the BOSS server.`}
                 data-tip={`Runs ${label}'s own sign-in on the BOSS server.`}>
      {busy ? <CircleNotch className="su-spin" size={12} aria-hidden /> : null}
      {busy ? "Starting…" : word}
    </GatedButton>
  );

  // Starting: the press is in flight, or the CLI has not printed its URL yet.
  if (busy || session?.state === "starting") {
    return (
      <div className="su-signin" data-testid="su-signin" data-state="starting">
        <span className="su-line su-faint" data-testid="su-signin-starting">
          <CircleNotch className="su-spin" size={12} aria-hidden />Starting sign-in…
        </span>
        {session ? (
          <button type="button" className="su-link" data-testid="su-signin-cancel"
                  title="Stops the sign-in on the BOSS server." onClick={() => void cancel()}>Cancel</button>
        ) : null}
      </div>
    );
  }

  if (session?.state === "waiting") {
    const copy = () => {
      try { void navigator.clipboard?.writeText(session.user_code ?? "").catch(() => undefined); toast("Code copied"); }
      catch { /* no clipboard */ }
    };
    return (
      <div className="su-signin" data-testid="su-signin" data-state="waiting">
        {session.needs_code_input ? (
          // Paste-back (Claude): the provider's page shows a code the person brings back here.
          // Three numbered steps, so nobody has to guess where "paste it into Claude Code" goes.
          <ol className="su-signin-steps" data-testid="su-signin-steps">
            <li>
              <span className="su-signin-step-n" aria-hidden>1</span>
              <div className="su-signin-step">
                <span className="su-signin-step-title">Approve on {label}&apos;s sign-in page</span>
                {session.verification_url ? (
                  <a className="su-btn su-btn-primary su-signin-open" href={session.verification_url}
                     target="_blank" rel="noopener noreferrer" data-testid="su-signin-open"
                     title={`Opens ${label}'s sign-in page in a new tab.`}>
                    Open sign-in page<ArrowSquareOut size={14} aria-hidden />
                  </a>
                ) : null}
              </div>
            </li>
            <li>
              <span className="su-signin-step-n" aria-hidden>2</span>
              <div className="su-signin-step">
                <span className="su-signin-step-title">Copy the code the page shows</span>
                <span className="su-signin-step-hint" data-testid="su-signin-code-help">
                  The page says to paste it into {label}: paste it here instead.
                </span>
              </div>
            </li>
            <li>
              <span className="su-signin-step-n" aria-hidden>3</span>
              <form className="su-signin-step" data-testid="su-signin-code-form"
                    onSubmit={(e) => { e.preventDefault(); void submit(); }}>
                <label className="su-signin-step-title" htmlFor="su-signin-code-input">
                  Paste the code and submit
                </label>
                <div className="su-signin-paste">
                  <input id="su-signin-code-input" type="text" autoComplete="one-time-code"
                         spellCheck={false} value={code} aria-label="Code"
                         placeholder="Paste the code here" data-testid="su-signin-code-input"
                         onChange={(e) => setCode(e.target.value)} />
                  <GatedButton permission="model_provider:manage" type="submit"
                               className="su-btn su-btn-primary"
                               disabled={sending || !code.trim()} data-testid="su-signin-code-submit"
                               title="Hands the code to the provider's sign-in. Never stored."
                               data-tip="Hands the code to the provider's sign-in. Never stored.">
                    {sending ? "Sending…" : "Submit"}
                  </GatedButton>
                </div>
              </form>
            </li>
          </ol>
        ) : (
          <div className="su-line">
            {session.verification_url ? (
              <Out href={session.verification_url} testid="su-signin-open"
                   tip={`Opens ${label}'s sign-in page in a new tab.`}>Open sign-in page</Out>
            ) : null}
            {session.user_code ? (
              <button type="button" className="su-copy su-signin-code" data-testid="su-signin-code"
                      title="Enter this code on the sign-in page. Click to copy."
                      data-tip="Enter this code on the sign-in page. Click to copy." onClick={copy}>
                <code>{session.user_code}</code><Copy size={14} aria-label="copy" />
              </button>
            ) : null}
          </div>
        )}
        {refusal ? <Stopped tone="red" headline={refusal.headline} fix={refusal.fix} state={refusal.state} /> : null}
        <div className="su-line">
          <Tip tip={session.headline || "Approve the sign-in in your browser."} className="su-line su-faint"
               testid="su-signin-waiting">
            <CircleNotch className="su-spin" size={12} aria-hidden />
            {session.needs_code_input ? "Waiting for the code…" : "Waiting for you to approve…"}
          </Tip>
          <button type="button" className="su-link" data-testid="su-signin-cancel"
                  title="Stops the sign-in on the BOSS server." onClick={() => void cancel()}>Cancel</button>
        </div>
      </div>
    );
  }

  if (session?.state === "signed_in") {
    return (
      <div className="su-signin su-line" data-testid="su-signin" data-state="signed_in">
        <span className="su-pill su-tone-green" role="status" data-testid="su-signin-done"
              title={session.headline} data-tip={session.headline}>
          <Dot tone="green" />Signed in
        </span>
      </div>
    );
  }

  // Stopped (failed, expired, cancelled) or refused at start: the headline, its fix, Try again.
  const stoppedTone = session ? STOPPED_TONE[session.state] ?? "grey" : "red";
  return (
    <div className="su-signin" data-testid="su-signin" data-state={session?.state ?? (refusal ? "refused" : "idle")}>
      {session ? <Stopped tone={stoppedTone} headline={session.headline} fix={session.fix} state={session.state} /> : null}
      {!session && refusal ? <Stopped tone="red" headline={refusal.headline} fix={refusal.fix} state={refusal.state} /> : null}
      <div className="su-line">{start(session || refusal ? "Try again" : "Sign in")}</div>
    </div>
  );
}
