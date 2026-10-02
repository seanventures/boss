"use client";

/**
 * The first ten minutes.
 *
 * Every piece of this already had an endpoint. What did not exist was an answer to "I just
 * installed this, what now?", and the honest answer was: set two environment variables on the
 * API container, restart it, then call four endpoints in the right order with the right ids.
 * For open source that is where the evaluation ends — not because anything is missing, but
 * because nothing told you the order.
 *
 * Two steps, because two things make a working install: somewhere to write code, and something
 * to think with. Community stores model keys without a live provider request and labels them
 * unverified until a governed model run can prove them.
 */

import React, { useCallback, useEffect, useState } from "react";

import { RoleTable } from "./settings-knobs-ui";
import { HelpTip } from "./help-tip";

type Step = { id: string; title: string; done: boolean; detail: string;
              from_environment?: boolean; repository?: string | null };
type EnvCheck = {
  configured?: boolean; ok?: boolean; error?: string;
  /** "connected" when this is the repository the project is BOUND to, "environment" when it
   *  is the legacy env pair. Different facts, and the card has to say which. */
  source?: string;
  repository?: string; token_last4?: string; can_push?: boolean;
  default_branch?: string; private?: boolean;
  base_url?: string; model?: string; key_last4?: string;
  models_available?: number; model_listed?: boolean; note?: string;
  verification_state?: string;
};
/** A model source this project has already saved — newest first in `Status.model_saved`. */
type SavedModel = {
  source_id: string; provider: string; wizard_provider: string; auth_method: string;
  kind: string; connected: boolean; verified: boolean; saved_at: string | null;
};
type Status = {
  ready?: boolean; steps?: Step[]; repositories?: number;
  model_sources?: number; model_from_environment?: boolean;
  model_saved?: SavedModel[]; model_probe_available?: boolean;
  environment?: { github?: EnvCheck; model?: EnvCheck };
};
type Provider = {
  provider: string; label: string; kind: string; api: string;
  base_url?: string; example_model?: string; key_url?: string; note?: string;
};

/** What `/v1/setup/coding-agents` reports about one CLI on THIS machine. No credential
 *  appears in it, because a subscription does not have one to report. */
type CodingAgent = {
  name: string; binary: string; installed: boolean; version: string | null;
  authenticated: boolean | null; auth_detail: string | null; ready: boolean;
  next_step: string | null; install_hint?: string; signin_hint?: string; docs_url?: string;
  // What was RECORDED, as opposed to what detection sees now. These two answer different
  // questions and the wizard needs both: detection says whether a build could run this
  // minute, `connected` says whether anyone ever finished this step. Showing only the first
  // meant a refresh or a redeploy presented a finished connection as untouched.
  connected?: boolean; connected_at?: string | null; connected_status?: string | null;
  // How this subscription reaches a SERVER. "token" means the CLI reads a value the operator
  // can mint on their own machine and hand over — claude_code does; "file" means the CLI's
  // whole credential file has to travel, which is codex. "" means it can only be used where
  // its login already lives.
  credential_kind?: string; credential_env?: string | null; token_present?: boolean;
  token_hint?: string | null; portable?: boolean;
};

/**
 * Which dropdown entry the model picker should open on.
 *
 * "Can you pick up the one that's last saved?" — so: the most recently saved source that the
 * catalogue actually offers. Exported and pure because this is the whole of the decision, and
 * a rule buried in a component is a rule nothing can test.
 *
 * Returns "" when there is nothing saved, or when everything saved is something this build's
 * catalogue does not list — in which case the caller keeps its own default rather than
 * selecting an option that is not in the list.
 */
export function lastSavedProvider(saved: SavedModel[], providers: Provider[]): string {
  const offered = new Set(providers.map((p) => p.provider));
  // `saved` arrives newest-first from the API; find keeps that order.
  const hit = saved.find((m) => m.wizard_provider && offered.has(m.wizard_provider));
  return hit?.wizard_provider ?? "";
}

/** A decision the API is asking for, rather than an error it is reporting. */
type Decision = { error?: string; message?: string; backend?: string };

async function call<T>(body: Record<string, unknown>): Promise<
  { ok: boolean; data?: T; error?: string; decision?: Decision }> {
  try {
    const r = await fetch("/api/setup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return { ok: false, error: String(data?.error ?? `HTTP ${r.status}`),
               decision: data?.decision as Decision | undefined };
    }
    return { ok: true, data: data as T };
  } catch (e) {
    return { ok: false, error: String((e as Error)?.message ?? e) };
  }
}

export function SetupHome({ projectId, actor }: { projectId: string; actor: string }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);

  const reload = useCallback(async () => {
    try {
      const r = await fetch(`/api/setup?view=status&project_id=${encodeURIComponent(projectId)}`,
                            { cache: "no-store" });
      if (r.ok) setStatus(await r.json());
    } catch { /* the page still works; the steps just cannot say "done" */ }
  }, [projectId]);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    (async () => {
      try {
        const r = await fetch("/api/setup?view=providers", { cache: "no-store" });
        if (r.ok) setProviders(((await r.json()).providers ?? []) as Provider[]);
      } catch { /* the form falls back to a plain input */ }
    })();
  }, []);

  const steps = status?.steps ?? [];
  const unverifiedModel = (status?.model_saved ?? [])
    .some((item) => item.kind === "api_key" && !item.verified);
  return (
    <section className="panel-stack" aria-label="Repo and model">
      <article className="stack-card">
        <div className="section-head">
          <div>
            <span className="eyebrow">Repo and model</span>
            <div className="help-heading">
              <h2>Connect a repo and model</h2>
              <HelpTip label="Repo and model setup"
                text="BOSS needs a repository and a model. Community saves model keys as unverified until a governed run can test them." />
            </div>
          </div>
          {status ? (
            <span className={`cap-badge ${status.ready && !unverifiedModel ? "tone-green" : "tone-amber"}`}
                  data-testid="setup-verdict">
              {status.ready ? (unverifiedModel ? "Configured · model unverified" : "Ready")
                            : "Not finished"}
            </span>
          ) : null}
        </div>
        <ol className="setup-steps" data-testid="setup-steps">
          {steps.map((step) => (
            <li key={step.id} className={step.done ? "is-done" : ""}>
              <span className="setup-tick" aria-hidden>{step.done ? "✓" : "○"}</span>
              <span>
                <b>{step.title}</b>
                <span className="detail"> — {step.detail}</span>
                {/* AN INSTALL CONFIGURED THE OLD WAY IS CONFIGURED. Saying "not connected" to
                    somebody whose BOSS is already opening pull requests would be a falsehood
                    about their own system — and saying nothing about WHERE it came from leaves
                    them hunting for a setting the wizard never wrote. */}
                {step.from_environment ? (
                  <span className="detail" data-testid={`setup-env-${step.id}`}>
                    {" "}Already set up from this install&rsquo;s environment
                    {step.repository ? ` — ${step.repository}` : ""}. Connecting one here would
                    replace it.
                  </span>
                ) : null}
              </span>
            </li>
          ))}
        </ol>
      </article>

      <Configured env={status?.environment} />

      <GitHubStep projectId={projectId} actor={actor} onDone={reload} />
      <ModelStep projectId={projectId} actor={actor} providers={providers}
                 saved={status?.model_saved ?? []}
                 probeAvailable={status?.model_probe_available ?? false} onDone={reload} />
    </section>
  );
}

