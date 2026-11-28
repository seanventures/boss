"use client";

/**
 * BOSSAPP-1270. A builder writes one sentence. The saved rule, not this screen,
 * decides whether a live action runs.
 *
 * The gate ticket is the screen: a notched plate on a sea-glass bench. The
 * effect is punched in the notch. Sea glass is the bench, not a cream page,
 * and the plate is one dark object, not a dark product.
 */
import React, { useEffect, useState } from "react";
import { apiWrite } from "../../lib/api-write";
import { GatedButton } from "../../components/gated";

export type LivePredicate = {
  field?: string;
  op?: string;
  value?: string | number;
};

export type LiveRule = {
  id: string;
  app_key: string;
  description: string;
  effect: "allow" | "block";
  action_pattern: string;
  predicate: LivePredicate;
  position: number;
};

export type AttemptResult = {
  decision: "allow" | "block";
  ran: boolean;
  rule_id: string | null;
  decision_id: string | null;
  action: string;
  result: Record<string, unknown> | null;
  repeated?: boolean;
};

export type LiveRulesTransport = {
  list: (projectId: string, appKey: string) => Promise<{ rules: LiveRule[] }>;
  record: (body: {
    project_id: string;
    app_key: string;
    description: string;
    actor: string;
  }) => Promise<{ rule: LiveRule }>;
  attempt: (body: {
    project_id: string;
    app_key: string;
    action: string;
    actor: string;
    payload: Record<string, string | number>;
  }) => Promise<AttemptResult>;
};

const OPS: Record<string, string> = { gt: ">", lt: "<", gte: "≥", lte: "≤", eq: "=" };

async function readError(res: Response): Promise<string> {
  const raw = await res.text();
  try {
    const body = JSON.parse(raw) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
    if (Array.isArray(body.detail)) {
      const parts = body.detail.map((item) => {
        if (item && typeof item === "object" && "msg" in item) return String(item.msg ?? "");
        return "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ");
    }
  } catch {
    /* not JSON */
  }
  return raw.trim() || `Request failed (${res.status}).`;
}

async function take<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(await readError(res));
  return res.json() as Promise<T>;
}

