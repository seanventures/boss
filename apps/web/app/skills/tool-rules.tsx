"use client";

/**
 * SKILLS & MCP › RULES (BOSSAPP-815/817): the ordered tool-policy rules, in the order the engine
 * reads them. One table: priority, who it applies to, what it does, which tools, the argument
 * conditions, hits — enable/disable and revoke per row. "Always allow" rows (made from an
 * approval) are the same rows, filtered by the chip. A simulate box answers "what would happen
 * if this agent called this tool with these arguments?" without recording anything.
 *
 * No rule can lift Off, the tier floor (tier 2+ always asks) or a quarantine — the simulate
 * answer says so when it happens.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  BUCKET_WORDS, BUCKETS, conditionWords, createRule, EFFECT_WORDS, EFFECTS, ordered, rateWords, readConditions,
  readRules, readSuggestions, revokeRule, scopeWords, simulate, updateRule, parseSimulation,
  type Bucket, type Effect, type Rule, type Simulation, type Suggestion,
} from "../../lib/tool-policies";

type Filter = "all" | "decision";

export function ToolRules({ projectId }: { projectId: string; actor?: string }) {
  const [rules, setRules] = useState<Rule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [said, setSaid] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    const r = await readRules(projectId);
    if (r.ok && "data" in r && r.data) { setRules(r.data); setError(null); }
    else if (!r.ok) setError(r.error);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const act = async (p: Promise<{ ok: boolean; error: string | null }>, done: string) => {
    const r = await p;
    setSaid(r.ok ? done : r.error ?? "Not saved.");
    if (r.ok) void load();
  };

  const shown = ordered((rules ?? []).filter((r) => filter === "all" || r.source === "decision"));
  return (
    <div className="tp-rules" data-testid="tool-rules">
      <div className="tp-head">
        <div className="tp-chips" role="radiogroup" aria-label="Which rules">
          {(["all", "decision"] as const).map((f) => (
            <button key={f} type="button" role="radio" aria-checked={filter === f} className="kit-tab"
                    data-testid={`tool-rules-filter-${f}`} onClick={() => setFilter(f)}>
              {f === "all" ? "All rules" : "Always allow"}
            </button>
          ))}
        </div>
        <span className="kit-spacer" />
        <GatedButton permission="mcp_server:review" projectId={projectId} className="kit-btn"
                     data-testid="tool-rules-add" onClick={() => setAdding((a) => !a)}>
          {adding ? "Close" : "Add rule"}
        </GatedButton>
      </div>
      {said ? <p className="tp-said" role="status" data-testid="tool-rules-said">{said}</p> : null}
      {adding ? <AddRule projectId={projectId} onAdded={() => { setAdding(false); setSaid("Rule added."); void load(); }} /> : null}
      {error && !rules ? <p className="tp-said" data-testid="tool-rules-error">{error}</p> : null}
      {!rules && !error ? <p className="tp-muted" aria-busy="true">Loading…</p> : null}
      {rules && shown.length === 0 ? (
        <p className="tp-muted" data-testid="tool-rules-empty">
          {filter === "decision" ? "No Always allow yet — choose it on an approval." : "No rules. Each tool's Allow / Ask / Off stands."}
        </p>
      ) : null}
      {shown.length ? (
        <div className="tp-table-wrap">
          <table className="tp-table" data-testid="tool-rules-table">
            <thead>
              <tr><th>#</th><th>Applies to</th><th>Does</th><th>Tools</th><th>When</th><th>Hits</th><th /></tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} data-testid={`tool-rule-${r.id}`} data-status={r.status}>
                  <td>{r.priority}</td>
                  <td>{scopeWords(r.scope)}{r.source === "decision" ? <span className="tp-tag" title={r.source_decision_id ?? ""}>Always allow</span> : null}</td>
                  <td><span className={`tp-eff tp-eff-${r.effect}`}>{EFFECT_WORDS[r.effect]}</span>
                    {r.rate_limit ? <span className="tp-muted" data-testid={`tool-rule-rate-${r.id}`}> {rateWords(r.rate_limit)}</span> : null}</td>
                  <td className="tp-mono">{r.selector.tool} @ {r.selector.server}</td>
                  <td className="tp-mono" title={r.expires_at ? `Expires ${r.expires_at}` : undefined}>
                    {r.conditions.length ? conditionWords(r.conditions) : "any arguments"}</td>
                  <td>{r.hit_count}</td>
                  <td className="tp-actions">
                    <GatedButton permission="mcp_server:review" projectId={projectId} className="kit-link"
                                 data-testid={`tool-rule-toggle-${r.id}`}
                                 onClick={() => void act(updateRule(projectId, r.id, { status: r.status === "active" ? "disabled" : "active" },
                                                                    r.status === "active" ? "Disable" : "Enable"),
                                                         r.status === "active" ? "Rule disabled." : "Rule enabled.")}>
                      {r.status === "active" ? "Disable" : "Enable"}
                    </GatedButton>
                    <GatedButton permission="mcp_server:review" projectId={projectId} className="kit-link"
                                 data-testid={`tool-rule-revoke-${r.id}`}
                                 onClick={() => void act(revokeRule(projectId, r.id), "Rule revoked.")}>Revoke</GatedButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <Suggested projectId={projectId} rulesVersion={rules} onAdded={(m) => { setSaid(m); void load(); }} />
      <SimulateBox projectId={projectId} />
    </div>
  );
}

/** SUGGESTED LIMITS (BOSSAPP-868): the MCP catalogue's default rules for this project's catalogue
 *  servers that it does not have yet. Nothing applies by itself; Add writes the rule as shown. */