/**
 * What this install is ALREADY running on.
 *
 * The wizard answers "how do I set this up". It had no answer at all for the far more common
 * state — already configured, from the environment, by somebody who now wants to know whether
 * it still works. That operator was shown two empty forms, which reads as "nothing is connected"
 * about a BOSS that has been opening pull requests for weeks.
 *
 * Repository access is checked. In Community, environment model keys are reported as
 * unverified without contacting the provider.
 */
function Configured({ env }: { env?: { github?: EnvCheck; model?: EnvCheck } }) {
  const github = env?.github;
  const model = env?.model;
  if (!github && !model) return null;
  if (!github?.configured && !model?.configured) return null;

  return (
    <article className="stack-card" data-testid="setup-configured">
      <div className="section-head">
        <div>
          <span className="eyebrow">Already on this install</span>
          <h2>What BOSS is running on</h2>
          <p className="detail">
            {/* SAY WHICH ONE IT IS. This always read "set from this install's environment",
                and reported the env pair even when the project had a repository connected —
                so a working connection was invisible and a shipped default was shown failing
                beside it. */}
            {model?.verification_state === "unverified"
              ? <>This install has a model key, but Community has not sent it to the provider.
                  Repository access is shown separately below.</>
              : github?.source === "connected"
              ? <>The repository this project is connected to, and the model endpoint for this
                  install. Checked just now against GitHub — not read from a variable and
                  assumed.</>
              : <>Set from this install&rsquo;s environment. Checked just now against GitHub and
                  the model endpoint — not read from a variable and assumed.</>}
          </p>
        </div>
      </div>

      <dl className="setup-facts">
        {github?.configured ? (
          <div data-testid="setup-env-github-fact">
            <dt>
              Repository
              <span className={`cap-badge ${github.ok ? "tone-green" : "tone-amber"}`}>
                {github.ok ? "Can push" : "Check failed"}
              </span>
            </dt>
            <dd>
              <b>{github.repository}</b>
              {github.default_branch ? ` · default ${github.default_branch}` : ""}
              {github.private === true ? " · private" : github.private === false ? " · public" : ""}
              {/* THE LAST FOUR, never the token. Enough to tell two credentials apart when
                  somebody is working out which one an install is using; never enough to use. */}
              {github.token_last4 ? ` · token …${github.token_last4}` : ""}
              {github.source === "connected" ? " · connected here" : " · from the environment"}
              {github.error ? <span className="setup-bad"> — {github.error}</span> : null}
            </dd>
          </div>
        ) : null}

        {model?.configured ? (
          <div data-testid="setup-env-model-fact">
            <dt>
              Model
              <span className={`cap-badge ${model.ok ? "tone-green" : "tone-amber"}`}>
                {model.verification_state === "unverified" ? "Unverified"
                  : model.ok ? "Answering" : "Check failed"}
              </span>
            </dt>
            <dd>
              <b>{model.model || "not named"}</b>
              {model.base_url ? <> · <code>{model.base_url}</code></> : null}
              {model.key_last4 ? ` · key …${model.key_last4}` : ""}
              {typeof model.models_available === "number"
                ? ` · ${model.models_available} models offered` : ""}
              {model.note ? <span className="detail"> — {model.note}</span> : null}
              {model.error ? <span className="setup-bad"> — {model.error}</span> : null}
            </dd>
          </div>
        ) : null}
      </dl>
      <p className="detail">
        Connecting something below replaces what is here for this project.
      </p>
    </article>
  );
}

/**
 * Ask BOSS about the step you are looking at.
 *
 * "If you can provide me simple steps on how to get the token for git hub… or if it may change
 * then we should probably use an Agent in the setup."
 *
 * Both, with the division of labour that way round. The steps below are exact, free and
 * offline; an agent asked "where do I click" answers from training data with no browser open,
 * so it is the LESS reliable source for the one thing that has a known answer. Where it earns
 * its place is everything a list cannot anticipate — a token GitHub rejected, an organisation
 * that blocks fine-grained tokens, a 403 on push — so the question it opens with carries
 * whatever went wrong on screen.
 *
 * It opens the console's existing dock (`?agent=general&dock_ask=`) rather than becoming a
 * second chat surface with its own habits.
 */
function askInDock(question: string): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("agent", "general");
    url.searchParams.set("dock_ask", question);
    // ADDRESS IT, DO NOT INHERIT IT: dock_role survives in the URL until something clears it,
    // so a door that sets only the question opens whichever role was last addressed.
    url.searchParams.delete("dock_role");
    // AND START A NEW CONVERSATION. Clearing the role is not enough — the dock reopens the
    // last thread, which carries the role it was had with. A setup question opened under a
    // week-old Deployment Manager conversation, banner and all: "Continuing with the
    // Deployment Manager". A question about connecting a repository has nothing to do with
    // releases and rollbacks, and answering it with that history in context is worse than
    // answering it cold.
    url.searchParams.set("dock_fresh", "1");
    window.history.replaceState(null, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  } catch { /* non-browser */ }
}

