"use client";

/**
 * SETUP › EVENTS › RULES (P9a-7, wireframe E4): "Wake this agent when…". The rules of ONE
 * connection, checked top to bottom (first match wins): each is an agent's trigger of
 * `kind=intake`, listed by GET /v1/intake/rules (source, agent, enabled) and ordered by
 * priority. Move a rule up or down when the list is not narrowed, switch it on or off, open it.
 *
 * The editor is a SENTENCE over the filter language (lib/intake-rules.ts): When [a message
 * mentions @BOSS] in [#support] → [agent], acting for [the person], and [text doesn't start with
 * “ignore”]. While you edit, "Would match n of the last 50 events" is asked of the API
 * (preview-match, read-only) and drawn as a row of dots with a few of the events. Then the
 * guards, one line each: acting for, limits, a daily budget (a scoped budget on the rule) with
 * an estimate, quiet hours, and triage (off by default; on, a small model with a per-event and a
 * daily cap). Writes: create / update (agent:deploy), on/off (agent:lifecycle), budget
 * (budget:manage).
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Code, Plus, ShieldCheck, Trash, X } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { FormError } from "../../components/form-kit";
import { usd } from "../../lib/money";
import {
  IDENTITY_WORDS, createRule, deleteRule, envOf, parsePreview, parseRule, previewMatch, readCatalog, readRouteBudgets, readRules,
  removeRuleBudget, setRuleBudget, setRulePaused, updateRule, type Agent, type Connection, type Preview, type Rule, type RuleBody,
} from "../../lib/intake";
import {
  CHAT_KINDS, CHAT_WHEN, FIELDS, OPS, emptyDraft, fromMatch, limitDefaults, ruleWords, toMatch, typeWords, type Cond, type Draft, type Op,
} from "../../lib/intake-rules";
import { EvFrame, useConnPick, useEv } from "./frame";
import { AgentName, Chip, Empty, Face, ReadState, Said, SourceMark, goEvents, useRead } from "./parts";

type Editing = { rule: Rule | null; agent: string; name: string; draft: Draft; identity: string; perEntity: string; windowS: number; storm: string;
  budget: string; quiet: "answer" | "ack_and_defer" | "defer"; qStart: string; qEnd: string; qTz: string; triage: boolean; triageCap: string; json: string; showJson: boolean };

function editingOf(rule: Rule | null, chat: boolean, agents: Agent[], budget: number | null): Editing {
  const d = limitDefaults(chat);
  const draft = rule ? fromMatch(rule.match, rule.addressing, chat) : emptyDraft(chat);
  const q = rule?.quiet_hours;
  return {
    rule, agent: rule?.agent_key || agents[0]?.key || "", name: rule?.name || "", draft,
    identity: rule?.identity_mode || (chat ? "person" : "owner"),
    perEntity: String(rule?.limits.per_entity ?? d.per_entity), windowS: rule?.limits.per_entity_window_seconds ?? d.window,
    storm: String(rule?.limits.storm_per_min ?? 60), budget: budget === null ? "" : budget.toFixed(2),
    quiet: q?.mode === "defer" || q?.mode === "ack_and_defer" ? (q.mode as "defer" | "ack_and_defer") : "answer",
    qStart: q?.start || "22:00", qEnd: q?.end || "08:00", qTz: q?.timezone || browserZone(),
    triage: rule?.triage.mode === "model", triageCap: String(rule?.triage.daily_usd_cap ?? 5),
    json: JSON.stringify(toMatch(draft, chat).match ?? {}, null, 2), showJson: draft.custom !== null,
  };
}
function browserZone(): string { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } }

export function RulesTab() {
  const { projectId, actor, conns, agents } = useEv();
  const { list, picked } = useConnPick();
  const deploy = useGate("agent:deploy", projectId);
  const lifecycle = useGate("agent:lifecycle", projectId);
  const agentList = useMemo(() => (agents?.ok ? agents.data : []), [agents]);
  const chat = picked ? CHAT_KINDS.has(picked.kind) : false;
  const [agentFilter, setAgentFilter] = useState("");
  const [enabledFilter, setEnabledFilter] = useState<"" | "on" | "off">("");
  const narrowed = Boolean(agentFilter || enabledFilter);
  const rules = useRead<Rule[]>(() => (picked ? readRules(projectId, {
    source: picked.id, agent: agentFilter || undefined,
    enabled: enabledFilter === "" ? undefined : enabledFilter === "on",
  }) : Promise.resolve(null as never)), [projectId, picked?.id, agentFilter, enabledFilter]);
  const budgets = useRead(() => readRouteBudgets(projectId), [projectId]);
  const catalog = useRead(readCatalog, []);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" | "amber" } | null>(null);
  const items = rules.read?.ok ? rules.read.data : [];
  const budgetOf = (id: string) => (budgets.read?.ok ? budgets.read.data.find((b) => b.scope_id === id) ?? null : null);
  const source = (catalog.read?.ok ? catalog.read.data : []).find((s) => s.key === picked?.kind) ?? null;

  // Open the first rule (or a blank one) when the connection's rules arrive.
  useEffect(() => {
    if (!rules.read?.ok || !picked) return;
    setEditing((cur) => {
      if (cur && (cur.rule === null || items.some((r) => r.id === cur.rule?.id))) {
        const fresh = cur.rule ? items.find((r) => r.id === cur.rule?.id) ?? null : null;
        return fresh && fresh !== cur.rule ? { ...cur, rule: fresh } : cur;
      }
      const first = items[0] ?? null;
      return first ? editingOf(first, chat, agentList, budgetOf(first.id)?.cap_usd ?? null) : null;
    });
  }, [rules.read, picked?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const open = (r: Rule | null) => { setSaid(null); setEditing(editingOf(r, chat, agentList, r ? budgetOf(r.id)?.cap_usd ?? null : null)); };
  const move = async (r: Rule, dir: -1 | 1) => {
    const i = items.findIndex((x) => x.id === r.id);
    const other = items[i + dir];
    if (!other) return;
    const a = other.priority === r.priority ? r.priority + dir : other.priority;
    const b = r.priority;
    const one = await updateRule(projectId, actor, r.id, { priority: Math.max(0, a) });
    const two = one.ok ? await updateRule(projectId, actor, other.id, { priority: Math.max(0, b === a ? b - dir : b) }) : one;
    setSaid(one.ok && two.ok ? null : { text: (two.error || one.error) || "Not moved.", tone: "red" });
    rules.reload();
  };
  const flip = async (r: Rule) => {
    const res = await setRulePaused(projectId, actor, r.id, r.on);
    setSaid(res.ok ? { text: `${r.name || "The rule"} is ${r.on ? "off" : "on"}.`, tone: "green" } : { text: res.error || "Not changed.", tone: "red" });
    rules.reload();
  };

  const picker = list.length ? (
    <label className="ev-pick" aria-pressed="true" title="The connection these rules listen to">
      {picked ? <SourceMark kind={picked.kind} size="sm" /> : null}
      <select className="ev-select" value={picked?.id || ""} aria-label="Connection" data-testid="ev-rules-conn"
              onChange={(e) => { setEditing(null); goEvents({ conn: e.target.value }, "replace"); }}>
        {list.map((c) => <option key={c.id} value={c.id}>{c.label}{c.name && c.name !== c.label ? ` · ${c.name}` : ""}</option>)}
      </select>
      <select className="ev-select" value={agentFilter} aria-label="Agent" data-testid="ev-rules-agent"
              onChange={(e) => { setAgentFilter(e.target.value); setEditing(null); }}>
        <option value="">Any agent</option>
        {agentList.map((g) => <option key={g.key} value={g.key}>{g.name}</option>)}
      </select>
      <select className="ev-select" value={enabledFilter} aria-label="Enabled" data-testid="ev-rules-enabled"
              onChange={(e) => { setEnabledFilter(e.target.value as "" | "on" | "off"); setEditing(null); }}>
        <option value="">On or off</option>
        <option value="on">On</option>
        <option value="off">Off</option>
      </select>
    </label>
  ) : null;

  return (
    <EvFrame tab="rules" title="Wake this agent when…" sub="Rules for one connection, checked top to bottom."
             actions={<>{picker}{picked ? <GatedButton permission="agent:deploy" projectId={projectId} className="ev-btn" data-kind="primary" data-testid="ev-rule-new"
                                                     disabled={!agentList.length} title={agentList.length ? "A new rule at the bottom" : "Deploy an agent first"}
                                                     onClick={() => open(null)}><Plus size={15} aria-hidden />New rule</GatedButton> : null}</>}>
      {conns?.ok && !list.length ? (
        <Empty icon={<SourceMarkIcon />} title="Connect a source first" line="Rules listen to one connection: Slack, GitHub or a webhook." testId="ev-rules-noconn">
          <button type="button" className="ev-btn" data-kind="primary" onClick={() => goEvents({ events: "connections" })}>Connections</button>
        </Empty>
      ) : null}
      {agents?.ok && !agentList.length && list.length ? <Said said={{ text: "No agent is deployed yet. Deploy one (Agents › Deploy), then give it a rule here.", tone: "amber" }} /> : null}
      {editing ? null : <Said said={said} testId="ev-rules-said" />}
      {picked ? (
        <div className="ev-split" data-lead="aside">
          <section className="ev-card" aria-labelledby="ev-rl-h" data-testid="ev-rule-list">
            <h2 id="ev-rl-h" className="ev-sec-title" title="Rules are checked top to bottom; the first match wins.">{items.length} rule{items.length === 1 ? "" : "s"} · first match wins</h2>
            <ReadState read={rules.read} what="Rules" onRetry={rules.reload} testId="ev-rules" />
            {rules.read?.ok && !items.length ? <span className="ev-faint" data-testid="ev-rules-empty">No rule yet: nothing on {picked.label} wakes an agent.</span> : null}
            <div className="ev-rules">
              {items.map((r, i) => {
                const words = ruleWords(fromMatch(r.match, r.addressing, chat), chat);
                return (
                  <div key={r.id} className="ev-rule-row" data-testid={`ev-rule-${r.id}`}>
                    <span className="ev-rule-move">
                      <GatedButton permission="agent:deploy" projectId={projectId} disabled={narrowed || i === 0} aria-label={`Move ${r.name || words} up`} onClick={() => void move(r, -1)} data-testid={`ev-rule-up-${r.id}`}><ArrowUp size={12} /></GatedButton>
                      <GatedButton permission="agent:deploy" projectId={projectId} disabled={narrowed || i === items.length - 1} aria-label={`Move ${r.name || words} down`} onClick={() => void move(r, 1)} data-testid={`ev-rule-down-${r.id}`}><ArrowDown size={12} /></GatedButton>
                    </span>
                    <button type="button" className="ev-rule" aria-current={editing?.rule?.id === r.id} data-off={r.on ? undefined : ""} onClick={() => open(r)} data-testid={`ev-rule-open-${r.id}`}>
                      <span className="ev-rule-n">{i + 1}</span>
                      <Face name={r.agent_key} agent />
                      <span className="ev-rule-words"><span>{r.name || words}</span><span className="ev-faint">→ {agentList.find((g) => g.key === r.agent_key)?.name || r.agent_key}</span>
                        {r.digest_mode ? <Chip tone="violet" tip="A flood: one run every 5 minutes with a summary">Digest</Chip> : null}
                        {!r.match_ok ? <Chip tone="red" tip={r.match_error}>Filter broken</Chip> : null}</span>
                    </button>
                    <button type="button" role="switch" className="ev-switch" aria-checked={r.on} aria-label={r.name || words} data-testid={`ev-rule-switch-${r.id}`}
                            title={r.paused_reason || (r.on ? "On" : "Off")} {...lifecycle.props} onClick={lifecycle.guard(() => void flip(r))} />
                  </div>
                );
              })}
            </div>
          </section>
          {editing ? (
            <RuleEditor key={editing.rule?.id || "new"} start={editing} conn={picked} chat={chat} agents={agentList} rules={items}
                        eventTypes={source?.event_types ?? []} canEdit={deploy.allowed} budgetId={editing.rule ? budgetOf(editing.rule.id)?.id || "" : ""}
                        said={said}
                        onSaved={(words, rule, budget, partly) => { setSaid({ text: words, tone: partly ? "red" : "green" }); rules.reload(); budgets.reload(); if (rule) setEditing(editingOf(rule, chat, agentList, budget)); }}
                        onRemoved={(words) => { setSaid({ text: words, tone: "green" }); setEditing(null); rules.reload(); }}
                        onError={(t) => setSaid({ text: t, tone: "red" })} />
          ) : <aside className="ev-card ev-faint">Open a rule, or press New rule.</aside>}
        </div>
      ) : null}
    </EvFrame>
  );
}
function SourceMarkIcon() { return <ShieldCheck size={30} aria-hidden />; }

function RuleEditor({ start, conn, chat, agents, rules, eventTypes, canEdit, budgetId, said, onSaved, onRemoved, onError }: {
  start: Editing; conn: Connection; chat: boolean; agents: Agent[]; rules: Rule[]; eventTypes: string[]; canEdit: boolean; budgetId: string;
  said: { text: string; tone: "green" | "red" | "amber" } | null;
  onSaved: (words: string, rule: Rule | null, budget: number | null, partly: boolean) => void; onRemoved: (words: string) => void; onError: (t: string) => void;
}) {
  const { projectId, actor } = useEv();
  const preview = useGate("integration:read", projectId);
  const budgetGate = useGate("budget:manage", projectId);
  const [e, setE] = useState<Editing>(start);
  const [shown, setShown] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  const seq = useRef(0);
  const patch = (p: Partial<Editing>) => setE((cur) => ({ ...cur, ...p }));
  const draft = (p: Partial<Draft>) => setE((cur) => ({ ...cur, draft: { ...cur.draft, ...p } }));
  const rule = e.rule;
  const index = rule ? rules.findIndex((r) => r.id === rule.id) + 1 : rules.length + 1;
  const modes = rule?.identity_modes.length ? rule.identity_modes : conn.kind === "webhook" ? ["owner"] : ["person", "person_or_owner", "owner"];
  const agent = agents.find((g) => g.key === e.agent);

  let match: unknown = null;
  let jsonError = "";
  if (e.showJson) {
    try { match = JSON.parse(e.json || "null"); } catch { jsonError = "That is not valid JSON."; }
  } else match = toMatch(e.draft, chat).match;
  const addressing = e.showJson ? rule?.addressing : toMatch(e.draft, chat).addressing;
  const key = JSON.stringify(match);

  useEffect(() => {
    if (!preview.allowed || jsonError) return;
    const n = ++seq.current;
    const t = window.setTimeout(() => {
      void previewMatch(projectId, actor, e.agent || agents[0]?.key || "", conn.id, match).then((r) => {
        if (n !== seq.current) return;
        setShown(r.ok ? parsePreview(r.body) : { ok: false, error: r.error || "The preview failed.", matched: 0, considered: 0, events: [], people_redacted: false });
      });
    }, 400);
    return () => window.clearTimeout(t);
  }, [key, conn.id, preview.allowed]); // eslint-disable-line react-hooks/exhaustive-deps

  const conds = e.draft.conds;
  const setCond = (i: number, p: Partial<Cond>) => draft({ conds: conds.map((c, j) => (j === i ? { ...c, ...p } : c)) });
  const perDay = agent ? agent.cost_7d_usd / 7 : 0;

  const save = async () => {
    setBusy(true); setError("");
    const limits: Record<string, number> = {};
    const pe = Number(e.perEntity), st = Number(e.storm);
    if (Number.isInteger(pe) && pe > 0) { limits.per_entity = pe; limits.per_entity_window_seconds = e.windowS; }
    if (Number.isInteger(st) && st > 0) limits.storm_per_min = st;
    const others = rules.filter((r) => r.id !== rule?.id).map((r) => r.id);
    const cap = Number(e.triageCap);
    const config: Record<string, unknown> = {
      ...(rule?.config ?? {}), match, ...(chat && addressing ? { addressing } : {}),
      triage: e.triage ? { mode: "model", candidates: [...(rule ? [rule.id] : []), ...others, "none"], daily_usd_cap: cap > 0 ? cap : 5, max_usd_per_event: 0.002 } : { mode: "off" },
    };
    const body: RuleBody = {
      name: e.name.trim() || ruleWords(e.draft, chat).slice(0, 80), priority: rule?.priority ?? ((rules[rules.length - 1]?.priority ?? 90) + 10),
      identity_mode: e.identity, config, limits,
      quiet_hours: e.quiet === "answer" ? {} : { start: e.qStart, end: e.qEnd, timezone: e.qTz, mode: e.quiet },
    };
    const r = rule ? await updateRule(projectId, actor, rule.id, body) : await createRule(projectId, actor, e.agent, envOf(agent), { ...body, connection_id: conn.id });
    if (!r.ok) { setBusy(false); setError(r.error || "Not saved."); return; }
    const saved = parseRule(r.body);
    const id = saved.id || rule?.id || "";
    // Every step after the rule itself says when it failed: the rule is saved, the rest is not.
    const problems: string[] = [];
    if (!rule && e.triage && id) {
      // Triage may pick this rule too: its own id exists only now.
      const t = await updateRule(projectId, actor, id, { config: { ...config, triage: { ...(config.triage as Record<string, unknown>), candidates: [id, ...others, "none"] } } });
      if (!t.ok) problems.push(`triage cannot pick this rule yet (${t.error || "not saved"})`);
    }
    const want = Number(e.budget);
    let budget: number | null = start.budget.trim() ? Number(start.budget) : null;    // what is stored now
    if (budgetGate.allowed && id) {
      if (e.budget.trim() && want > 0) {
        const b = await setRuleBudget(projectId, id, want);
        if (b.ok) budget = want; else problems.push(`the daily budget was not set (${b.error || "refused"})`);
      } else if (!e.budget.trim() && budgetId) {
        const b = await removeRuleBudget(projectId, budgetId);
        if (b.ok) budget = null; else problems.push(`the daily budget was not removed (${b.error || "refused"})`);
      }
    }
    setBusy(false);
    onSaved(`${body.name} saved${rule ? "" : ` as rule ${index}`}${problems.length ? `, but ${problems.join("; ")}` : ""}.`, saved.id ? saved : null, budget, problems.length > 0);
  };
  const remove = async () => {
    if (!rule) return;
    setConfirm(false);
    const r = await deleteRule(projectId, actor, rule.id);
    if (r.ok) onRemoved(`${rule.name || "The rule"} removed.`); else onError(r.error || "Not removed.");
  };

  const hits = shown?.events.filter((x) => x.matched) ?? [];
  const misses = shown?.events.filter((x) => !x.matched) ?? [];
  const samples = [...hits.slice(0, 2), ...misses.slice(0, 1)];
  const typeOptions = eventTypes.length ? eventTypes : [];

  return (
    <section className="ev-card ev-form" aria-labelledby="ev-re-h" data-testid="ev-rule-editor">
      <div className="ev-row-line">
        <h2 id="ev-re-h" className="ev-h2">{rule ? `Rule ${index}` : "New rule"}</h2>
        {rule ? <Chip tone={rule.on ? "green" : "grey"} tip={rule.paused_reason || undefined}>{rule.on ? "On" : "Off"}</Chip> : null}
        <span className="ev-spacer" />
        {rule?.on_behalf_of ? <span className="ev-faint ev-who">owner: <Face name={rule.on_behalf_of} />{rule.on_behalf_of}</span> : null}
      </div>
      {e.showJson ? null : (
        <div className="ev-sentence" data-testid="ev-sentence">
          <span>When</span>
          {chat ? (
            <select value={e.draft.when} onChange={(x) => draft({ when: x.target.value })} aria-label="What happened" title="Pick what happened: a mention, a DM, any message, a thread follow-up" data-testid="ev-when">
              {CHAT_WHEN.map((w) => <option key={w.key} value={w.key} title={w.tip}>{w.word}</option>)}
            </select>
          ) : (
            <select value={e.draft.when} onChange={(x) => draft({ when: x.target.value })} aria-label="What happened" data-testid="ev-when">
              <option value="">anything arrives</option>
              {typeOptions.map((t) => <option key={t} value={t}>{typeWords(t).toLowerCase()}</option>)}
            </select>
          )}
          {chat && e.draft.when !== "dm" ? (
            <>
              <span>in</span>
              <input value={e.draft.channels.join(", ")} placeholder="any channel" aria-label="Channels" title="Channels this rule listens to (channel ids, comma-separated)"
                     onChange={(x) => draft({ channels: x.target.value.split(",").map((c) => c.trim()).filter((c, i, all) => c || i === all.length - 1) })} data-testid="ev-channels" />
            </>
          ) : null}
          <span>→</span>
          {rule ? <AgentName agentKey={rule.agent_key} name={agent?.name} /> : (
            <select value={e.agent} onChange={(x) => patch({ agent: x.target.value })} aria-label="Agent" data-testid="ev-agent">
              {agents.map((g) => <option key={g.key} value={g.key}>{g.name}</option>)}
            </select>
          )}
          <span>, acting for</span>
          <select value={e.identity} onChange={(x) => patch({ identity: x.target.value })} aria-label="Acting for" title="Whose permissions the agent uses" data-testid="ev-identity">
            {modes.map((m) => <option key={m} value={m} title={IDENTITY_WORDS[m]?.tip}>{(IDENTITY_WORDS[m]?.word || m).toLowerCase()}</option>)}
          </select>
        </div>
      )}
      {e.showJson ? null : (
        <div className="ev-form" data-testid="ev-conds">
          {conds.map((c, i) => (
            <div key={i} className="ev-cond" data-testid={`ev-cond-${i}`}>
              <span>and</span>
              <select value={c.field} onChange={(x) => setCond(i, { field: x.target.value })} aria-label="Field">
                {FIELDS.filter((f) => f.chat === chat || f.field === c.field || !chat).map((f) => <option key={f.field} value={f.field}>{f.word}</option>)}
              </select>
              <select value={`${c.not ? "!" : ""}${c.op}`} onChange={(x) => setCond(i, { not: x.target.value.startsWith("!"), op: x.target.value.replace("!", "") as Op })} aria-label="How">
                {OPS.flatMap((o) => [<option key={o.op} value={o.op}>{o.word}</option>, <option key={`!${o.op}`} value={`!${o.op}`}>{o.not}</option>])}
              </select>
              {c.op === "exists" ? null : <input value={c.value} onChange={(x) => setCond(i, { value: x.target.value })} aria-label="Value" data-testid={`ev-cond-value-${i}`} />}
              <button type="button" className="ev-link" aria-label="Remove condition" onClick={() => draft({ conds: conds.filter((_, j) => j !== i) })}><X size={13} /></button>
            </div>
          ))}
          <div className="ev-row-line">
            <button type="button" className="ev-link" onClick={() => draft({ conds: [...conds, { field: chat ? "data.text" : "subject", op: "contains", value: "", not: false }] })} data-testid="ev-cond-add">
              <Plus size={13} aria-hidden /> Condition</button>
            {chat && (e.draft.when === "mention" || e.draft.when === "dm") ? (
              <span className="ev-row-line" title="A reply in a thread the agent already owns continues it, no new @mention needed">
                <button type="button" role="switch" className="ev-switch" aria-checked={e.draft.followups} aria-label="Follow-ups in its threads"
                        onClick={() => draft({ followups: !e.draft.followups })} data-testid="ev-followups" />
                <span className="ev-faint">Follow-ups in its threads: {e.draft.followups ? "on" : "off"}</span>
              </span>
            ) : null}
            <span className="ev-spacer" />
            <button type="button" className="ev-link" onClick={() => patch({ showJson: !e.showJson, json: JSON.stringify(match ?? {}, null, 2) })} data-testid="ev-json-toggle">
              <Code size={13} aria-hidden /> Edit as JSON</button>
          </div>
        </div>
      )}
      {e.showJson ? (
        <div className="ev-form">
          <span className="ev-faint">{e.draft.custom !== null ? "This rule's filter is more than the sentence can say, so it is shown as it is." : "The filter in the route language (all / any / not, field · op · value)."}</span>
          <textarea className="textarea ev-json" value={e.json} onChange={(x) => patch({ json: x.target.value })} aria-label="Filter as JSON" data-testid="ev-json" />
          {jsonError ? <span className="ev-dl-why">{jsonError}</span> : null}
          {e.draft.custom === null ? <div><button type="button" className="ev-link" onClick={() => patch({ showJson: false })}>Back to the sentence</button></div> : null}
        </div>
      ) : null}

      <div className="ev-match" data-testid="ev-would-match" aria-live="polite">
        {!preview.allowed ? <span className="ev-faint">{preview.reason}</span>
          : !shown ? <span className="ev-faint">Checking the last 50 events…</span>
          : !shown.ok ? <span className="ev-dl-why" data-testid="ev-match-error">{shown.error}</span> : (
          <>
            <span className="ev-row-line"><b data-testid="ev-match-count">Would match {shown.matched} of the last {shown.considered} event{shown.considered === 1 ? "" : "s"}</b><Chip tone="green">live</Chip></span>
            <div className="ev-dots" aria-label={`${shown.matched} of ${shown.considered} recent events match`} role="img">
              {shown.events.map((x) => <span key={x.id} className="ev-dot" data-hit={x.matched ? "" : undefined} title={x.matched ? "Would match" : "No match"} />)}
            </div>
            {samples.map((x) => (
              <span key={x.id} className="ev-sample" data-hit={x.matched ? "" : undefined}>
                {x.actor ? <Face name={x.actor} /> : <SourceMark kind={conn.kind} size="sm" />}
                <span className="ev-sample-text">{x.text || x.subject || x.type}</span>
                <Chip tone={x.matched ? "green" : "grey"}>{x.matched ? "match" : "no"}</Chip>
              </span>
            ))}
            <span className="ev-faint" data-testid="ev-match-credentials-hidden"
              title="URL credentials are hidden in this preview for every role, so a rule on a password or a token may match a live event the preview did not.">
              Credentials hidden in this preview
            </span>
            {shown.people_redacted ? (
              <span className="ev-faint" data-testid="ev-match-people-hidden"
                title="People's emails and BOSS accounts were hidden from this preview (seeing them needs “View people”: Owner, Admin or Auditor). A rule on those fields may match live events the preview did not.">
                People hidden · live results may differ
              </span>
            ) : null}
            {!shown.considered ? <span className="ev-faint">No events on {conn.label} yet; the count fills in as they arrive.</span> : null}
          </>
        )}
      </div>

      <div>
        <div className="ev-guard">
          <span className="ev-guard-label">Acting for</span>
          <div className="ev-seg" role="radiogroup" aria-label="Acting for">
            {modes.map((m) => <button key={m} type="button" role="radio" aria-checked={e.identity === m} title={IDENTITY_WORDS[m]?.tip} onClick={() => patch({ identity: m })} data-testid={`ev-mode-${m}`}>{IDENTITY_WORDS[m]?.word || m}</button>)}
          </div>
        </div>
        <div className="ev-guard">
          <span className="ev-guard-label" title="Past the limit, BOSS holds the event and tells the owner once">Limits</span>
          <div className="ev-guard-body">
            <input type="number" min={1} value={e.perEntity} onChange={(x) => patch({ perEntity: x.target.value })} aria-label="Runs" data-testid="ev-limit" />
            <span>per {chat ? "thread" : "record"} per</span>
            <select value={e.windowS} onChange={(x) => patch({ windowS: Number(x.target.value) })} aria-label="Window"><option value={3600}>hour</option><option value={86400}>day</option></select>
            <span className="ev-faint" title="More than this a minute and the rule runs once every 5 minutes with a summary">· storm at</span>
            <input type="number" min={1} value={e.storm} onChange={(x) => patch({ storm: x.target.value })} aria-label="Storm per minute" /><span className="ev-faint">/min</span>
          </div>
        </div>
        <div className="ev-guard">
          <span className="ev-guard-label" title="At the cap the rule pauses. People see: This assistant has reached today's budget.">Daily budget</span>
          <div className="ev-guard-body">
            <span>$</span>
            <input type="number" min={0} step="0.5" value={e.budget} placeholder="none" onChange={(x) => patch({ budget: x.target.value })} aria-label="Daily budget in dollars"
                   disabled={!budgetGate.allowed} title={budgetGate.allowed ? undefined : budgetGate.reason} data-testid="ev-budget" />
            <span className="ev-faint" title="From this agent's spend over the last 7 days">≈ {usd(perDay)}/day{agent ? ` · ${agent.name}, last 7 days` : ""}</span>
          </div>
        </div>
        <div className="ev-guard">
          <span className="ev-guard-label" title="Conversations a person starts are always answered. Record events can wait until morning.">Quiet hours</span>
          <div className="ev-guard-body">
            <div className="ev-seg" role="radiogroup" aria-label="Quiet hours">
              {([["answer", "Always answer"], ["ack_and_defer", "Say “later”"], ["defer", "Wait"]] as const).map(([k, w]) => (
                <button key={k} type="button" role="radio" aria-checked={e.quiet === k} onClick={() => patch({ quiet: k })} data-testid={`ev-quiet-${k}`}
                        title={k === "answer" ? "No quiet hours" : k === "ack_and_defer" ? "The person hears “I'll pick this up at 08:00”" : "Held until quiet hours end"}>{w}</button>
              ))}
            </div>
            {e.quiet === "answer" ? <span className="ev-faint">{chat ? "Always answer people" : "Runs any time"}</span> : (
              <>
                <input type="time" value={e.qStart} onChange={(x) => patch({ qStart: x.target.value })} aria-label="Quiet from" />
                <span>to</span>
                <input type="time" value={e.qEnd} onChange={(x) => patch({ qEnd: x.target.value })} aria-label="Quiet until" />
                <input type="text" value={e.qTz} onChange={(x) => patch({ qTz: x.target.value })} aria-label="Time zone" />
              </>
            )}
          </div>
        </div>
        <div className="ev-guard">
          <span className="ev-guard-label" title="A cheap model picks the agent for messy inputs, like a busy support channel. Off by default.">Triage</span>
          <div className="ev-guard-body">
            <button type="button" role="switch" className="ev-switch" aria-checked={e.triage} aria-label="Triage" onClick={() => patch({ triage: !e.triage })} data-testid="ev-triage" />
            {e.triage ? (
              <>
                <span>Small model, $0.002/event, cap $</span>
                <input type="number" min={0} step="0.5" value={e.triageCap} onChange={(x) => patch({ triageCap: x.target.value })} aria-label="Triage daily cap" data-testid="ev-triage-cap" />
                <span>/day</span>
              </>
            ) : <span className="ev-faint">Off · rules are enough <span title="When on: small model, $0.002/event, $5/day cap">(when on: small model, $0.002/event, $5/day cap)</span></span>}
          </div>
        </div>
        <div className="ev-guard">
          <span className="ev-guard-label">Name</span>
          <div className="ev-guard-body"><input type="text" value={e.name} placeholder={ruleWords(e.draft, chat)} onChange={(x) => patch({ name: x.target.value })} aria-label="Rule name" data-testid="ev-rule-name" /></div>
        </div>
      </div>
      <span className="ev-faint ev-row-line"><ShieldCheck size={14} aria-hidden /> Loop guard on · own bot ignored</span>
      <FormError>{error}</FormError>
      <Said said={said} testId="ev-rules-said" />
      <div className="ev-row-line">
        {rule ? (
          confirm ? (
            <>
              <span className="ev-faint">Remove this rule?</span>
              <GatedButton permission="agent:deploy" projectId={projectId} className="ev-btn" data-size="sm" data-kind="danger" onClick={() => void remove()} data-testid="ev-rule-remove-yes">Remove</GatedButton>
              <button type="button" className="ev-btn" data-size="sm" data-kind="ghost" onClick={() => setConfirm(false)}>Keep</button>
            </>
          ) : <GatedButton permission="agent:deploy" projectId={projectId} className="ev-btn" data-size="sm" data-kind="ghost" onClick={() => setConfirm(true)} data-testid="ev-rule-remove"><Trash size={14} aria-hidden /> Remove</GatedButton>
        ) : null}
        <span className="ev-spacer" />
        <GatedButton permission="agent:deploy" projectId={projectId} className="ev-btn" data-kind="primary" onClick={() => void save()}
                     disabled={busy || !canEdit || Boolean(jsonError) || (!rule && !e.agent)} data-testid="ev-rule-save">{busy ? "Saving…" : "Save rule"}</GatedButton>
      </div>
    </section>
  );
}