function Suggested({ projectId, rulesVersion, onAdded }: { projectId: string; rulesVersion: unknown; onAdded: (m: string) => void }) {
  const [items, setItems] = useState<Suggestion[]>([]);
  useEffect(() => {
    let live = true;
    void readSuggestions(projectId).then((r) => { if (live && r.ok && "data" in r && r.data) setItems(r.data); });
    return () => { live = false; };
  }, [projectId, rulesVersion]);
  if (!items.length) return null;
  return (
    <section className="tp-sim" aria-label="Suggested limits" data-testid="tool-rules-suggested">
      <h4>Suggested limits</h4>
      <ul className="tp-suggest">
        {items.map((s, i) => (
          <li key={`${s.server}-${i}`} data-testid={`tool-rules-suggestion-${i}`}>
            <span className={`tp-eff tp-eff-${s.summary.effect}`}>{EFFECT_WORDS[s.summary.effect]}</span>{" "}
            {s.summary.rate_limit ? <span className="tp-muted">{rateWords(s.summary.rate_limit)} </span> : null}
            <span className="tp-mono">{s.summary.selector.tool} @ {s.summary.selector.server}</span>
            {s.why ? <span className="tp-muted" title={s.why}> · why</span> : null}{" "}
            <GatedButton permission="mcp_server:review" projectId={projectId} className="kit-link"
                         data-testid={`tool-rules-suggestion-add-${i}`}
                         onClick={() => void createRule(projectId, s.rule).then((r) => onAdded(r.ok ? "Rule added." : r.error ?? "Not saved."))}>
              Add
            </GatedButton>
          </li>
        ))}
      </ul>
    </section>
  );
}