function AskBoss({ about, problem }: { about: string; problem?: string | null }) {
  // The error is the whole value of asking. Without it the agent starts by asking what
  // happened, which the operator has already told the screen.
  const question = problem
    ? `I am setting up BOSS — ${about}. It failed with: ${problem}. What do I do?`
    : `I am setting up BOSS — ${about}. Walk me through it.`;
  return (
    <button type="button" className="cap-btn" data-testid="setup-ask-boss"
            onClick={() => askInDock(question)}>
      {problem ? "Ask BOSS about this error" : "Ask BOSS about this step"}
    </button>
  );
}

function GitHubStep({ projectId, actor, onDone }: {
  projectId: string; actor: string; onDone: () => void;
}) {
  const [token, setToken] = useState("");
  const [repo, setRepo] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const connect = useCallback(async () => {
    if (!token.trim() || busy) return;
    setBusy(true); setResult(null);
    const r = await call<Record<string, unknown>>({
      step: "github",
      payload: {
        project_id: projectId, actor,
        connection_label: "GitHub",
        auth_method: "personal_access_token",
        // THE TOKEN, not the name of an environment variable holding it. The env path still
        // works and is better for a server you own; this one exists so a first run does not
        // begin with "edit the container's environment and restart it".
        token: token.trim(),
        repository: repo.trim() || undefined,
        role: "primary",
        test_after_link: true,
      },
    });
    setBusy(false);
    // WIPE IT EITHER WAY. A token left in a form field survives every screenshot, screen share
    // and browser session restore after it.
    setToken("");
    if (!r.ok) { setResult({ ok: false, text: r.error ?? "could not connect" }); return; }
    // THE REASON LIVES IN `detail`. Reading only `message` and `status` produced "Connected,
    // but the check failed: failed" — four words that tell the operator nothing and told
    // BOSS's own agent nothing either, so it could only repeat them back.
    const test = (r.data?.test ?? null) as {
      status?: string; message?: string;
      detail?: { message?: string; missing_permissions?: string[] };
    } | null;
    const bound = (r.data?.repository_binding ?? null) as { full_name?: string } | null;
    const passed = !test || test.status === "passed" || test.status === "ok";
    const missing = test?.detail?.missing_permissions ?? [];
    const why = missing.length
      // The most common real failure, and the only one with a precise fix: the token works
      // and is short a permission. Name it rather than saying "failed".
      ? `the token is missing ${missing.join(" and ")} — add ${missing.length > 1 ? "them" : "it"} `
        + `at Read and write on the token, then press Connect and test again`
      : test?.detail?.message ?? test?.message ?? test?.status ?? "unknown";
    setResult({
      ok: passed,
      text: passed
        ? `Connected${bound?.full_name ? ` — ${bound.full_name} is the build target.` : "."}`
        : `Connected, but the check failed: ${why}`,
    });
    onDone();
  }, [token, repo, busy, projectId, actor, onDone]);

  return (
    <article className="stack-card" data-testid="setup-github">
      <div className="section-head">
        <div>
          <h2 className="eyebrow">Repository</h2>
          <p className="detail">
            BOSS writes code on a branch and opens a pull request — it never pushes to your
            default branch. It needs a token that can do those two things.
          </p>
        </div>
      </div>
      <p className="detail">
        A <a href="https://github.com/settings/personal-access-tokens/new" target="_blank"
             rel="noreferrer">fine-grained token</a> scoped to the one repository, with{" "}
        <b>Contents: read and write</b> and <b>Pull requests: read and write</b>. The token is
        written straight through to this install&rsquo;s secret store — BOSS keeps a reference,
        never the value.
      </p>
      <TokenHelp />
      <div className="setup-form">
        <label className="setup-field">
          <span>Token</span>
          <input type="password" value={token} autoComplete="off" spellCheck={false}
                 data-testid="setup-github-token"
                 placeholder="github_pat_…"
                 onChange={(e) => setToken(e.target.value)} />
        </label>
        <label className="setup-field">
          <span>Repository</span>
          <input type="text" value={repo} spellCheck={false}
                 data-testid="setup-github-repo"
                 placeholder="owner/name"
                 onChange={(e) => setRepo(e.target.value)} />
        </label>
        <button type="button" className="cap-btn primary" disabled={busy || !token.trim()}
                data-testid="setup-github-submit" onClick={() => void connect()}>
          {busy ? "Checking…" : "Connect and test"}
        </button>
      </div>
      {result ? (
        <p className={result.ok ? "setup-ok" : "setup-bad"} data-testid="setup-github-result">
          {result.text}
        </p>
      ) : null}
      <AskBoss about="connecting a GitHub repository so BOSS can open pull requests"
               problem={result && !result.ok ? result.text : null} />
    </article>
  );
}

/**
 * How to get the token, written out rather than linked to.
 *
 * "It will be easier rather me searching for the steps." Six clicks in somebody else's
 * settings UI is not knowledge worth holding, and a wizard that names the two permissions and
 * then leaves you to find the page has done the easy half.
 *
 * Collapsed, because the second visit does not need it and the field above is the point. The
 * deep link goes straight to the create form — that address has outlived several reshuffles of
 * the surrounding UI, which is exactly why it is here instead of a screenshot.
 */
