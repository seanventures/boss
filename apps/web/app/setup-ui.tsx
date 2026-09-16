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
 * to think with. Each one PROVES itself against the real service before it says it worked — a
 * wizard that stores a key and reports success has verified that the operator can type.
 */

import React, { useCallback, useEffect, useState } from "react";

type Step = { id: string; title: string; done: boolean; detail: string;
              from_environment?: boolean; repository?: string | null };
type EnvCheck = {
  configured?: boolean; ok?: boolean; error?: string; source?: string;
  repository?: string; token_last4?: string; can_push?: boolean;
  default_branch?: string; private?: boolean;
  base_url?: string; model?: string; key_last4?: string;
  models_available?: number; model_listed?: boolean; note?: string;
};
type Status = {
  ready?: boolean; steps?: Step[]; repositories?: number;
  model_sources?: number; model_from_environment?: boolean;
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
};

async function call<T>(body: Record<string, unknown>): Promise<{ ok: boolean; data?: T; error?: string }> {
  try {
    const r = await fetch("/api/setup", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: String(data?.error ?? `HTTP ${r.status}`) };
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
  return (
    <section className="panel-stack" aria-label="Setup">
      <article className="stack-card">
        <div className="section-head">
          <div>
            <span className="eyebrow">Setup</span>
            <h2>Two things make a working install.</h2>
            <p className="detail">
              Somewhere to write code, and something to think with. Each step proves itself
              against the real service before it says it worked.
            </p>
          </div>
          {status ? (
            <span className={`cap-badge ${status.ready ? "tone-green" : "tone-amber"}`}
                  data-testid="setup-verdict">
              {status.ready ? "Ready" : "Not finished"}
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
      <ModelStep projectId={projectId} actor={actor} providers={providers} onDone={reload} />
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
 * Every line here is CHECKED, not merely read. "Connected" that means "an environment variable
 * is not empty" is the status that stays green while the first build dies at push time.
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
            Set from this install&rsquo;s environment. Checked just now against GitHub and the
            model endpoint — not read from a variable and assumed.
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
              {github.error ? <span className="setup-bad"> — {github.error}</span> : null}
            </dd>
          </div>
        ) : null}

        {model?.configured ? (
          <div data-testid="setup-env-model-fact">
            <dt>
              Model
              <span className={`cap-badge ${model.ok ? "tone-green" : "tone-amber"}`}>
                {model.ok ? "Answering" : "Check failed"}
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
    const test = (r.data?.test ?? null) as { status?: string; message?: string } | null;
    const bound = (r.data?.repository_binding ?? null) as { full_name?: string } | null;
    const passed = !test || test.status === "passed" || test.status === "ok";
    setResult({
      ok: passed,
      text: passed
        ? `Connected${bound?.full_name ? ` — ${bound.full_name} is the build target.` : "."}`
        : `Connected, but the check failed: ${test?.message ?? test?.status ?? "unknown"}`,
    });
    onDone();
  }, [token, repo, busy, projectId, actor, onDone]);

  return (
    <article className="stack-card" data-testid="setup-github">
      <div className="section-head">
        <div>
          <span className="eyebrow">Step one</span>
          <h2>Connect a repository</h2>
          <p className="detail">
            BOSS writes code on a branch and opens a pull request — it never pushes to your
            default branch. It needs a token that can do those two things.
          </p>
        </div>
      </div>
      <p className="detail">
        A <a href="https://github.com/settings/personal-access-tokens" target="_blank"
             rel="noreferrer">fine-grained token</a> scoped to the one repository, with{" "}
        <b>Contents: read and write</b> and <b>Pull requests: read and write</b>. The token is
        written straight through to this install&rsquo;s secret store — BOSS keeps a reference,
        never the value.
      </p>
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
    </article>
  );
}

function ModelStep({ projectId, actor, providers, onDone }: {
  projectId: string; actor: string; providers: Provider[]; onDone: () => void;
}) {
  const [provider, setProvider] = useState("openrouter");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [agents, setAgents] = useState<CodingAgent[] | null>(null);
  const [connecting, setConnecting] = useState(false);

  const chosen = providers.find((p) => p.provider === provider);
  const subscription = chosen?.kind === "subscription";

  // ASK THE MACHINE, DO NOT ASK THE OPERATOR. A subscription is an OAuth session the CLI
  // holds; there is no credential to type, so the only honest question is whether the CLI is
  // here and signed in — and BOSS can answer that itself.
  useEffect(() => {
    if (!subscription) { setAgents(null); return; }
    (async () => {
      try {
        const r = await fetch("/api/setup?view=coding-agents", { cache: "no-store" });
        if (r.ok) setAgents(((await r.json()).agents ?? []) as CodingAgent[]);
      } catch { setAgents(null); }   // fall back to the manual instructions below
    })();
  }, [subscription, provider]);

  const detected = agents?.find((a) => a.name === provider) ?? null;

  const connectAgent = useCallback(async () => {
    if (connecting) return;
    setConnecting(true); setResult(null);
    const r = await call<Record<string, unknown>>({
      step: "coding-agent",
      payload: { project_id: projectId, actor, provider, prove: true },
    });
    setConnecting(false);
    setResult(r.ok
      ? { ok: true, text: `Connected. ${provider} answered a test turn, and the entitlement `
                          + `is recorded. Use it with BOSS_PROVIDER_LOCK=${provider}.` }
      // The API answers 422 with the next command for every fixable case, so show it verbatim
      // rather than "could not connect".
      : { ok: false, text: r.error ?? "could not connect" });
    if (r.ok) onDone();
  }, [connecting, projectId, actor, provider, onDone]);

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
    setResult({ ok: true, text: `Working — answered as ${String(r.data?.model ?? "the model")}.` });
    onDone();
  }, [busy, projectId, actor, provider, apiKey, baseUrl, model, onDone]);

  return (
    <article className="stack-card" data-testid="setup-model">
      <div className="section-head">
        <div>
          <span className="eyebrow">Step two</span>
          <h2>Connect a model</h2>
          <p className="detail">
            An API key, anything that speaks the OpenAI API, or a subscription you already pay
            for. BOSS spends one token proving it works before saving it.
          </p>
        </div>
      </div>
      <div className="setup-form">
        <label className="setup-field">
          <span>Provider</span>
          <select value={provider} data-testid="setup-model-provider"
                  onChange={(e) => {
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
        {subscription ? null : (
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
              {busy ? "Checking…" : "Verify and save"}
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
            {chosen?.note} There is nothing to paste: the CLI holds its own login, and BOSS
            runs it on this machine.
          </p>
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
              Sign in: <code>{detected.signin_hint}</code>
              {detected.authenticated ? " — already signed in." : null}
            </li>
            <li>Press Connect below. BOSS checks it, it never asks you for a credential.</li>
          </ol>
          <button className="button" disabled={connecting || !detected.installed}
                  data-testid="setup-agent-connect" onClick={() => void connectAgent()}>
            {connecting ? "Checking it answers…"
              : detected.ready ? `Connect ${chosen?.label ?? provider}`
              : `Re-check ${chosen?.label ?? provider}`}
          </button>
          {!detected.ready ? (
            // The next COMMAND, not "not configured" — which sends somebody to search.
            <p data-testid="setup-agent-next-step">
              Run <code>{detected.next_step}</code> first, then press Re-check.
            </p>
          ) : null}
          <p className="hint">
            Connecting proves it works before recording anything: BOSS makes the agent complete
            one turn in its read-only sandbox. Signed in is not the same as working — so
            pressing this again after re-authorising is the way to confirm it, not a no-op.
            {detected.docs_url ? <> The CLI&rsquo;s own docs: <code>{detected.docs_url}</code>.</> : null}
          </p>
        </div>
      ) : subscription ? (
        <div className="detail" data-testid="setup-model-subscription">
          <p>
            {chosen?.note} There is no browser login here and BOSS does not hold a live session
            from your machine. What it uses is the long-lived token the CLI mints for headless
            use — you authorise once, on your own machine, and paste the result.
          </p>
          {chosen?.provider === "claude_code" ? (
            <p>
              Run <code>claude setup-token</code>, then set{" "}
              <code>CLAUDE_CODE_OAUTH_TOKEN</code> on this install. The coding pipeline picks it
              up for the builder agents.{" "}
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
      {chosen?.key_url ? (
        <p className="detail">
          Keys live at <a href={chosen.key_url} target="_blank" rel="noreferrer">{chosen.key_url}</a>.
        </p>
      ) : null}
      {result ? (
        <p className={result.ok ? "setup-ok" : "setup-bad"} data-testid="setup-model-result">
          {result.text}
        </p>
      ) : null}
    </article>
  );
}
