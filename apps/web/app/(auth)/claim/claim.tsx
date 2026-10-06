"use client";

/**
 * CLAIM THIS BOSS (IDENTITY.md §4.11, §4.12, mockup e "First run"): the first Owner, on whichever
 * path this install is on —
 *
 *   setup_token      the link from the install output (`#t=`), valid an hour;
 *   legacy_password  the shared console password proves you are the operator (D25);
 *   network_bridge   being on this network proves it (shared-server, D27). The claim binds BOSS to
 *                    the origin shown, and a passkey is required.
 *
 * Then a passkey on this device (an authenticator app only where the browser has no passkeys),
 * and recovery codes, shown once. Owners must use MFA, so nothing is skippable.
 */
import React, { useEffect, useState } from "react";
import { Fingerprint, ShieldCheck, Warning } from "@phosphor-icons/react";

import { idPost } from "../../../lib/identity/api";
import type { AuthOptions } from "../../../lib/identity/types";
import type { RecoveryCodes } from "../../../lib/identity/types-auth";
import { webauthnAvailable } from "../../../lib/identity/webauthn";
import { AuthStage, Live, Logo, fragmentToken } from "../auth-parts";
import { primeCsrf, registerPasskey, sayAuth } from "../auth-flow";
import { CopyCommand } from "../copy-command";
import { RecoveryCodesView } from "../recovery-codes";
import { TotpStep } from "../enrol/totp-step";

type Claim = NonNullable<AuthOptions["claim"]>;
type Stage = "form" | "passkey" | "totp" | "codes";

const SHOW_ON_SERVER = "python3 scripts/install/console_password.py show";
const SHOW_OVER_SSH = `ssh -t user@your-server 'cd ~/boss && ${SHOW_ON_SERVER}'`;
const SETUP_LINK = "docker exec boss-api boss identity setup-link";

const SEAT_WORDS: Record<string, string> = {
  ceo: "ceo · approvals and history will show your name",
  cto: "cto · builds and reviews will show your name",
  cao: "cao · approvals will show your name",
  vp_agents: "vp_agents · agent history will show your name",
  release_manager: "release_manager · releases will show your name",
};

function minutesLeft(iso: string | null): number | null {
  if (!iso) return null;
  return Math.max(0, Math.round((Date.parse(iso) - Date.now()) / 60_000));
}

export function ClaimFlow({ options }: { options: AuthOptions }) {
  if (!options.claim) {
    return (
      <AuthStage label="Claim this BOSS">
        <Logo host={options.host} />
        <h1>Already claimed</h1>
        <p className="idn-s" data-copy="prose">This BOSS has an Owner. Sign in, or ask an Owner to invite you.</p>
        <a className="idn-big p" href="/login">Sign in</a>
      </AuthStage>
    );
  }
  return <ClaimForm options={options} claim={options.claim} />;
}

