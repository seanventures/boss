"use client";

/**
 * ① GitHub, the one "Sign in with GitHub" (device flow, engine identity/github_device.py).
 *
 * Press it, type the code at github.com/login/device, approve: BOSS polls until GitHub says yes.
 * No callback URL, so it works on localhost, a tailnet or a private host. The same sign-in is your
 * "as me on GitHub" connection (no separate trip to My account › Agents). What agents may do with
 * it is still your grant: nothing here widens one.
 *
 * Unconfigured (no published BOSS App on this install) it says so plainly and points at Advanced.
 */
import React, { forwardRef, useCallback, useEffect, useRef, useState } from "react";
import { CircleNotch, Copy, GithubLogo } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { IdentityError } from "../../lib/identity/api";
import {
  afterPoll, fetchSignin, pollDevice, SIGNIN_EVENT, startDevice, STOP_WORDS,
  type Phase, type SigninStatus,
} from "../../lib/identity/github-signin";
import { toast } from "./parts";
import { Dot, Out, Tip } from "./pill";
import { SetupRow } from "./setup-row";

const ROW_TIP = "One sign-in: lists your repos and lets agents act as you, within your grants.";
const ADVANCED_TIP = "No published BOSS app here. Use a token in Pick repos, or Advanced › GitHub App.";

/** The signed-in person's GitHub sign-in status, shared by every row; re-read on SIGNIN_EVENT. */
export function useSignin(): { status: SigninStatus | null; loaded: boolean; reload: () => void } {
  const [status, setStatus] = useState<SigninStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const reload = useCallback(() => {
    fetchSignin().then((s) => { setStatus(s); setLoaded(true); })
      .catch(() => { setStatus(null); setLoaded(true); });
  }, []);
  useEffect(() => {
    reload();
    window.addEventListener(SIGNIN_EVENT, reload);
    return () => window.removeEventListener(SIGNIN_EVENT, reload);
  }, [reload]);
  return { status, loaded, reload };
}

function said(error: unknown): string {
  if (error instanceof IdentityError) {
    const code = String(error.body.error ?? error.code);
    if (code === "device_flow_disabled") return "Device flow is off on the BOSS app";
    if (code === "signin_not_configured") return "Not set up on this install";
    if (code === "people_only") return "Sign in to BOSS as a person";
  }
  return "GitHub did not answer";
}