function TokenHelp() {
  const [open, setOpen] = useState(false);
  return (
    <div className="setup-help" data-testid="setup-github-help">
      <button type="button" className="cap-btn" aria-expanded={open}
              data-testid="setup-github-help-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "Hide" : "How do I get a token?"}
      </button>
      {open ? (
        <ol className="hint" data-testid="setup-github-help-steps">
          <li>
            Open{" "}
            <a href="https://github.com/settings/personal-access-tokens/new" target="_blank"
               rel="noreferrer">github.com/settings/personal-access-tokens/new</a>{" "}
            — that is Settings → Developer settings → Personal access tokens → Fine-grained
            tokens → Generate new token.
          </li>
          <li>
            <b>Token name</b>: anything you will recognise later, such as <code>boss</code>.
            <b> Expiration</b>: your choice — BOSS keeps working until the day it expires, and
            then every build fails at push time, so pick a date you will not be surprised by.
          </li>
          <li>
            <b>Resource owner</b>: you, for your own repository. For a repository owned by an
            organisation, pick the organisation here — a token owned by you cannot reach it,
            and some organisations must approve the token before it works.
          </li>
          <li>
            <b>Repository access</b> → <i>Only select repositories</i> → choose the one
            repository BOSS should build in. It must already exist; BOSS writes branches and
            pull requests, it does not create the repository.
          </li>
          <li>
            <b>Permissions</b> → Repository permissions → set exactly two to{" "}
            <i>Read and write</i>: <b>Contents</b> and <b>Pull requests</b>. Everything else
            can stay at no access. Metadata turns itself on and is read-only — that is normal.
          </li>
          <li>
            <b>Generate token</b>, then copy it. GitHub shows it once. Paste it above and press
            Connect and test — BOSS checks it can actually push before saving anything.
          </li>
        </ol>
      ) : null}
      {open ? (
        <p className="hint">
          If your organisation blocks fine-grained tokens, a classic token with the{" "}
          <code>repo</code> scope works too. BOSS tests whichever you give it the same way:
          it asks GitHub whether this token can push to that repository, so a token that is
          short a permission is refused here rather than at push time.
        </p>
      ) : null}
    </div>
  );
}

