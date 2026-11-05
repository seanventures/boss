"use client";

/**
 * CLAIM THIS BOSS (IDENTITY.md §4.11, §4.12, mockup e "First run"): the first Owner, on whichever
 * path this install is on —
 *
 *   setup_token      the link from the install output (`#t=`), valid an hour;
 *   legacy_password  the shared console password proves you are the operator (D25);
 *   network_bridge   being on this trusted network proves it (D27). The claim binds BOSS to
 *                    the origin shown. Also (BOSSAPP-813) any unclaimed install opened from a
 *                    browser whose REAL address is private (`claim.network: "private_network"`):
 *                    the first visitor claims on the site itself, like Gitea or n8n.
 *
 * From a public address there is no form: the setup link screen says, in words nobody can read as
 * an address to type, that `boss identity setup-link` is a terminal command for the server.
 *
 * PASSWORD FIRST (BOSSAPP-812): step 1 is name, email and a PASSWORD — the way in from any
 * browser. Step 2 "Protect your account": a passkey on this device (recommended; it is also the
 * one-tap sign-in) or an authenticator app. After a passkey, an authenticator is offered too,
 * because a passkey lives in one browser profile. Then recovery codes, shown once. Owners must
 * use MFA, so the second factor is not skippable. A passkey is never the only way in.
 */
import { passwordMin, type AuthOptionsWithPolicy } from "../../../lib/identity/types-account";
import React, { useEffect, useState } from "react";
import { DeviceMobile, Eye, EyeSlash, Fingerprint, LockSimple, ShieldCheck, Warning } from "@phosphor-icons/react";

import { idPost } from "../../../lib/identity/api";
import type { AuthOptions } from "../../../lib/identity/types";
import { codesOf, type RecoveryCodes } from "../../../lib/identity/types-auth";
import { claimOrigin, claimPathFor, claimProof } from "../../../lib/identity/claim-path";
import { passkeyBlockWords, passkeyPlace } from "../../../lib/identity/passkey-support";
import { webauthnAvailable } from "../../../lib/identity/webauthn";
import { AuthStage, Live, Logo, linkToken } from "../auth-parts";
import { primeCsrf, registerPasskey, sayAuth } from "../auth-flow";
import { CopyCommand } from "../copy-command";
import { HOST_COMMANDS, overSsh } from "../../../lib/identity/host-commands";
import { RecoveryCodesView } from "../recovery-codes";
import { TotpStep } from "../enrol/totp-step";

type Claim = NonNullable<AuthOptions["claim"]>;
type Stage = "form" | "protect" | "passkey" | "also-totp" | "totp" | "codes";

const SHOW_ON_SERVER = HOST_COMMANDS.password;
const SHOW_OVER_SSH = overSsh(SHOW_ON_SERVER);
const SETUP_LINK = HOST_COMMANDS.setupLink;

/** THE NEXT STEP AFTER THE CLAIM is setup (repository, model, first build), not the Identity
 *  overview: a new Owner landed on posture warnings with no pointer to what comes next. */
export const AFTER_CLAIM = "/?area=setup";

const SEAT_WORDS: Record<string, string> = {
  ceo: "ceo. Approvals and history will show your name",
  cto: "cto. Builds and reviews will show your name",
  cao: "cao. Approvals will show your name",
  vp_agents: "vp_agents. Agent history will show your name",
  release_manager: "release_manager. Releases will show your name",
};