function AddRule({ projectId, onAdded }: { projectId: string; onAdded: () => void }) {
  const [effect, setEffect] = useState<Effect>("deny");
  const [scope, setScope] = useState("project");
  const [priority, setPriority] = useState("100");
  const [server, setServer] = useState("*");
  const [tool, setTool] = useState("");
  const [conds, setConds] = useState("");
  const [cap, setCap] = useState("20");
  const [windowS, setWindowS] = useState("3600");
  const [bucket, setBucket] = useState<Bucket>("project");
  const [err, setErr] = useState<string | null>(null);
  const parsed = readConditions(conds);
  const save = async () => {
    if (parsed.error) { setErr(parsed.error); return; }
    const r = await createRule(projectId, {
      effect, scope: scope.trim() || "project", priority: Number(priority) || 0,
      selector: { server: server.trim() || "*", tool: tool.trim() || "*" }, conditions: parsed.conditions,
      ...(effect === "rate_limit" ? { rate_limit: { max_calls: Number(cap) || 0, window_seconds: Number(windowS) || 60,
                                                     ...(bucket === "project" ? {} : { bucket }) } } : {}),
    });
    if (r.ok) onAdded(); else setErr(r.error ?? "Not saved.");
  };
  return (
    <div className="tp-form" data-testid="tool-rules-form">
      <label>Does<select aria-label="Effect" value={effect} onChange={(e) => setEffect(e.target.value as Effect)}>
        {EFFECTS.map((x) => <option key={x} value={x}>{EFFECT_WORDS[x]}</option>)}</select></label>
      <label>Applies to<input aria-label="Scope" value={scope} placeholder="project, org, agent:forge, workflow:<id>"
                             onChange={(e) => setScope(e.target.value)} /></label>
      <label>Priority<input aria-label="Priority" inputMode="numeric" value={priority} onChange={(e) => setPriority(e.target.value)} /></label>
      <label>Server<input aria-label="Server" value={server} placeholder="* or a name" onChange={(e) => setServer(e.target.value)} /></label>
      <label>Tool<input aria-label="Tool" value={tool} placeholder="delete_* or a name" onChange={(e) => setTool(e.target.value)} /></label>
      {effect === "rate_limit" ? (
        <label>Calls per window<span className="tp-pair">
          <input aria-label="Max calls" inputMode="numeric" value={cap} onChange={(e) => setCap(e.target.value)} />
          <input aria-label="Window seconds" inputMode="numeric" value={windowS} onChange={(e) => setWindowS(e.target.value)} />
          <select aria-label="Counted" value={bucket} onChange={(e) => setBucket(e.target.value as Bucket)}>
            {BUCKETS.map((b) => <option key={b} value={b}>{BUCKET_WORDS[b]}</option>)}</select></span></label>
      ) : null}
      <label className="tp-wide">When (optional, one per line)<textarea aria-label="Conditions" rows={2} value={conds}
        placeholder={"$.path prefix /prod\n$.branch equals \"main\""} onChange={(e) => setConds(e.target.value)} /></label>
      {err || parsed.error ? <p className="tp-said">{err ?? parsed.error}</p> : null}
      <GatedButton permission="mcp_server:review" projectId={projectId} className="kit-btn" data-testid="tool-rules-save"
                   onClick={() => void save()}>Save rule</GatedButton>
    </div>
  );
}

function SimulateBox({ projectId }: { projectId: string }) {
  const [server, setServer] = useState("");
  const [tool, setTool] = useState("");
  const [role, setRole] = useState("");
  const [args, setArgs] = useState("{}");
  const [out, setOut] = useState<Simulation | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const run = async () => {
    let parsedArgs: unknown;
    try { parsedArgs = JSON.parse(args || "{}"); } catch { setErr("Arguments must be JSON."); return; }
    const r = await simulate(projectId, { server: server.trim(), tool: tool.trim(), arguments: parsedArgs,
                                          role_key: role.trim() || null });
    if (r.ok) { setOut(parseSimulation(r.body)); setErr(null); } else { setOut(null); setErr(r.error); }
  };
  return (
    <section className="tp-sim" aria-label="Simulate a call" data-testid="tool-rules-simulate">
      <h4>Simulate a call</h4>
      <div className="tp-form">
        <label>Server<input aria-label="Simulate server" value={server} placeholder="a server name, or boss" onChange={(e) => setServer(e.target.value)} /></label>
        <label>Tool<input aria-label="Simulate tool" value={tool} onChange={(e) => setTool(e.target.value)} /></label>
        <label>Agent<input aria-label="Simulate agent" value={role} placeholder="optional role key" onChange={(e) => setRole(e.target.value)} /></label>
        <label className="tp-wide">Arguments (JSON)<textarea aria-label="Simulate arguments" rows={2} value={args} onChange={(e) => setArgs(e.target.value)} /></label>
        <GatedButton permission="mcp_server:read" projectId={projectId} className="kit-btn" data-testid="tool-rules-simulate-run"
                     disabled={!server.trim() || !tool.trim()} onClick={() => void run()}>Simulate</GatedButton>
      </div>
      {err ? <p className="tp-said">{err}</p> : null}
      {out ? (
        <p className="tp-sim-out" data-testid="tool-rules-simulate-out">
          <span className={`tp-eff tp-eff-${out.decision === "deny" ? "deny" : out.decision}`}>{out.decision.toUpperCase()}</span>{" "}
          {out.explanation}
        </p>
      ) : null}
    </section>
  );
}