function ModelStep({ projectId, actor, providers, saved, probeAvailable, onDone }: {
  projectId: string; actor: string; providers: Provider[]; saved: SavedModel[];
  probeAvailable: boolean;
  onDone: () => void;
}) {
  const [provider, setProvider] = useState("openrouter");
  // PICK UP THE LAST ONE SAVED. The wizard opened on its first dropdown entry every time,
  // which on an install that had already connected something read as though the connection
  // had been lost. `touched` is what keeps this a starting point rather than a correction:
  // once the operator chooses a provider themselves, a later status refresh must not move it.
  const [touched, setTouched] = useState(false);
  const startOn = lastSavedProvider(saved, providers);
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [agents, setAgents] = useState<CodingAgent[] | null>(null);
  const [liveProofAvailable, setLiveProofAvailable] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [token, setToken] = useState("");
  // The store fallback is a decision, so it is asked once and remembered for the retry
  // rather than being sent optimistically on the first attempt.
  const [acceptFallback, setAcceptFallback] = useState(false);

  useEffect(() => {
    if (touched || !startOn) return;
    setProvider(startOn);
    const next = providers.find((p) => p.provider === startOn);
    setBaseUrl(next?.base_url ?? "");
    setModel(next?.example_model ?? "");
  }, [touched, startOn, providers]);

  const chosen = providers.find((p) => p.provider === provider);
  const subscription = chosen?.kind === "subscription";
  // TypeSafe is listed where people look for model keys, but its key is for decisions only.
  const decisionKey = chosen?.kind === "decision";

  // ASK THE MACHINE, DO NOT ASK THE OPERATOR. A subscription is an OAuth session the CLI
  // holds; there is no credential to type, so the only honest question is whether the CLI is
  // here and signed in — and BOSS can answer that itself.
  const loadAgents = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/setup?view=coding-agents&project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (r.ok) {
        const status = await r.json();
        setAgents((status.agents ?? []) as CodingAgent[]);
        setLiveProofAvailable(status.live_proof_available === true);
      }
    } catch { setAgents(null); }   // fall back to the manual instructions below
  }, [projectId]);

  useEffect(() => {
    if (!subscription) { setAgents(null); return; }
    void loadAgents();
  }, [subscription, provider, loadAgents]);

  const detected = agents?.find((a) => a.name === provider) ?? null;

  const connectAgent = useCallback(async () => {
    if (connecting) return;
    setConnecting(true); setResult(null);
    const r = await call<Record<string, unknown>>({
      step: "coding-agent",
      payload: { project_id: projectId, actor, provider, prove: liveProofAvailable },
    });
    setConnecting(false);
    setResult(r.ok
      ? { ok: true, text: liveProofAvailable
          ? `Connected. ${provider} answered a test turn, and the entitlement is recorded. `
            + `Use it with BOSS_PROVIDER_LOCK=${provider}.`
          : `Connected without a live model check. The first governed job will verify `
            + `${provider}. Use it with BOSS_PROVIDER_LOCK=${provider}.` }
      // The API answers 422 with the next command for every fixable case, so show it verbatim
      // rather than "could not connect".
      : { ok: false, text: r.error ?? "could not connect" });
    // Re-read the recorded state rather than assuming it. The banner below is meant to say
    // what the database holds; setting it from the reply would make it say what we hoped.
    if (r.ok) { await loadAgents(); onDone(); }
  }, [connecting, projectId, actor, provider, liveProofAvailable, onDone, loadAgents]);

  const storeTokenAndConnect = useCallback(async () => {
    if (connecting || !token.trim()) return;
    setConnecting(true); setResult(null);
    const stored = await call<Record<string, unknown>>({
      step: "coding-agent-credential",
      payload: { project_id: projectId, actor, provider, credential: token.trim(),
                 accept_database_fallback: acceptFallback },
    });
    if (!stored.ok) {
      setConnecting(false);
      // 409 is not a failure, it is a question: this install has no secret store, so the
      // token would go into Postgres. Say so and let them decide, rather than storing it
      // quietly or refusing outright.
      if (stored.decision?.error === "secret_store_fallback_not_accepted") {
        setAcceptFallback(true);
        setResult({ ok: false, text: `${stored.error} Press the button again to store it `
                                     + `in the database deliberately.` });
        return;
      }
      setResult({ ok: false, text: stored.error ?? "could not store the token" });
      return;
    }
    // The value is gone from the form the moment BOSS has it. A credential left in an input
    // survives every screenshot and session restore after it.
    setToken("");
    const connected = await call<Record<string, unknown>>({
      step: "coding-agent",
      payload: { project_id: projectId, actor, provider, prove: liveProofAvailable },
    });
    setConnecting(false);
    setResult(connected.ok
      ? { ok: true, text: `Connected. BOSS holds the token in its `
                          + `${String(stored.data?.backend ?? "secret")} store. `
                          + (liveProofAvailable
                            ? `${provider} answered a test turn.`
                            : `${provider} will be verified by the first governed job.`) }
      : { ok: false, text: connected.error ?? "the token was stored but did not work" });
    await loadAgents();
    if (connected.ok) onDone();
  }, [connecting, token, acceptFallback, projectId, actor, provider,
      liveProofAvailable, onDone, loadAgents]);

  const save = useCallback(async () => {
    if (busy) return;
    setBusy(true); setResult(null);
    const r = await call<Record<string, unknown>>({
      step: "model",
      payload: { project_id: projectId, actor, provider,
                 api_key: apiKey.trim(), base_url: baseUrl.trim(), model: model.trim() },
    });
    setBusy(false);
    setApiKey("");
    if (!r.ok) { setResult({ ok: false, text: r.error ?? "could not save" }); return; }
    setResult({ ok: true, text: r.data?.verified === true
      ? `Working — answered as ${String(r.data?.model ?? "the model")}.`
      : `Key saved securely for ${String(r.data?.model ?? "the model")}. No live test was sent; `
        + "this model is unverified until a governed run uses it." });
    onDone();
  }, [busy, projectId, actor, provider, apiKey, baseUrl, model, onDone]);

  return (
    <article className="stack-card" data-testid="setup-model">
      <div className="section-head">
        <div>
          <h2 className="eyebrow">Model</h2>
          <p className="detail">
            An API key, anything that speaks the OpenAI API, or a subscription you already pay
            for. {probeAvailable
              ? "BOSS spends one token proving it works before saving it."
              : "Community saves an API key without a live test and marks it unverified; a "
                + "subscription is connected only after it answers one test turn."}
          </p>
        </div>
      </div>
      {/* WHAT IS ALREADY CONNECTED, BEFORE THE FORM ASKS AGAIN.
          This was one run-on sentence — "Saved here: OpenRouter — date, Claude Code
          subscription (subscription) — date, …" — which is a list pretending to be prose: no
          state, no colour, and no way to see which model does which job. Three connected
          models and an operator who could not tell they were connected. */}
      <ConnectedModels projectId={projectId} providers={providers} saved={saved} />
      <div className="setup-form">
        <label className="setup-field">
          <span>Provider</span>
          <select value={provider} data-testid="setup-model-provider"
                  onChange={(e) => {
                    setTouched(true);
                    setProvider(e.target.value); setResult(null);
                    const next = providers.find((p) => p.provider === e.target.value);
                    setBaseUrl(next?.base_url ?? "");
                    setModel(next?.example_model ?? "");
                  }}>
            {providers.map((p) => (
              <option key={p.provider} value={p.provider}>{p.label}</option>
            ))}
          </select>
        </label>
        {decisionKey ? (
          <DecisionKeyForm projectId={projectId} actor={actor} />
        ) : subscription ? null : (
          <>
            <label className="setup-field">
              <span>API key</span>
              <input type="password" value={apiKey} autoComplete="off" spellCheck={false}
                     data-testid="setup-model-key" placeholder="sk-…"
                     onChange={(e) => setApiKey(e.target.value)} />
            </label>
            {provider === "custom" ? (
              <label className="setup-field">
                <span>Base URL</span>
                <input type="text" value={baseUrl} spellCheck={false}
                       data-testid="setup-model-base"
                       placeholder="http://localhost:11434/v1"
                       onChange={(e) => setBaseUrl(e.target.value)} />
              </label>
            ) : null}
            <label className="setup-field">
              <span>Model</span>
              <input type="text" value={model} spellCheck={false}
                     data-testid="setup-model-name"
                     placeholder={chosen?.example_model || "model name"}
                     onChange={(e) => setModel(e.target.value)} />
            </label>
            <button type="button" className="cap-btn primary"
                    disabled={busy || !apiKey.trim() || !model.trim()}
                    data-testid="setup-model-submit" onClick={() => void save()}>
              {busy ? (probeAvailable ? "Checking…" : "Saving…")
                    : (probeAvailable ? "Verify and save" : "Save key without test")}
            </button>
          </>
        )}
      </div>
      {/* SAY WHAT ACTUALLY HAPPENS TODAY.
          The first version of this promised a connection session "once the provider's login
          flow is wired up", which reads as though BOSS shares a live session with your CLI.
          It does not, and `/connect` would not give it one: that endpoint RECORDS a connection
          from a reference the caller already has — it performs no login and no token exchange,
          so pointing the wizard at it would produce a green tick with nothing authorised.
          What genuinely works is the headless token the CLI mints for exactly this purpose. */}
      {subscription && detected ? (
        <div className="detail" data-testid="setup-model-agent-detected">
          <p>
            {chosen?.note}{" "}
            {detected.credential_kind === "token"
              // Said differently for the two routes, because the page below says something
              // different too. "There is nothing to paste" above a paste box is a page that
              // contradicts itself, and the operator believes the sentence, not the form.
              ? <>One value travels: the token its CLI mints for headless use. BOSS stores
                  that and runs the CLI here — your own login stays yours.</>
              : <>There is nothing to paste: the CLI holds its own login, and BOSS runs it on
                  this machine.</>}
          </p>
          {/* WHAT WAS SAVED, FIRST AND UNCONDITIONALLY.
              This line is the answer to "did my connection stick?", and it comes from
              entitlement_sources, not from a probe — so a restart, a refresh or a redeploy
              cannot make a finished step read as an untouched one. */}
          {detected.connected ? (
            <p className="setup-ok" data-testid="setup-agent-connected">
              Connected{detected.connected_at
                ? ` — saved ${detected.connected_at.slice(0, 16).replace("T", " ")}`
                : ""}. Nothing to redo.
            </p>
          ) : null}
          <p data-testid="setup-agent-state">
            <b>{detected.binary}</b>{" "}
            {detected.installed
              ? <>found{detected.version ? ` — ${detected.version}` : ""}.{" "}
                  {detected.authenticated === false
                    ? <>Not signed in.</>
                    : <>{detected.auth_detail ?? "Signed in."}</>}</>
              : <>not installed on this machine.</>}
          </p>
          {/* THE STEPS STAY ON SCREEN EVEN WHEN IT IS ALREADY WORKING.
              A wizard that hides the instructions the moment it is happy makes the SECOND
              visit worse than the first — and the second visit is the common one: a new
              machine, an expired login, a colleague being shown how. Re-running either
              command is safe, and so is pressing Connect again. */}
          <ol data-testid="setup-agent-steps" className="hint">
            <li>
              Install it: <code>{detected.install_hint ?? `install ${detected.binary}`}</code>
              {detected.installed ? " — already installed." : null}
            </li>
            <li>
              {provider === "claude_code"
                ? <>Sign in with <code>claude auth login</code> as the boss user inside the
                    API container, then rerun the installer with <code>--connect-claude-local</code>.
                    Or mint a token with <code>claude setup-token</code> and paste it below.
                    That token is stored only in the local secret store.</>
                : detected.credential_kind === "token"
                ? <>Mint a token for BOSS: <code>{detected.signin_hint}</code> on your own
                    machine, then paste it below. It is made for headless use, so it does not
                    disturb the session you sign in with yourself.</>
                : <>Sign in: <code>{detected.signin_hint}</code></>}
              {detected.authenticated ? " — already signed in." : null}
            </li>
            <li>
              {provider === "claude_code"
                ? <>Press <b>Store and connect</b> for a pasted setup token, or use{" "}
                    <code>--connect-claude-local</code> after the CLI login. BOSS then sends it
                    one trivial test turn — budgeted and receipted — and connects it only if it
                    answers; if the login is rejected it says so here.</>
                : detected.credential_kind === "token"
                // "It never asks you for a credential" is true of the file route and false
                // here, where the box below is asking for exactly one.
                ? <>Press <b>Store and connect</b>. {liveProofAvailable
                    ? "BOSS proves it with one real turn before recording anything."
                    : "Community records it without a live model check; the first governed job verifies it."}</>
                : <>Press Connect below. {liveProofAvailable
                    ? "BOSS checks it without asking you for a credential."
                    : "Community records eligible CLIs without a live model check."}</>}
              {detected.connected ? " — already connected." : null}
            </li>
          </ol>
          {/* A CREDENTIAL THAT TRAVELS GETS A BOX TO TRAVEL IN.
              claude_code reads a token it can mint anywhere, and until now the console said
              so and then left the operator to set an environment variable on the API
              container and restart it — the exact step this wizard exists to remove. BOSS
              takes the value and picks the store itself. Shown even once a token is present,
              because replacing one is how an expired token gets fixed. */}
          {detected.credential_kind === "token" ? (
            <div className="setup-form" data-testid="setup-agent-token">
              <label className="setup-field">
                <span>
                  Token from <code>{detected.signin_hint}</code>
                  {detected.token_present ? " — one is already stored; pasting replaces it" : ""}
                </span>
                <input type="password" value={token} autoComplete="off" spellCheck={false}
                       data-testid="setup-agent-token-input" placeholder="sk-ant-oat01-…"
                       onChange={(e) => setToken(e.target.value)} />
              </label>
              <button type="button" className="cap-btn primary"
                      disabled={connecting || !token.trim() || !detected.installed}
                      data-testid="setup-agent-token-submit"
                      onClick={() => void storeTokenAndConnect()}>
                {connecting ? "Storing and checking…"
                  : acceptFallback ? "Store in the database and connect"
                  : "Store and connect"}
              </button>
              <p className="hint">
                BOSS writes it straight through to this install&rsquo;s secret store and keeps
                a reference, never the value. No restart, and nothing to put in an env file.
              </p>
            </div>
          ) : null}
          <button className="button" disabled={connecting || !detected.installed}
                  data-testid="setup-agent-connect" onClick={() => void connectAgent()}>
            {connecting ? (liveProofAvailable ? "Checking it answers…" : "Connecting…")
              : detected.connected ? `${liveProofAvailable ? "Re-check" : "Reconnect"} ${chosen?.label ?? provider}`
              : detected.ready ? `Connect ${chosen?.label ?? provider}`
              : `${liveProofAvailable ? "Re-check" : "Connect"} ${chosen?.label ?? provider}`}
          </button>
          {detected.ready ? null
            : detected.credential_kind === "token" && !detected.token_present ? (
              // The thing to do is ON THIS PAGE. Detection's own next step is a shell
              // instruction for setting an environment variable, which was the right answer
              // until the box above existed and is a worse one now.
              <p data-testid="setup-agent-next-step">
                Paste the token above and press <b>Store and connect</b>.
              </p>
            ) : (
              // The next COMMAND, not "not configured" — which sends somebody to search.
              <p data-testid="setup-agent-next-step">
                Run <code>{detected.next_step}</code> first, then press Re-check.
              </p>
            )}
          <p className="hint">
            {liveProofAvailable
              ? <>Connecting proves it works before recording anything: BOSS makes the agent
                  complete one turn in its read-only sandbox. Signed in is not the same as
                  working — so pressing this again after re-authorising is the way to confirm it.</>
              : <>Community pauses live proof until model turns have budget, lease, and receipt
                  tracking. CLIs that require live proof cannot be connected yet; other
                  subscriptions can be recorded and verified by their first governed job.</>}
            {detected.docs_url ? <> The CLI&rsquo;s own docs: <code>{detected.docs_url}</code>.</> : null}
          </p>
        </div>
      ) : subscription ? (
        <div className="detail" data-testid="setup-model-subscription">
          <p>
            {chosen?.note} There is no browser login here and BOSS does not hold a live session
            from your machine.
          </p>
          {chosen?.provider === "claude_code" ? (
            <p>
              Run <code>claude setup-token</code> and paste it when this screen can see the
              CLI, or sign in with <code>claude auth login</code> as the boss user inside the
              API container and record it with <code>--connect-claude-local</code>. An
              operator-supplied token is stored only in the local secret store.{" "}
              <b>It powers builds, not this chat</b> — the conversation you are reading uses the
              model configured above.
            </p>
          ) : (
            <p>
              Set the provider&rsquo;s own headless token on this install. BOSS has no wizard
              step for it yet, and saying otherwise would be a green tick over nothing.
            </p>
          )}
        </div>
      ) : null}
      {chosen?.key_url && !decisionKey ? (
        <p className="detail">
          Keys live at <a href={chosen.key_url} target="_blank" rel="noreferrer">{chosen.key_url}</a>.
        </p>
      ) : null}
      {result ? (
        <p className={result.ok ? "setup-ok" : "setup-bad"} data-testid="setup-model-result">
          {result.text}
        </p>
      ) : null}
      {/* The step where the errors are least guessable — an expired subscription, a CLI signed
          in as another OS user, a plan that does not cover the model. The question carries the
          provider and the failure, so the answer can start from the failure. */}
      <AskBoss about={`connecting ${chosen?.label ?? provider} as the model BOSS builds with`}
               problem={result && !result.ok ? result.text : null} />
      <RoleModels projectId={projectId} actor={actor} />
      <DecisionModel projectId={projectId} />
    </article>
  );
}