function minutesLeft(iso: string | null, now: number): number | null {
  if (!iso) return null;
  return Math.max(0, Math.round((Date.parse(iso) - now) / 60_000));
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
  const minPw = passwordMin(options as AuthOptionsWithPolicy);
  const [stage, setStage] = useState<Stage>("form");
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [seat, setSeat] = useState(claim.seats[0] ?? "");
  const [legacy, setLegacy] = useState("");
  const [tried, setTried] = useState(false);
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  /** The recovery codes issued with the FIRST factor, held while a second one is added. */
  const [issuedCodes, setIssuedCodes] = useState<string[]>([]);
  const [originOk, setOriginOk] = useState(false);
  const [passkeys, setPasskeys] = useState(true);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mins, setMins] = useState<number | null>(null);
  const [pageOrigin, setPageOrigin] = useState<string | null>(null);
  const [noPasskey, setNoPasskey] = useState<string | null>(null);

  useEffect(() => {
    setPasskeys(webauthnAvailable());
    setToken(linkToken("setup"));
    setPageOrigin(window.location.origin);
    // An address no browser can hold a passkey for (an IP, plain http): the authenticator app is
    // the way in, said in one plain line — never a prompt the browser will refuse.
    setNoPasskey(passkeyBlockWords(options.passkey_reason, passkeyPlace()));
    setMins(minutesLeft(claim.expires_at, Date.now()));
  }, [claim.expires_at, options.passkey_reason]);

  // A setup link in the fragment is the proof whenever the API accepts one (lib/identity/claim-path).
  const path = claimPathFor(claim, token);
  const origin = claimOrigin(claim, pageOrigin);
  const network = path === "network_bridge";
  // The network BRIDGE (BOSS_CONSOLE_AUTH=network) keeps its passkey-first step; a private-network
  // claim on the site offers both factors.
  const bridgeOnly = network && claim.network !== "private_network";
  const fallback = !passkeys || Boolean(noPasskey); // no passkey here: the authenticator app is the factor
  const proofMissing = (path === "setup_token" && !token) || (path === "legacy_password" && !legacy)
    || (network && (!originOk || !origin));
  // The password is ALWAYS required (BOSSAPP-812): the way in from any browser.
  const ready = name.trim() && email.includes("@") && password.length >= minPw && !proofMissing;
  // Why Claim cannot go yet, in one line — a silent disabled button left people guessing.
  const missing = path === "setup_token" && !token ? "Open the setup link first"
    : !name.trim() ? "Add your name"
    : !email.includes("@") ? "Add your email"
    : password.length < minPw ? `Your password needs ${minPw - password.length} more characters`
    : path === "legacy_password" && !legacy ? "Add the shared console password"
    : network && (!originOk || !origin) ? "Confirm the address"
    : "";
  const pwShort = password.length > 0 && password.length < minPw;

  /** The codes issued with the first factor; only when there were none, ask for a set. */
  const showCodes = async (issued: string[] = []) => {
    setCodes(issued.length ? issued : codesOf(await idPost<RecoveryCodes>("/v1/identity/me/recovery-codes", {})));
    setStage("codes");
  };

  /** Step 2 with a passkey: the ceremony starts only on this click. Then offer an authenticator too. */
  const withPasskey = async () => {
    setError(null);
    setBusy(true);
    try {
      setStage("passkey");
      setIssuedCodes(await registerPasskey());
      setStage("also-totp");
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
      const proof = claimProof(path, { token, legacy, origin });
      await idPost("/v1/auth/setup/claim", { ...proof, name: name.trim(), email: email.trim(), seat: seat || null, password });
      setLegacy("");
      setPassword("");
      await primeCsrf();
      setBusy(false);
      // Step 2: where no passkey is possible the authenticator app is the factor; else choose.
      setStage(fallback ? "totp" : "protect");
    } catch (e) {
      const said = sayAuth(e);
      setError(path === "legacy_password" && said === "Wrong password"
        ? "That's not the shared console password."
        : said);
      setBusy(false);
    }
  };

  if (stage === "codes" && codes) {
    return (
      <AuthStage label="Recovery codes">
        <Logo host={options.host} />
        <h1>Save your recovery codes</h1>
        <p className="idn-s" data-copy="prose">If you lose your second factor, one of these gets you back in. You're this BOSS's first Owner.</p>
        <RecoveryCodesView codes={codes} host={options.host} continueLabel="Continue setup" onContinue={() => { setCodes(null); window.location.assign(AFTER_CLAIM); }} />
      </AuthStage>
    );
  }

  if (stage === "totp") {
    return (
      <AuthStage label="Authenticator">
        <Logo host={options.host} />
        <h1>Add an authenticator</h1>
        <p className="idn-s" data-copy="prose">{fallback ? `${(noPasskey ?? "Passkeys aren't available here.").replace(/\.$/, "")}. ` : ""}Scan the code with an authenticator app, then enter the 6 digits it shows.</p>
        {issuedCodes.length ? null : <div className="idn-note" data-copy="prose"><Warning size={15} aria-hidden /> Posture will show &ldquo;No passkey on the only Owner&rdquo; until you add one.</div>}
        <TotpStep onDone={(issued) => { showCodes(issuedCodes.length ? issuedCodes : issued).catch((e) => setError(sayAuth(e))); }} />
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
      </AuthStage>
    );
  }

  if (stage === "protect") {
    return (
      <AuthStage label="Protect your account">
        <Logo host={options.host} />
        <h1>Protect your account</h1>
        <p className="idn-s" data-copy="prose">Owners need a second factor. Your password still signs you in from any browser.</p>
        <button type="button" className="idn-big p" onClick={withPasskey} disabled={busy} data-testid="claim-factor-passkey">
          <Fingerprint size={18} weight="bold" aria-hidden /> Passkey on this device
        </button>
        <p className="idn-fine" data-copy="prose">Recommended. It is also your one-tap sign-in on this device.</p>
        {!bridgeOnly ? (
          <button type="button" className="idn-big s" onClick={() => { setError(null); setStage("totp"); }} data-testid="claim-factor-totp">
            <DeviceMobile size={18} aria-hidden /> Authenticator app (code)
          </button>
        ) : null}
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
      </AuthStage>
    );
  }

  if (stage === "also-totp") {
    return (
      <AuthStage label="Add an authenticator too">
        <Logo host={options.host} />
        <h1>Add an authenticator too?</h1>
        <p className="idn-s" data-copy="prose">Your passkey lives in this browser. With an authenticator app, your password and a code sign you in from any other browser.</p>
        <button type="button" className="idn-big p" onClick={() => { setError(null); setStage("totp"); }} data-testid="claim-also-totp">
          <DeviceMobile size={18} aria-hidden /> Add an authenticator
        </button>
        <button type="button" className="idn-big s" disabled={busy} data-testid="claim-skip-totp"
          onClick={() => { showCodes(issuedCodes).catch((e) => setError(sayAuth(e))); }}>Skip for now</button>
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        <p className="idn-fine" data-copy="prose">You can add one later in My account › Security.</p>
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
        <div className="idn-wait" role="status">
          {error ? <span className="idn-err">{error}</span> : <><span className="idn-blink" aria-hidden /> Waiting for your device…</>}
        </div>
        {error ? (
          <div className="idn-alt">
            <button type="button" onClick={withPasskey} disabled={busy}><Fingerprint size={16} aria-hidden /> Try again</button>
            {!bridgeOnly ? <button type="button" onClick={() => { setError(null); setStage("totp"); }}><ShieldCheck size={16} aria-hidden /> Use an authenticator app</button> : null}
          </div>
        ) : null}
        <p className="idn-fine" data-copy="prose">Your passkey never leaves your device. BOSS stores only its public key.</p>
      </AuthStage>
    );
  }

  // No setup link in the address: one thing to do — get the link. No form to fill in first.
  // The link lives in the fragment, read after mount: show nothing until it has been read.
  if (pageOrigin === null && path === "setup_token") return <AuthStage label="Claim this BOSS"><Logo host={options.host} /><h1>Claim this BOSS</h1></AuthStage>;
  if (path === "setup_token" && !token) {
    return (
      <AuthStage label="Claim this BOSS">
        <Logo host={options.host} />
        <h1>Claim this BOSS from its server</h1>
        <div data-testid="claim-no-token">
          <p className="idn-s" data-copy="prose">You&apos;re not on the server&apos;s own network, so claiming needs a one-hour link from the server.</p>
          <p className="idn-s" data-copy="prose" data-testid="claim-terminal-step">Run this <b>in a terminal on the server where BOSS is installed</b> (over SSH). It&apos;s a command, not a web address.</p>
          <CopyCommand command={SETUP_LINK} />
          <p className="idn-s" data-copy="prose">It prints a link. Open that link in this browser to claim.</p>
          {claim.link_over_api
            ? <p className="idn-fine" data-copy="prose">No shell there? Ask whoever holds this install&apos;s admin key to issue the link.</p>
            : null}
          <p className="idn-fine" data-copy="prose">From the server&apos;s own network (its LAN or tailnet), this page is the claim form itself.</p>
        </div>
      </AuthStage>
    );
  }

  return (
    <AuthStage label="Claim this BOSS" wide>
      <Logo host={options.host} right={path === "setup_token" && mins !== null ? `setup link · expires in ${mins} min` : bridgeOnly ? "network bridge" : network ? "private network" : options.host} />
      <h1>Claim this BOSS</h1>
      <p className="idn-s" data-copy="prose">
        {path === "legacy_password"
          ? "You'll be its first Owner. The shared console password works once, to prove it's you. From now on everyone signs in as themselves."
          : network
            ? "You'll be its first Owner. Being on this network proves you run it. From now on everyone signs in as themselves."
            : "You'll be its first Owner. The setup link proves you installed it. From now on everyone signs in as themselves."}
      </p>
      <form onSubmit={(e) => { e.preventDefault(); setTried(true); if (ready) submit(); }} noValidate>

        {network ? (
          <div className="idn-f" data-testid="claim-origin">
            <span className="idn-flabel">This BOSS will be bound to</span>
            <div className="idn-origin"><ShieldCheck size={16} aria-hidden /><code data-copy="data">{origin ?? ""}</code></div>
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

        {path === "legacy_password" ? (
          <div className="idn-f">
            <label htmlFor="claim-legacy">Shared console password</label>
            <input id="claim-legacy" className="idn-i" type="password" autoComplete="current-password" required value={legacy}
              onChange={(e) => setLegacy(e.target.value)} data-testid="claim-legacy-password" />
          </div>
        ) : null}
        <div className="idn-f">
          <label htmlFor="claim-pw">Choose a password</label>
          <div className="idn-ig">
            <LockSimple size={18} className="idn-ig-ico" aria-hidden />
            <input id="claim-pw" className="idn-i" type={showPassword ? "text" : "password"} autoComplete="new-password" minLength={minPw} required value={password}
              onChange={(e) => setPassword(e.target.value)} aria-describedby="claim-pw-hint" />
            <button type="button" className="idn-eye" aria-label={showPassword ? "Hide password" : "Show password"} aria-pressed={showPassword}
              onClick={() => setShowPassword(!showPassword)} data-testid="claim-pw-eye">
              {showPassword ? <EyeSlash size={18} aria-hidden /> : <Eye size={18} aria-hidden />}
            </button>
          </div>
          <span id="claim-pw-hint" className={pwShort ? "idn-err idn-hint" : "idn-dim idn-hint"} data-testid="claim-pw-hint">{password.length && password.length < minPw ? `${minPw - password.length} more characters needed` : `${minPw} characters or more`}</span>
        </div>

        {noPasskey ? <div className="idn-note" data-copy="prose" data-testid="no-passkey-here">{noPasskey}</div> : null}
        {error ? <div className="idn-err" role="alert">{error}</div> : null}
        {!error && tried && missing ? <div className="idn-err" role="alert" data-testid="claim-missing">{missing}</div> : null}
        <button type="submit" className="idn-big p" disabled={busy} aria-disabled={!ready || busy || undefined} data-testid="claim-submit">
          <ShieldCheck size={18} aria-hidden />
          {busy ? "Claiming…" : fallback ? "Claim and add an authenticator" : "Claim and continue"}
        </button>
      </form>

      {path === "legacy_password" ? (
        <details className="idn-help" data-testid="console-password-hint">
          <summary>Where&apos;s my password?</summary>
          <p className="idn-s" data-copy="prose">The installer kept it on the server. Print it from the install directory:</p>
          <CopyCommand command={SHOW_ON_SERVER} />
          <p className="idn-s" data-copy="prose">Or from your own machine, over SSH:</p>
          <CopyCommand command={SHOW_OVER_SSH} />
        </details>
      ) : null}
      <p className="idn-fine" data-copy="prose">Owners must use MFA: next you&apos;ll add a passkey or an authenticator app.</p>
      <Live text={busy ? "Claiming" : ""} />
    </AuthStage>
  );
}
