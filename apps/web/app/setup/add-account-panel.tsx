"use client";

/**
 * Setup › Add a GitHub account (BOSSAPP-808 screen 3): a side panel in three steps —
 * 1 How to connect · 2 Pick repos · 3 Check access.
 *
 * Every way in is one that already worked: "Sign in with GitHub" (device flow, github-signin-row),
 * the BOSS app installed on an organisation (the same sign-in lists its repos), a pasted
 * fine-grained token (PasteTokenForm). Picking repos runs the existing `repo_select` per repo,
 * which runs the three checks; step 3 shows what they said. The org's own GitHub App (manifest)
 * stays in Advanced settings, one link away.
 */
import React, { useEffect, useState } from "react";
import { CircleNotch } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { setupWrite } from "../../lib/setup-client";
import type { SetupRefusal, SetupView, TokenFound } from "../../lib/setup-types";
import { Drawer } from "./drawer";
import { DeviceSignin } from "./github-signin-row";
import { Consent, CONSENT_REFUSAL, toast, useBoard } from "./parts";
import { Out, Refusal, Tip } from "./pill";
import { createTokenUrl, InstallNeededLinks, refusalOf, type SigninFound } from "./repository-row";

export type How = "own" | "org" | "token";
type Step = 1 | 2 | 3;

const HOW: { key: How; title: string; line: string }[] = [
  { key: "own", title: "My own account", line: "Sign in with GitHub. One click, no tokens to copy." },
  { key: "org", title: "An organisation", line: "Install the BOSS app on the org. An org admin may need to approve." },
  { key: "token", title: "Paste an access token", line: "For locked-down orgs. We show exactly which permissions to tick." },
];
const STEPS = ["How to connect", "Pick repos", "Check access"] as const;
const CHECKS = [["read", "Read"], ["write", "Write"], ["pull_requests", "Open PRs"]] as const;

export function StepChips({ step }: { step: Step }) {
  return (
    <ol className="su-steps" data-testid="su-account-steps">
      {STEPS.map((label, i) => (
        <li key={label} className="su-step-chip" data-current={step === i + 1 || undefined}
            data-done={step > i + 1 || undefined}>
          <span aria-hidden="true">{i + 1}</span>{label}
        </li>
      ))}
    </ol>
  );
}

/** Step 2: a checkbox per repository the account can reach (already-connected ones left out). */
function PickRepos({ found, picked, setPicked }: {
  found: TokenFound; picked: string[]; setPicked: (next: string[]) => void;
}) {
  const { view } = useBoard();
  const bound = new Set(view.repository.repos.filter((r) => r.binding_id).map((r) => r.full_name));
  const [query, setQuery] = useState("");
  const choices = found.repos.filter((r) => !bound.has(r.full_name)
                                          && r.full_name.toLowerCase().includes(query.toLowerCase()));
  const toggle = (name: string) => setPicked(picked.includes(name) ? picked.filter((p) => p !== name)
                                                                    : [...picked, name]);
  return (
    <div className="su-stack" data-testid="su-account-pick">
      <div className="su-line">
        <span className="su-dot su-tone-green" aria-hidden="true" />
        <span>Repos for <span className="su-mono" data-user-text>@{found.account_login ?? "github"}</span></span>
      </div>
      {found.repos.length > 6 ? (
        <input type="search" className="su-input" placeholder="Search repos…" aria-label="Search repos"
               value={query} onChange={(e) => setQuery(e.target.value)} data-testid="su-account-search" />
      ) : null}
      <ul className="su-checklist" aria-label="Repositories">
        {choices.map((r) => (
          <li key={r.full_name}>
            <label className="su-check-row">
              <input type="checkbox" checked={picked.includes(r.full_name)} onChange={() => toggle(r.full_name)}
                     data-testid={`su-account-repo-${r.full_name}`} />
              <span className="su-mono" data-user-text>{r.full_name}</span>
              <span className="su-faint">{r.private ? "private" : "public"}</span>
            </label>
          </li>
        ))}
        {!choices.length ? <li className="su-faint" data-testid="su-account-no-repos">No new repos to add</li> : null}
      </ul>
    </div>
  );
}