/**
 * The models this project has, what each is, and what it is doing.
 *
 * A dot means BOSS has a usable source. The roles beside a model are the jobs it
 * currently holds — so "which model writes my code" is answered by looking, not by opening a
 * picker and reading four dropdowns.
 */
function ConnectedModels(
  { projectId, providers, saved }:
  { projectId: string; providers: Provider[]; saved: SavedModel[] },
) {
  const [roles, setRoles] = useState<Array<{ key: string; label: string; value: string }>>([]);
  const [connected, setConnected] = useState<string[]>([]);
  const [loginStates, setLoginStates] = useState<Record<string, string>>({});
  const [jev, setJev] = useState<DecisionStatus | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      // Jev does not build code, so its saved-key state is absent from connected_engines.
      // Show that state here without implying decision calls are enabled during BOSS-451.
      try {
        const r = await fetch(
          `/api/setup?view=decision-model&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (r.ok && alive) setJev((await r.json()) as DecisionStatus);
      } catch { /* the rest of the table stands on its own */ }
      try {
        const r = await fetch(`/api/settings?project_id=${encodeURIComponent(projectId)}`,
                              { cache: "no-store" });
        if (!r.ok || !alive) return;
        const body = await r.json();
        setConnected((body.connected_engines ?? []) as string[]);
        setLoginStates((body.engine_login_states ?? {}) as Record<string, string>);
        setRoles(((body.knobs ?? []) as Array<{ key: string; label: string; kind: string;
                                                value: unknown }>)
          .filter((k) => k.kind === "choice")
          .map((k) => ({ key: k.key, label: k.label, value: String(k.value ?? "") })));
      } catch { /* the chips are a summary; the form below still works */ }
    })();
    return () => { alive = false; };
  }, [projectId]);

  // One row per model this install can actually use. `saved` carries WHEN and WHAT KIND;
  // `connected_engines` is the authority on whether it is usable at all, and it includes a
  // model configured from the environment that was never "saved" through this screen.
  const rows = connected.map((engine) => {
    const from = saved.find((m) => m.wizard_provider === engine);
    return {
      engine,
      label: providers.find((p) => p.provider === engine)?.label
             ?? engine.replace(/_/g, " "),
      kind: from?.kind === "subscription" ? "subscription" : from ? "API key" : "this install",
      verified: from?.verified,
      at: from?.saved_at ? from.saved_at.slice(0, 10) : "",
      // The jobs it holds right now. Empty is a real answer and says so below.
      jobs: roles.filter((r) => r.value === engine).map((r) => r.label),
      // The provider rejected this login on its last call: it is connected and unusable.
      rejected: loginStates[engine] === "rejected",
    };
  });
  if (jev?.typesafe_key) {
    rows.push({
      engine: "typesafe",
      label: "TypeSafe Jev",
      kind: jev.execution_available ? "API key · decisions" : "saved key · paused",
      verified: jev.execution_available,
      at: "",
      jobs: jev.effective_backend === "typesafe" ? ["Workflow decisions"] : ["Paused"],
      rejected: false,
    });
  }
  if (!rows.length) return null;

  // A TABLE, BECAUSE THESE ARE ROWS WITH THE SAME FIELDS. As a flex list the roles column was
  // pushed right by `margin-left: auto`, so a model holding five roles wrapped onto its own
  // line while the two beside it sat flush right saying "no role yet" — three rows that never
  // lined up with each other. Columns make the comparison the eye is actually trying to do.
  return (
    <div className="setup-models" data-testid="setup-connected-models">
      <span className="setup-models-head">Models on this install</span>
      <table className="setup-models-table">
        <thead>
          <tr>
            <th scope="col">Model</th>
            <th scope="col">Connection</th>
            <th scope="col">Roles it holds</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.engine}>
              <th scope="row">
                <span className="setup-dot" aria-hidden
                      style={row.rejected ? { background: "var(--red, #F85149)" }
                        : row.kind === "saved key · paused" || (row.kind === "API key" && !row.verified)
                        ? { background: "var(--amber)" } : undefined} />
                {row.label}
              </th>
              <td className="setup-models-kind">
                {row.rejected ? (
                  <span className="setup-bad" data-testid={`setup-login-rejected-${row.engine}`}>
                    login rejected — reconnect it below
                  </span>
                ) : <>
                  {row.kind}{row.kind === "API key" && !row.verified ? " · unverified" : ""}
                  {row.at ? ` · ${row.at}` : ""}
                </>}
              </td>
              <td className="setup-models-jobs" data-empty={row.jobs.length ? undefined : "true"}>
                {row.jobs.length ? row.jobs.join(" · ") : "no role yet"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Which connected model fills which role.
 *
 * It belongs HERE, immediately after connecting one, because that is the moment the question
 * arises — and because these knobs first shipped under "Govern", beside concurrency and spend,
 * where nobody looking to change a model would think to look.
 *
 * VISIBLE, NOT COLLAPSED. It was a <details> that defaulted shut, on the reasoning that an
 * install with one provider has nothing to decide. Twice now the response has been the same —
 * "this is hiding somewhere which I cannot find it" — and the reasoning was wrong in a way
 * worth naming: which model does the implementing is not an advanced setting, it is the
 * second question anybody asks after connecting one, and a disclosure answers it only for
 * people who already suspect it exists.
 */
function RoleModels({ projectId, actor }: { projectId: string; actor: string }) {
  return (
    <article className="stack-card" data-testid="setup-role-models">
      <div className="section-head">
        <div>
          {/* The eyebrow TYPE, on a real heading. Matching "Already on this install" is a
              font choice, and dropping to a <span> to get it would cost the card its heading
              landmark for a screen reader — so the element stays an h2 and only the type
              changes. */}
          <div className="help-heading">
            <h2 className="eyebrow">Who does what</h2>
            <HelpTip label="Model roles"
              text="Choose the model that implements, reviews, or handles difficult work. An unset review or escalation role uses the implementer." />
          </div>
          <p className="detail">Saved per project; applies on the next run.</p>
        </div>
      </div>
      <RoleTable projectId={projectId} actor={actor} />
    </article>
  );
}


type DecisionStatus = {
  backend: string; effective_backend: string; configured_backend: string;
  claude_model: string; typesafe_key: boolean; typesafe_model: string;
  execution_available: boolean; hold_reason: string; key_url: string;
};

/**
 * Paste a TypeSafe key. BOSS checks it with one tiny decision before saving it — a billed call,
 * so it is reserved against the budget and receipted like every other decision (BOSS-451).
 */
function DecisionKeyForm({ projectId, actor }: { projectId: string; actor: string }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [acceptFallback, setAcceptFallback] = useState(false);
  const [saved, setSaved] = useState<boolean | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch(
          `/api/setup?view=decision-model&project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" });
        if (r.ok) setSaved(((await r.json()) as DecisionStatus).typesafe_key);
      } catch { setSaved(null); }
    })();
  }, [projectId, result]);

  const save = useCallback(async () => {
    if (busy || !key.trim()) return;
    setBusy(true); setResult(null);
    const r = await call<Record<string, unknown>>({
      step: "decision-key",
      payload: { project_id: projectId, actor, api_key: key.trim(),
                 accept_database_fallback: acceptFallback },
    });
    setBusy(false);
    if (!r.ok && r.decision?.error === "secret_store_fallback_not_accepted") {
      setAcceptFallback(true);
      setResult({ ok: false, text: `${r.error} Press Save again to store it in the database `
                                   + `deliberately.` });
      return;
    }
    if (r.ok) setKey("");   // gone from the form the moment BOSS holds it
    setResult(r.ok
      ? { ok: true, text: `Connected. Jev answered a test decision in `
                          + `${String(r.data?.latency_ms ?? "?")} ms. Workflow decisions on this `
                          + `project now go to Jev, and to Claude when Jev is unsure.` }
      : { ok: false, text: r.error ?? "could not save the key" });
  }, [busy, key, acceptFallback, projectId, actor]);

  return (
    <>
      <label className="setup-field">
        <span>TypeSafe API key{saved ? " — one is already saved; pasting replaces it" : ""}</span>
        <input type="password" value={key} autoComplete="off" spellCheck={false}
               data-testid="setup-decision-key" placeholder="ts_…"
               onChange={(e) => setKey(e.target.value)} />
      </label>
      <p className="detail">
        Keys live at <a href="https://console.typesafe.ai/keys" target="_blank"
        rel="noreferrer">console.typesafe.ai</a>. BOSS checks the key with one tiny decision
        before saving it; that check is budgeted and receipted like any other decision.
      </p>
      <button type="button" className="cap-btn primary" disabled={busy || !key.trim()}
              data-testid="setup-decision-key-submit" onClick={() => void save()}>
        {busy ? "Checking…" : "Check and save key"}
      </button>
      {result ? (
        <p className={result.ok ? "setup-ok" : "setup-bad"} data-testid="setup-decision-result">
          {result.text}
        </p>
      ) : null}
    </>
  );
}