/** The code, where to type it, and the wait. Polls at GitHub's interval; slow_down lengthens it. */
export function DeviceSignin({ onDone, label = "Sign in with GitHub" }: {
  onDone: (login: string | null) => void; label?: string;
}) {
  const [phase, setPhase] = useState<Phase>({ at: "idle" });
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const done = useRef(onDone);
  done.current = onDone;

  const begin = async () => {
    setBusy(true);
    try {
      const s = await startDevice();
      setPhase({ at: "code", code: s.user_code, uri: s.verification_uri, handle: s.handle,
                 interval: s.interval, expiresAt: Date.now() + s.expires_in * 1000 });
    } catch (error) {
      toast(said(error), "error");
    } finally { setBusy(false); }
  };

  const handle = phase.at === "code" ? phase.handle : null;
  const interval = phase.at === "code" ? phase.interval : 0;
  useEffect(() => {
    if (!handle) return;
    const timer = window.setTimeout(async () => {
      try {
        const answer = await pollDevice(handle);
        if (!alive.current) return;
        setPhase((p) => {
          const next = afterPoll(p, answer);
          // A new object even when unchanged keeps the poll going (the effect re-arms on it).
          return next.at === "code" ? { ...next } : next;
        });
        if (answer.state === "connected") {
          toast(answer.login ? `Signed in as @${answer.login}` : "Signed in with GitHub");
          try { window.dispatchEvent(new Event(SIGNIN_EVENT)); } catch { /* no window */ }
          done.current(answer.login);
        }
      } catch {
        if (alive.current) setPhase({ at: "stopped", why: "failed", error: "unreachable" });
      }
    }, Math.max(interval, 0) * 1000);
    return () => window.clearTimeout(timer);
  }, [handle, interval, phase]);

  if (phase.at === "code") {
    const copy = () => { try { void navigator.clipboard?.writeText(phase.code); toast("Code copied"); } catch { /* none */ } };
    return (
      <div className="su-stack" data-testid="gh-signin-waiting">
        <div className="su-line">
          <Tip tip="Type this code on GitHub's device page." className="su-mono su-code" testid="gh-signin-code">
            {phase.code}
          </Tip>
          <button type="button" className="su-btn su-btn-sm" onClick={copy} data-testid="gh-signin-copy"
                  title="Copy the code" data-tip="Copy the code"><Copy size={12} aria-hidden />Copy</button>
          <Out href={phase.uri} testid="gh-signin-open" tip="Opens github.com/login/device. Paste the code, then Authorize.">
            Open GitHub
          </Out>
        </div>
        <span className="su-line su-faint"><CircleNotch className="su-spin" size={12} aria-hidden />Waiting for GitHub…</span>
      </div>
    );
  }
  return (
    <div className="su-line">
      {phase.at === "stopped" ? (
        <span className={`su-pill su-tone-red`} data-testid="gh-signin-stopped" data-why={phase.why}
              title={STOP_WORDS[phase.why].tip} data-tip={STOP_WORDS[phase.why].tip}>
          <Dot tone="red" />{STOP_WORDS[phase.why].label}
        </span>
      ) : null}
      <GatedButton permission="connection:use_own" type="button" className="su-btn su-btn-primary"
                   onClick={() => void begin()} disabled={busy} data-testid="gh-signin-start"
                   title="Shows a code to type on GitHub. Works on any host."
                   data-tip="Shows a code to type on GitHub. Works on any host.">
        {busy ? <CircleNotch className="su-spin" size={12} aria-hidden /> : <GithubLogo size={14} aria-hidden />}
        {phase.at === "stopped" ? "Try again" : label}
      </GatedButton>
    </div>
  );
}

export const GitHubSigninRow = forwardRef<HTMLElement, {
  step: number | null; mode: string; focused: boolean; status: SigninStatus | null;
  onAdvanced: () => void;
}>(function GitHubSigninRow({ step, mode, focused, status, onAdvanced }, ref) {
  const [again, setAgain] = useState(false);
  if (!status) return null;
  const me = status.me && status.me.status === "active" ? status.me : null;
  let body: React.ReactNode;
  if (!status.configured) {
    body = (
      <div className="su-line" data-testid="gh-signin-unconfigured">
        <span className="su-pill su-tone-grey" title={ADVANCED_TIP} data-tip={ADVANCED_TIP}>
          <Dot tone="grey" />This install uses the advanced path
        </span>
        <button type="button" className="su-link" onClick={onAdvanced} data-testid="gh-signin-advanced">
          Advanced
        </button>
      </div>
    );
  } else if (me && !again) {
    body = (
      <div className="su-line" data-testid="gh-signin-done">
        <Dot tone="green" />
        <span>Signed in as <span className="su-mono" data-user-text>@{me.login ?? "you"}</span></span>
        <Tip tip="Agents act as you only where a grant says so." className="su-chip">As me: linked</Tip>
        <button type="button" className="su-link" onClick={() => setAgain(true)} data-testid="gh-signin-switch">
          Switch account
        </button>
      </div>
    );
  } else {
    body = (
      <div className="su-stack">
        {status.me?.status === "needs_reauth" ? (
          <span className="su-line" data-testid="gh-signin-reauth"><Dot tone="amber" />GitHub asks you to sign in again</span>
        ) : null}
        <DeviceSignin onDone={() => setAgain(false)} />
      </div>
    );
  }
  return (
    <SetupRow ref={ref} row="github" label="GitHub" step={step} mode={mode} done={Boolean(me) && !again}
              focused={focused} tip={ROW_TIP}>
      {body}
    </SetupRow>
  );
});