/** Step 3: Read / Write / Open PRs for each picked repo, as the checks answered. */
function CheckAccess({ names }: { names: string[] }) {
  const { view } = useBoard();
  return (
    <ul className="su-stack su-checklist" data-testid="su-account-access">
      {names.map((name) => {
        const repo = view.repository.repos.find((r) => r.full_name === name);
        return (
          <li key={name} className="su-stack" data-testid={`su-account-access-${name}`}>
            <span className="su-mono su-strong" data-user-text>{name}</span>
            <span className="su-chips">
              {CHECKS.map(([key, label]) => {
                const ok = repo?.checks[key].ok;
                return (
                  <span key={key} className="su-chip" data-ok={String(ok ?? null)}>
                    {label} {ok === true ? "✓" : ok === false ? "✗" : "–"}
                  </span>
                );
              })}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function AddAccountPanel({ onClose, onAdvanced, initial = null }: {
  onClose: () => void;
  /** Open Advanced settings on the org's own GitHub App (manifest). */
  onAdvanced: () => void;
  initial?: How | null;
}) {
  const { projectId, view, apply, signin } = useBoard();
  const signinReady = Boolean(signin?.configured);
  const signedIn = signin?.me?.status === "active";
  // THE APP THAT WORKS HERE: the org's GitHub App when it is installed (repo_app lists its repos).
  const app = (view.github_accounts ?? []).find((a) => a.kind === "org_app") ?? null;
  // RECOMMENDED IS A PATH THAT WORKS ON THIS INSTALL, never a greyed-out one.
  const recommended: How = signinReady ? "own" : app ? "org" : "token";
  const [how, setHow] = useState<How>(initial ?? recommended);
  const [token, setToken] = useState("");
  const [consent, setConsent] = useState(false);
  const [step, setStep] = useState<Step>(1);
  const [signing, setSigning] = useState(false);
  const [found, setFound] = useState<(TokenFound & Partial<SigninFound>) | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<SetupRefusal | null>(null);

  const listSignin = async () => {
    setBusy(true);
    setRefusal(null);
    try {
      const answer = await setupWrite<SigninFound>(projectId, "repo_signin", {},
                                                   { label: "Pick repos", background: true });
      setFound(answer);
      setStep(2);
    } catch (error) { setRefusal(refusalOf(error)); } finally { setBusy(false); }
  };

  // Signed in just now (DeviceSignin): straight on to Pick repos.
  useEffect(() => {
    if (signing && signedIn) { setSigning(false); void listSignin(); }
  }, [signing, signedIn]); // eslint-disable-line react-hooks/exhaustive-deps

  const foundRepos = (f: TokenFound) => {
    setFound(f);
    setPicked(f.repos.length === 1 ? [f.repos[0].full_name] : []);
    setStep(2);
  };

  const sendToken = async () => {
    if (!token.trim()) { setRefusal({ state: "none", headline: "Paste a token first", fix: "" }); return; }
    if (view.secrets.needs_consent && !consent) { setRefusal(CONSENT_REFUSAL); return; }
    setBusy(true);
    try {
      foundRepos(await setupWrite<TokenFound>(projectId, "repo_token", {
        token: token.trim(), accept_database_fallback: view.secrets.needs_consent ? consent : undefined,
      }, { label: "Connect GitHub" }));
    } catch (error) { setRefusal(refusalOf(error)); } finally {
      setBusy(false);
      setToken("");                        // never held after it was sent
    }
  };

  const useApp = async () => {
    setBusy(true);
    try {
      foundRepos(await setupWrite<TokenFound>(projectId, "repo_app", {}, { label: "Use the GitHub App" }));
    } catch (error) { setRefusal(refusalOf(error)); } finally { setBusy(false); }
  };

  const next = async () => {
    setRefusal(null);
    if (step === 1) {
      if (how === "token") { await sendToken(); return; }
      if (how === "org") {
        if (app) { await useApp(); return; }
        if (signinReady && signedIn) { await listSignin(); return; }
        if (signinReady) { setSigning(true); return; }
        onAdvanced();
        return;
      }
      if (!signedIn) { setSigning(true); return; }
      await listSignin();
      return;
    }
    if (step === 2 && found) {
      if (!picked.length) { setRefusal({ state: "none", headline: "Pick at least one repo", fix: "" }); return; }
      setBusy(true);
      try {
        let latest: SetupView | null = null;
        const first = !view.repository.repos.some((r) => r.binding_id);
        for (const [i, name] of picked.entries()) {
          latest = await setupWrite<SetupView>(projectId, "repo_select", {
            connection_id: found.connection_id, full_name: name, make_default: first && i === 0,
          }, { label: "Connect" });
        }
        if (latest) apply(latest);
        setStep(3);
      } catch (error) { setRefusal(refusalOf(error)); } finally { setBusy(false); }
      return;
    }
    if (step === 3) {
      toast(`${picked.length} repo${picked.length === 1 ? "" : "s"} added`);
      onClose();
    }
  };

  let body: React.ReactNode;
  if (step === 1) {
    body = (
      <div className="su-stack">
        <div className="su-radio-cards" role="radiogroup" aria-label="How to connect">
          {HOW.map((h) => {
            const off = h.key === "own" && !signinReady;
            const why = "Sign in with GitHub is not set up on this install.";
            const line = off ? why
              : h.key === "org" && app ? `Use the BOSS GitHub App on @${app.login ?? app.name}.` : h.line;
            return (
              <label key={h.key} className="su-radio-card" data-checked={how === h.key || undefined}
                     data-disabled={off || undefined} data-testid={`su-how-${h.key}`}
                     title={off ? why : undefined} data-tip={off ? why : undefined}>
                <input type="radio" name="su-how" value={h.key} checked={how === h.key} disabled={off}
                       onChange={() => { setHow(h.key); setSigning(false); setRefusal(null); }} />
                <span className="su-stack su-tight">
                  <span className="su-strong">{h.title}{h.key === recommended
                    ? <span className="su-chip su-chip-good">Recommended</span> : null}</span>
                  <span className="su-faint">{line}</span>
                </span>
              </label>
            );
          })}
        </div>
        {how !== "token" && signing && !signedIn ? <DeviceSignin onDone={() => undefined} /> : null}
        {how === "own" && signedIn ? (
          <Tip tip="One GitHub sign-in per person. For another personal account, paste its token."
               className="su-faint" testid="su-how-signed-in">Signed in as @{signin?.me?.login ?? "you"}</Tip>
        ) : null}
        {how === "org" && !app ? (
          <div className="su-line">
            {signin?.install_url ? (
              <Out href={signin.install_url} testid="su-how-install"
                   tip="Pick the org on GitHub. An org admin may need to approve.">Install the BOSS app</Out>
            ) : null}
            <button type="button" className="su-link" onClick={onAdvanced} data-testid="su-how-own-app"
                    title="Org admin: create the org's own GitHub App." data-tip="Org admin: create the org's own GitHub App.">
              Use your own GitHub App
            </button>
          </div>
        ) : null}
        {how === "token" ? (
          <div className="su-stack" data-testid="su-how-token-form">
            <input type="password" autoComplete="off" spellCheck={false} className="su-input" value={token}
                   placeholder="Paste token" aria-label="Paste token" data-testid="su-account-token"
                   title="Stored in BOSS's secret store. Never shown again."
                   onChange={(e) => setToken(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter") void next(); }} />
            {view.secrets.needs_consent ? <Consent checked={consent} onChange={setConsent} /> : null}
            <div className="su-line">
              <Out href={createTokenUrl(typeof window !== "undefined" ? window.location.hostname : "this server")}
                   tip="Opens GitHub with Contents and Pull requests: Read and write ticked."
                   testid="su-account-create-token">Create token</Out>
            </div>
          </div>
        ) : null}
      </div>
    );
  } else if (step === 2 && found) {
    body = (
      <div className="su-stack">
        <PickRepos found={found} picked={picked} setPicked={setPicked} />
        <InstallNeededLinks needed={found.install_needed ?? []} onCheck={() => void listSignin()} busy={busy} />
      </div>
    );
  } else {
    body = <CheckAccess names={picked} />;
  }

  const continueLabel = step === 3 ? "Done"
    : step === 2 && picked.length ? `Add ${picked.length} repo${picked.length === 1 ? "" : "s"}` : "Continue";
  return (
    <Drawer title="Add a GitHub account" onClose={onClose} testid="su-add-account">
      <div className="su-stack" data-testid="su-add-account-body" data-step={step}>
        <StepChips step={step} />
        {body}
        {refusal ? <Refusal headline={refusal.headline} fix={refusal.fix} /> : null}
        <div className="su-line su-panel-foot">
          <button type="button" className="su-btn" onClick={onClose} data-testid="su-account-cancel">Cancel</button>
          {(
            <GatedButton permission={step === 2 ? "repository:register" : "github:connect"} type="button"
                         className="su-btn su-btn-primary" data-testid="su-account-continue"
                         disabled={busy || (step === 1 && how !== "token" && signing && !signedIn)} onClick={() => void next()}>
              {busy ? <CircleNotch className="su-spin" size={12} aria-hidden /> : null}{continueLabel}
            </GatedButton>
          )}
        </div>
      </div>
    </Drawer>
  );
}