/**
 * Who answers the multiple-choice questions — a workflow's typed_decision step picking a branch.
 *
 * Claude by default, on the subscription already connected above. TypeSafe's Jev is OPTIONAL
 * and billed to the operator's own TypeSafe account, so it is offered, never assumed: the card
 * says which one is actually deciding, because "typesafe" chosen with no key still means Claude.
 */
function DecisionModel({ projectId }: { projectId: string }) {
  const [status, setStatus] = useState<DecisionStatus | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/setup?view=decision-model&project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (r.ok) setStatus((await r.json()) as DecisionStatus);
    } catch { setStatus(null); }
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const deciding = status?.effective_backend === "typesafe"
    ? `TypeSafe Jev (${status.typesafe_model}), with Claude as the second opinion`
    : `Claude${status?.claude_model ? ` (${status.claude_model})` : ""} on your subscription`;

  return (
    <article className="stack-card" data-testid="setup-decision-model">
      <div className="section-head">
        <div>
          <h2 className="eyebrow">Decision model</h2>
          <p className="detail" data-testid="decision-model-status">
            Answers the multiple-choice questions in workflows — which branch to take, which
            team gets a ticket. Now deciding: <strong>{status ? deciding : "…"}</strong>.
            {status?.backend === "typesafe" && !status.typesafe_key
              ? " TypeSafe is selected but no key is saved, so Claude is deciding." : ""}
          </p>
          <p className="detail">
            Optional: TypeSafe&apos;s Jev answers in well under a second for a fraction of a
            cent, billed to your own TypeSafe account. Choose <em>TypeSafe Jev</em> as the
            Provider above to add its key. Every decision call is reserved against the budget
            and receipted before it is made.
          </p>
        </div>
      </div>
    </article>
  );
}
