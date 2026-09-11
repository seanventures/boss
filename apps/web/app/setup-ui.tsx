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

type Step = { id: string; title: string; done: boolean; detail: string };
type Status = {
  ready?: boolean; steps?: Step[]; repositories?: number;
  model_sources?: number; model_from_environment?: boolean;
};
type Provider = {
  provider: string; label: string; kind: string; api: string;
  base_url?: string; example_model?: string; key_url?: string; note?: string;
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
              </span>
            </li>
          ))}
        </ol>
      </article>

      <GitHubStep projectId={projectId} actor={actor} onDone={reload} />
      <ModelStep projectId={projectId} actor={actor} providers={providers} onDone={reload} />
    </section>
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

  const chosen = providers.find((p) => p.provider === provider);
  const subscription = chosen?.kind === "subscription";

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
      {/* NOT A KEY BOX THAT WOULD BE REJECTED ON SUBMIT. A subscription is authorised through
          its provider's own login — BOSS refuses a pasted credential for one by policy — so the
          wizard says so rather than offering a field that cannot work. */}
      {subscription ? (
        <p className="detail" data-testid="setup-model-subscription">
          {chosen?.note} BOSS does not accept a pasted credential for a subscription — it is
          authorised through a connection session, which is where this will take you once the
          provider&rsquo;s login flow is wired up. Until then, use an API key above, or set the
          provider&rsquo;s own environment variable on this install.
        </p>
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