const httpTransport: LiveRulesTransport = {
  async list(projectId, appKey) {
    const query = new URLSearchParams({ project_id: projectId, app_key: appKey });
    return take(await fetch(`/v1/live-action-rules?${query}`, { cache: "no-store" }));
  },
  async record(body) {
    return take(await apiWrite("/v1/live-action-rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  },
  async attempt(body) {
    return take(await apiWrite("/v1/live-action-rules/attempt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  },
};

function projectFrom(given?: string): string {
  const named = given?.trim();
  if (named) return named;
  if (typeof window !== "undefined") {
    const query = new URLSearchParams(window.location.search).get("project_id");
    if (query?.trim()) return query.trim();
  }
  return "boss-dogfood";
}

function predicateLine(rule: LiveRule): string | null {
  const predicate = rule.predicate;
  if (!predicate?.field || !predicate.op) return null;
  const op = OPS[predicate.op] ?? predicate.op;
  return `${predicate.field} ${op} ${predicate.value}`;
}

function payloadValue(raw: string): string | number {
  if (/^-?\d+$/.test(raw)) return Number(raw);
  if (/^-?\d+\.\d+$/.test(raw)) return Number(raw);
  return raw;
}

export function LiveRules({
  projectId,
  actor = "builder",
  initialAppKey = "storefront",
  initialRules,
  transport,
}: {
  projectId?: string;
  actor?: string;
  initialAppKey?: string;
  initialRules?: LiveRule[];
  transport?: LiveRulesTransport;
}) {
  const api = transport ?? httpTransport;
  const project = projectFrom(projectId);
  const [appKey, setAppKey] = useState(initialAppKey);
  const [rules, setRules] = useState<LiveRule[]>(initialRules ?? []);
  const [description, setDescription] = useState("");
  const [action, setAction] = useState("");
  const [field, setField] = useState("");
  const [value, setValue] = useState("");
  const [attempt, setAttempt] = useState<AttemptResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api.list(project, appKey.trim() || initialAppKey).then((data) => {
      if (alive) setRules(data.rules ?? []);
    }).catch((err: unknown) => {
      if (alive) setError(err instanceof Error ? err.message : "The rules did not load.");
    });
    return () => { alive = false; };
  }, [api, project, appKey, initialAppKey]);

  async function onRecord() {
    const sentence = description.trim();
    if (!sentence) {
      setError("Write a rule first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const saved = await api.record({
        project_id: project,
        app_key: appKey.trim() || initialAppKey,
        description: sentence,
        actor,
      });
      setRules((current) => [...current, saved.rule]);
      setDescription("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "The rule was not saved.");
    } finally {
      setBusy(false);
    }
  }

  async function onTry() {
    const name = action.trim();
    if (!name) {
      setError("Name the action.");
      return;
    }
    const named = field.trim();
    const raw = value.trim();
    if ((named && !raw) || (!named && raw)) {
      setError("Name the field and the value.");
      return;
    }
    const body: Record<string, string | number> = {};
    if (named && raw) body[named] = payloadValue(raw);
    setBusy(true);
    setError(null);
    try {
      const outcome = await api.attempt({
        project_id: project,
        app_key: appKey.trim() || initialAppKey,
        action: name,
        actor,
        payload: body,
      });
      setAttempt(outcome);
    } catch (err) {
      setAttempt(null);
      setError(err instanceof Error ? err.message : "The action was not tried.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="lr-gate" data-testid="lr-gate">
      <style>{GATE_CSS}</style>
      <header className="lr-head">
        <p className="lr-eyebrow">Live gate</p>
        <h1>Say what may run.</h1>
        <p id="lr-thesis">Nothing runs until a saved rule allows it.</p>
        <p>A model does not read the rule.</p>
      </header>

      <div className="lr-bench">
        <label className="lr-label" htmlFor="lr-app">App</label>
        <input id="lr-app" data-testid="lr-app" value={appKey} title="The live app this rule gates"
               onChange={(event) => setAppKey(event.target.value)} autoComplete="off" />
        <label className="lr-label" htmlFor="lr-rule">Rule</label>
        <textarea id="lr-rule" value={description} rows={3} placeholder="block refund"
                  title="Example: block refund when amount is over 50"
                  aria-describedby="lr-thesis"
                  onChange={(event) => setDescription(event.target.value)} />
        <GatedButton permission="policy:manage" type="button" data-testid="lr-record" disabled={busy}
                title="Save this sentence as a rule" onClick={() => void onRecord()}>
          Record rule
        </GatedButton>
      </div>

      {error ? <p className="lr-error" data-testid="lr-error" role="alert">{error}</p> : null}

      <h2>Saved rules</h2>
      {rules.length === 0 ? <p data-testid="lr-empty">No rule yet.</p> : (
        <ol className="lr-tape">
          {rules.map((rule) => {
            const when = predicateLine(rule);
            return (
              <li key={rule.id} className="lr-plate" data-testid="lr-rule">
                <div className={`lr-notch lr-${rule.effect}`} data-testid="lr-effect">{rule.effect.toUpperCase()}</div>
                <div className="lr-plate-body">
                  <span className="lr-serial" data-testid="lr-action" data-user-text>{rule.action_pattern}</span>
                  {when ? <span className="lr-when" data-user-text>{when}</span> : null}
                  <span className="lr-said" data-user-text>{rule.description}</span>
                </div>
              </li>
            );
          })}
        </ol>
      )}

      <h2>Try an action</h2>
      <div className="lr-try">
        <label className="lr-label" htmlFor="lr-try-action">Action</label>
        <input id="lr-try-action" data-testid="lr-try-action" value={action}
               onChange={(event) => setAction(event.target.value)} autoComplete="off" />
        <label className="lr-label" htmlFor="lr-try-field">Field</label>
        <input id="lr-try-field" data-testid="lr-try-field" value={field} title="A payload field, optional"
               onChange={(event) => setField(event.target.value)} autoComplete="off" />
        <label className="lr-label" htmlFor="lr-try-value">Value</label>
        <input id="lr-try-value" data-testid="lr-try-value" value={value} title="That field's value, optional"
               onChange={(event) => setValue(event.target.value)} autoComplete="off" />
        <button type="button" data-testid="lr-try" disabled={busy}
                title="Run it if a rule allows it" onClick={() => void onTry()}>
          Try action
        </button>
      </div>
      {attempt ? (
        <p className="lr-result" data-testid="lr-result">
          {attempt.ran ? (attempt.repeated ? "Already ran." : "Ran.") : "Did not run."}
          {attempt.decision_id ? (
            <span data-testid="lr-decision" data-user-text> {attempt.decision_id}</span>
          ) : null}
        </p>
      ) : null}
    </section>
  );
}

const GATE_CSS = `
.lr-gate {
  --bay: #d7e4de;
  --ink: #14241f;
  --plate: #12352c;
  --plate-ink: #f4f0e6;
  --brass: #e4b15a;
  --flare: #ff6a45;
  --paper: #f7faf8;
  --line: #6e9084;
  box-sizing: border-box;
  min-height: 70vh;
  padding: 28px 20px 48px;
  background: var(--bay);
  color: var(--ink);
  font-family: Manrope, ui-sans-serif, system-ui, sans-serif;
}
.lr-gate *, .lr-gate *::before, .lr-gate *::after { box-sizing: border-box; }
.lr-head, .lr-bench, .lr-try { max-width: 720px; }
.lr-eyebrow {
  margin: 0 0 8px;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: #24584c;
}
.lr-gate h1 {
  margin: 0 0 12px;
  font-family: Sora, Manrope, sans-serif;
  font-size: 40px;
  line-height: 1;
  font-weight: 600;
  letter-spacing: -0.04em;
}
.lr-gate h2 {
  max-width: 720px;
  margin: 28px 0 10px;
  font-family: Sora, Manrope, sans-serif;
  font-size: 18px;
  font-weight: 600;
}
.lr-gate p { margin: 0 0 8px; max-width: 42rem; line-height: 1.45; }
.lr-bench, .lr-try { display: grid; gap: 8px; }
.lr-label { font-size: 13px; font-weight: 700; }
.lr-gate input, .lr-gate textarea, .lr-gate button { font-family: inherit; font-size: 16px; }
.lr-gate input, .lr-gate textarea {
  width: 100%;
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--ink);
  padding: 10px 12px;
  border-radius: 0;
}
.lr-gate textarea { resize: vertical; min-height: 76px; }
.lr-gate button {
  justify-self: start;
  border: 0;
  background: var(--plate);
  color: var(--plate-ink);
  padding: 10px 16px;
  cursor: pointer;
}
.lr-gate button:disabled { opacity: 0.55; cursor: not-allowed; }
.lr-gate button:focus-visible, .lr-gate input:focus-visible, .lr-gate textarea:focus-visible {
  outline: 2px solid var(--plate);
  outline-offset: 2px;
}
.lr-error { color: #8d1d12; font-weight: 700; }
.lr-tape { list-style: none; margin: 0; padding: 0; max-width: 720px; display: grid; gap: 10px; }
.lr-plate {
  display: flex;
  min-height: 96px;
  background: var(--plate);
  color: var(--plate-ink);
  animation: lr-in 220ms ease;
}
.lr-notch {
  width: 112px;
  flex: none;
  display: grid;
  place-items: center;
  background: #0c241e;
  clip-path: polygon(0 0, 100% 0, 100% 38%, 84% 50%, 100% 62%, 100% 100%, 0 100%);
  font-family: Sora, Manrope, sans-serif;
  font-weight: 700;
  letter-spacing: 0.08em;
  font-size: 16px;
}
.lr-allow { color: var(--brass); }
.lr-block { color: var(--flare); }
.lr-plate-body { display: grid; align-content: center; gap: 4px; padding: 14px 16px; }
.lr-serial {
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 20px;
  letter-spacing: 0.02em;
}
.lr-when, .lr-said { font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 12px; color: #d5e6df; }
.lr-result { font-family: Sora, Manrope, sans-serif; font-size: 22px; font-weight: 600; }
@media (max-width: 640px) {
  .lr-gate h1 { font-size: 32px; }
  .lr-plate { flex-direction: column; }
  .lr-notch { width: 100%; height: 48px; clip-path: none; }
}
@media (prefers-reduced-motion: reduce) {
  .lr-plate { animation: none; }
}
@keyframes lr-in {
  from { transform: translateY(6px); opacity: 0; }
  to { transform: none; opacity: 1; }
}
`;
