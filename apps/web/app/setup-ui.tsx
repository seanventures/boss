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

import { SettingsKnobs } from "./settings-knobs-ui";

type Step = { id: string; title: string; done: boolean; detail: string;
              from_environment?: boolean; repository?: string | null };
type EnvCheck = {
  configured?: boolean; ok?: boolean; error?: string; source?: string;
  repository?: string; token_last4?: string; can_push?: boolean;
  default_branch?: string; private?: boolean;
  base_url?: string; model?: string; key_last4?: string;
  models_available?: number; model_listed?: boolean; note?: string;
};
/** A model source this project has already saved — newest first in `Status.model_saved`. */
type SavedModel = {
  source_id: string; provider: string; wizard_provider: string; auth_method: string;
  kind: string; connected: boolean; verified: boolean; saved_at: string | null;
};
type Status = {
  ready?: boolean; steps?: Step[]; repositories?: number;
  model_sources?: number; model_from_environment?: boolean;
  model_saved?: SavedModel[];
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
      <ModelStep projectId={projectId} actor={actor} providers={providers}
                 saved={status?.model_saved ?? []} onDone={reload} />
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

function ModelStep({ projectId, actor, providers, saved, onDone }: {
  projectId: string; actor: string; providers: Provider[]; saved: SavedModel[];
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
  const [connecting, setConnecting] = useState(false);

  useEffect(() => {
    if (touched || !startOn) return;
    setProvider(startOn);
    const next = providers.find((p) => p.provider === startOn);
    setBaseUrl(next?.base_url ?? "");
    setModel(next?.example_model ?? "");
  }, [touched, startOn, providers]);

  const chosen = providers.find((p) => p.provider === provider);
  const subscription = chosen?.kind === "subscription";

  // ASK THE MACHINE, DO NOT ASK THE OPERATOR. A subscription is an OAuth session the CLI
  // holds; there is no credential to type, so the only honest question is whether the CLI is
  // here and signed in — and BOSS can answer that itself.
  const loadAgents = useCallback(async () => {
    try {
      const r = await fetch(
        `/api/setup?view=coding-agents&project_id=${encodeURIComponent(projectId)}`,
        { cache: "no-store" });
      if (r.ok) setAgents(((await r.json()).agents ?? []) as CodingAgent[]);
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
      payload: { project_id: projectId, actor, provider, prove: true },
    });
    setConnecting(false);
    setResult(r.ok
      ? { ok: true, text: `Connected. ${provider} answered a test turn, and the entitlement `
                          + `is recorded. Use it with BOSS_PROVIDER_LOCK=${provider}.` }
      // The API answers 422 with the next command for every fixable case, so show it verbatim
      // rather than "could not connect".
      : { ok: false, text: r.error ?? "could not connect" });
    // Re-read the recorded state rather than assuming it. The banner below is meant to say
    // what the database holds; setting it from the reply would make it say what we hoped.
    if (r.ok) { await loadAgents(); onDone(); }
  }, [connecting, projectId, actor, provider, onDone, loadAgents]);

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
      {/* WHAT IS ALREADY SAVED, BEFORE THE FORM. The form asks a question this list has
          already answered, and reading the question first is what made an operator believe a
          connection had been lost when it was sitting in the database the whole time. */}
      {saved.length ? (
        <p className="detail" data-testid="setup-model-saved">
          Saved here:{" "}
          {saved.map((m, i) => {
            const label = providers.find((p) => p.provider === m.wizard_provider)?.label
                          ?? m.source_id;
            return (
              <span key={m.source_id}>
                {i ? ", " : ""}
                <b>{label}</b>
                {m.kind === "subscription" ? " (subscription)" : ""}
                {m.saved_at ? ` — ${m.saved_at.slice(0, 16).replace("T", " ")}` : ""}
              </span>
            );
          })}
          . The picker starts on the most recent one.
        </p>
      ) : null}
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
              Sign in: <code>{detected.signin_hint}</code>
              {detected.authenticated ? " — already signed in." : null}
            </li>
            <li>
              Press Connect below. BOSS checks it, it never asks you for a credential.
              {detected.connected ? " — already connected." : null}
            </li>
          </ol>
          <button className="button" disabled={connecting || !detected.installed}
                  data-testid="setup-agent-connect" onClick={() => void connectAgent()}>
            {connecting ? "Checking it answers…"
              : detected.connected ? `Re-check ${chosen?.label ?? provider}`
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
      <RoleModels projectId={projectId} actor={actor} />
    </article>
  );
}

/**
 * Which connected model fills which role.
 *
 * It belongs HERE, immediately after connecting one, because that is the moment the question
 * arises — and because these knobs first shipped under "Govern", beside concurrency and spend,
 * where nobody looking to change a model would think to look.
 *
 * Collapsed by default: an installation with one provider has nothing to decide, and a wizard
 * that opens four pickers in front of somebody who has just connected their first model makes
 * a simple setup look like a configuration exercise.
 */
function RoleModels({ projectId, actor }: { projectId: string; actor: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="setup-roles" data-testid="setup-role-models">
      <button type="button" className="cap-btn" aria-expanded={open}
              data-testid="setup-role-models-toggle"
              onClick={() => setOpen((v) => !v)}>
        {open ? "Hide" : "Choose"} a model per role
      </button>
      <p className="detail">
        Who implements, who reviews it adversarially, and which model takes over when a task
        escalates. Unset means the implementer does that job too. Saved per project and applied
        on the next run — no restart.
      </p>
      {open ? <SettingsKnobs projectId={projectId} actor={actor} only="choice" /> : null}
    </div>
  );
}