function ClaimForm({ options, claim }: { options: AuthOptions; claim: Claim }) {
  const [stage, setStage] = useState<Stage>("form");
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [seat, setSeat] = useState(claim.seats[0] ?? "");
  const [legacy, setLegacy] = useState("");
  const [password, setPassword] = useState("");
  const [originOk, setOriginOk] = useState(false);
  const [passkeys, setPasskeys] = useState(true);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mins, setMins] = useState<number | null>(null);

  useEffect(() => {
    setPasskeys(webauthnAvailable());
    setToken(fragmentToken());
    setMins(minutesLeft(claim.expires_at));
  }, [claim.expires_at]);

  const network = claim.path === "network_bridge";
  const fallback = !passkeys; // no WebAuthn here: password + authenticator app instead
  const proofMissing = (claim.path === "setup_token" && !token) || (claim.path === "legacy_password" && !legacy)
    || (network && !originOk) || (fallback && password.length < 12);
  const ready = name.trim() && email.includes("@") && !proofMissing;

  const showCodes = async () => {
    const r = await idPost<RecoveryCodes>("/v1/identity/me/recovery-codes", {});
    setCodes(r.codes);
    setStage("codes");
  };

  const secondFactor = async () => {
    setError(null);
    setBusy(true);
    try {
      if (fallback) { setStage("totp"); return; }
      setStage("passkey");
      await registerPasskey();
      await showCodes();
    } catch (e) {
      setError(sayAuth(e));
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const proof = claim.path === "setup_token" ? { setup_token: token }
        : claim.path === "legacy_password" ? { legacy_password: legacy }
        : { origin: claim.origin };
      await idPost("/v1/auth/setup/claim", { ...proof, name: name.trim(), email: email.trim(), seat: seat || null, ...(fallback ? { password } : {}) });
      setLegacy("");
      await primeCsrf();
      setBusy(false);
      await secondFactor();
    } catch (e) {
      setError(sayAuth(e));
      setBusy(false);
    }
  };

  if (stage === "codes" && codes) {
    return (
      <AuthStage label="Recovery codes">
        <Logo host={options.host} />
        <h1>Save your recovery codes</h1>
        <p className="idn-s" data-copy="prose">If you lose every passkey, one of these gets you back in. You're this BOSS's first Owner.</p>
        <RecoveryCodesView codes={codes} host={options.host} continueLabel="Open Identity" onContinue={() => { setCodes(null); window.location.assign("/identity"); }} />
      </AuthStage>
    );
  }

  if (stage === "totp") {
    return (
      <AuthStage label="Authenticator">
        <Logo host={options.host} />
        <h1>Add an authenticator</h1>
        <p className="idn-s" data-copy="prose">This browser has no passkeys. Scan the code with an authenticator app, then enter the 6 digits it shows.</p>
        <div className="idn-note" data-copy="prose"><Warning size={15} aria-hidden /> Posture will show &ldquo;No passkey on the only Owner&rdquo; until you add one.</div>
        <TotpStep onDone={() => { showCodes().catch((e) => setError(sayAuth(e))); }} />
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
      </AuthStage>
    );
  }

  if (stage === "passkey") {
    return (
      <AuthStage label="Create a passkey">
        <Logo host={options.host} />
        <h1 className="idn-center">Create your passkey</h1>
        <p className="idn-s idn-center" data-copy="prose">Your device is asking. Confirm to make a passkey for <b>{options.host}</b>.</p>
        <div className="idn-pk" aria-hidden>
          <span className="idn-wave w1" /><span className="idn-wave w2" /><span className="idn-wave w3" />
          <span className="idn-core"><Fingerprint size={40} weight="bold" /></span>
        </div>
        <div className="idn-state" role="status">
          {error ? <span className="idn-err">{error}</span> : <><span className="idn-blink" aria-hidden /> Waiting for your device…</>}
        </div>
        {error ? (
          <div className="idn-alt">
            <button type="button" onClick={secondFactor} disabled={busy}><Fingerprint size={16} aria-hidden /> Try again</button>
            {!network ? <button type="button" onClick={() => { setError(null); setStage("totp"); }}><ShieldCheck size={16} aria-hidden /> Use an authenticator app</button> : null}
          </div>
        ) : null}
        <p className="idn-fine" data-copy="prose">Your passkey never leaves your device. BOSS stores only its public key.</p>
      </AuthStage>
    );
  }

  return (
    <AuthStage label="Claim this BOSS" wide>
      <Logo host={options.host} right={claim.path === "setup_token" && mins !== null ? `setup link · expires in ${mins} min` : network ? "network bridge" : options.host} />
      <h1>Claim this BOSS</h1>
      <p className="idn-s" data-copy="prose">
        {claim.path === "legacy_password"
          ? "You'll be its first Owner. The shared console password works once, to prove it's you. From now on everyone signs in as themselves."
          : network
            ? "You'll be its first Owner. Being on this network proves you run it. From now on everyone signs in as themselves."
            : "You'll be its first Owner. The setup link proves you installed it. From now on everyone signs in as themselves."}
      </p>
      <form onSubmit={(e) => { e.preventDefault(); if (ready) submit(); }} noValidate>
        {claim.path === "setup_token" && !token ? (
          <div className="idn-note bad" role="alert" data-testid="claim-no-token">
            <Warning size={15} aria-hidden />
            <span data-copy="prose">Open the setup link from the install output. To print a new one:</span>
          </div>
        ) : null}
        {claim.path === "setup_token" && !token ? <CopyCommand command={SETUP_LINK} /> : null}

        {network ? (
          <div className="idn-f" data-testid="claim-origin">
            <span className="idn-flabel">This BOSS will be bound to</span>
            <div className="idn-origin"><ShieldCheck size={16} aria-hidden /><code data-copy="data">{claim.origin}</code></div>
            <label className="idn-check">
              <input type="checkbox" checked={originOk} onChange={(e) => setOriginOk(e.target.checked)} data-testid="claim-origin-confirm" />
              That&apos;s the address I use
            </label>
          </div>
        ) : null}

        <div className="idn-f">
          <label htmlFor="claim-name">Your name</label>
          <input id="claim-name" className="idn-i" autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="idn-f">
          <label htmlFor="claim-email">Email</label>
          <input id="claim-email" className="idn-i" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        {claim.seats.length ? (
          <div className="idn-f">
            <label htmlFor="claim-seat">Which seat were you?</label>
            <select id="claim-seat" className="idn-i" value={seat} onChange={(e) => setSeat(e.target.value)}>
              {claim.seats.map((s) => <option key={s} value={s}>{SEAT_WORDS[s] ?? s}</option>)}
              <option value="">None · start fresh</option>
            </select>
          </div>
        ) : null}

        {claim.path === "legacy_password" ? (
          <div className="idn-f">
            <label htmlFor="claim-legacy">Shared console password</label>
            <input id="claim-legacy" className="idn-i" type="password" autoComplete="current-password" required value={legacy}
              onChange={(e) => setLegacy(e.target.value)} data-testid="claim-legacy-password" />
          </div>
        ) : null}
        {fallback ? (
          <div className="idn-f">
            <label htmlFor="claim-pw">Choose a password</label>
            <input id="claim-pw" className="idn-i" type="password" autoComplete="new-password" minLength={12} value={password}
              onChange={(e) => setPassword(e.target.value)} aria-describedby="claim-pw-hint" />
            <span id="claim-pw-hint" className="idn-dim idn-hint">12 characters or more</span>
          </div>
        ) : null}

        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        <button type="submit" className="idn-big p" disabled={!ready || busy} aria-disabled={!ready || busy || undefined} data-testid="claim-submit">
          {fallback ? <ShieldCheck size={18} aria-hidden /> : <Fingerprint size={18} weight="bold" aria-hidden />}
          {busy ? "Claiming…" : fallback ? "Claim and add an authenticator" : "Create a passkey and continue"}
        </button>
      </form>

      {claim.path === "legacy_password" ? (
        <details className="idn-help" data-testid="console-password-hint">
          <summary>Where&apos;s my password?</summary>
          <p className="idn-s" data-copy="prose">The installer kept it on the server. Print it from the install directory:</p>
          <CopyCommand command={SHOW_ON_SERVER} />
          <p className="idn-s" data-copy="prose">Or from your own machine, over SSH:</p>
          <CopyCommand command={SHOW_OVER_SSH} />
        </details>
      ) : null}
      <p className="idn-fine" data-copy="prose">Owners must use MFA. You&apos;ll get recovery codes next.</p>
      <Live text={busy ? "Claiming" : ""} />
    </AuthStage>
  );
}
