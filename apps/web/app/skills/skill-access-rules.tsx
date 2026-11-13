"use client";

/**
 * SKILLS & MCP › RULES › SKILL ACCESS (BOSSAPP-890): the org's and project's rules over who may
 * create, edit, import, install, test, remove, hold or run which skills — in the order they are
 * read; the first match decides, and with no match the action is allowed. Disable/enable and
 * revoke per row, an Add rule form, and a simulator that answers without counting a hit.
 */
import React, { useCallback, useEffect, useState } from "react";

import { CheckGroup, CheckRow, Field, FormError, FormGrid } from "../../components/form-kit";
import { GatedButton } from "../../components/gated";
import {
  createSkillRule, parseSkillSimulation, readSkillRules, revokeSkillRule, ruleWords, simulateSkillRule,
  SKILL_ACTIONS, updateSkillRule, type SkillRule, type SkillSimulation,
} from "../../lib/skill-policies";

export function SkillAccessRules({ projectId }: { projectId: string }) {
  const [rules, setRules] = useState<SkillRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    const r = await readSkillRules(projectId);
    if (r.ok && "data" in r && r.data) { setRules(r.data); setError(null); }
    else if (!r.ok) setError(r.error === "missing" ? "This install has no skill-access rules yet." : r.error);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const act = async (p: Promise<{ ok: boolean; error: string | null }>, done: string) => {
    const r = await p;
    setSaid(r.ok ? done : r.error ?? "Not saved.");
    if (r.ok) void load();
  };

  return (
    <section className="tp-rules" aria-label="Skill access" data-testid="skill-access-rules">
      <div className="tp-head">
        <h4>Skill access</h4>
        <span className="kit-spacer" />
        <GatedButton permission="skill:manage" projectId={projectId} className="kit-btn"
                     data-testid="skill-access-add" onClick={() => setAdding((a) => !a)}>
          {adding ? "Close" : "Add rule"}
        </GatedButton>
      </div>
      {said ? <p className="tp-said" role="status" data-testid="skill-access-said">{said}</p> : null}
      {adding ? <AddSkillRule projectId={projectId} onAdded={() => { setAdding(false); setSaid("Rule added."); void load(); }} /> : null}
      {error && !rules ? <p className="tp-said" data-testid="skill-access-error">{error}</p> : null}
      {rules && rules.length === 0 ? (
        <p className="tp-muted" data-testid="skill-access-empty">No rules: anyone who may act on a skill today still may.</p>
      ) : null}
      {rules && rules.length ? (
        <div className="tp-table-wrap">
          <table className="tp-table" data-testid="skill-access-table">
            <thead><tr><th>#</th><th>Scope</th><th>Does</th><th>Actions</th><th>Skills</th><th>Who</th><th>Hits</th><th /></tr></thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id} data-testid={`skill-rule-${r.id}`} data-status={r.status} title={r.note ?? undefined}>
                  <td>{r.position}</td>
                  <td>{r.scope === "org" ? "Org" : "Project"}</td>
                  <td><span className={`tp-eff tp-eff-${r.effect}`}>{r.effect === "allow" ? "Allow" : "Deny"}</span></td>
                  <td className="tp-mono">{r.actions.join(", ")}</td>
                  <td className="tp-mono">{r.skill_pattern}</td>
                  <td>{ruleWords(r)}</td>
                  <td>{r.hit_count}</td>
                  <td className="tp-actions">
                    <GatedButton permission="skill:manage" projectId={projectId} className="kit-link"
                                 data-testid={`skill-rule-toggle-${r.id}`}
                                 onClick={() => void act(updateSkillRule(projectId, r.id, { status: r.status === "active" ? "disabled" : "active" },
                                                                         r.status === "active" ? "Disable" : "Enable"),
                                                         r.status === "active" ? "Rule disabled." : "Rule enabled.")}>
                      {r.status === "active" ? "Disable" : "Enable"}
                    </GatedButton>
                    <GatedButton permission="skill:manage" projectId={projectId} className="kit-link"
                                 data-testid={`skill-rule-revoke-${r.id}`}
                                 onClick={() => void act(revokeSkillRule(projectId, r.id), "Rule revoked.")}>Revoke</GatedButton>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <SimulateSkillAction projectId={projectId} />
    </section>
  );
}

function AddSkillRule({ projectId, onAdded }: { projectId: string; onAdded: () => void }) {
  const [effect, setEffect] = useState<"allow" | "deny">("deny");
  const [scope, setScope] = useState<"project" | "org">("project");
  const [position, setPosition] = useState("100");
  const [actions, setActions] = useState<string[]>(["import"]);
  const [skill, setSkill] = useState("*");
  const [role, setRole] = useState("*");
  const [kind, setKind] = useState<"any" | "person" | "agent">("any");
  const [source, setSource] = useState("*");
  const [tier, setTier] = useState("");
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const toggle = (a: string) => setActions((now) => now.includes(a) ? now.filter((x) => x !== a) : [...now, a]);
  const save = async () => {
    if (!actions.length) { setErr("Choose at least one action."); return; }
    const r = await createSkillRule(projectId, {
      effect, scope, position: Number(position) || 0, actions, skill_pattern: skill.trim() || "*",
      role_pattern: role.trim() || "*", actor_kind: kind, source_pattern: source.trim() || "*",
      min_risk_tier: tier === "" ? null : Number(tier), note: note.trim() || null,
    });
    if (r.ok) onAdded(); else setErr(r.error ?? "Not saved.");
  };
  return (
    <FormGrid label="Add a skill rule" testId="skill-access-form">
      <Field label="Does"><select aria-label="Skill rule effect" value={effect} onChange={(e) => setEffect(e.target.value as "allow" | "deny")}>
        <option value="deny">Deny</option><option value="allow">Allow</option></select></Field>
      <Field label="Scope"><select aria-label="Skill rule scope" value={scope} onChange={(e) => setScope(e.target.value as "project" | "org")}>
        <option value="project">This project</option><option value="org">The organization</option></select></Field>
      <Field label="Order"><input aria-label="Skill rule position" inputMode="numeric" value={position} onChange={(e) => setPosition(e.target.value)} /></Field>
      <CheckGroup label="Actions" tip="Which actions on a skill this rule decides">
        {SKILL_ACTIONS.map((a) => (
          <CheckRow key={a} label={a} checked={actions.includes(a)}
                    testId={`skill-access-action-${a}`} onChange={() => toggle(a)} />
        ))}
      </CheckGroup>
      <Field label="Skills"><input aria-label="Skill rule skills" value={skill} placeholder="deploy_* or a key" onChange={(e) => setSkill(e.target.value)} /></Field>
      <Field label="Role"><input aria-label="Skill rule role" value={role} placeholder="* or forge" onChange={(e) => setRole(e.target.value)} /></Field>
      <Field label="Done by"><select aria-label="Skill rule actor" value={kind} onChange={(e) => setKind(e.target.value as "any" | "person" | "agent")}>
        <option value="any">Anyone</option><option value="person">A person</option><option value="agent">An agent</option></select></Field>
      <Field label="From"><input aria-label="Skill rule source" value={source} placeholder="github:https://github.com/acme/*" onChange={(e) => setSource(e.target.value)} /></Field>
      <Field label="Tier at least"><select aria-label="Skill rule tier" value={tier} onChange={(e) => setTier(e.target.value)}>
        <option value="">Any</option>{[0, 1, 2, 3].map((t) => <option key={t} value={String(t)}>{t}</option>)}</select></Field>
      <Field label="Why (shown when it refuses)" wide><input aria-label="Skill rule note" value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      <FormError>{err}</FormError>
      <GatedButton permission="skill:manage" projectId={projectId} className="kit-btn" data-testid="skill-access-save"
                   onClick={() => void save()}>Save rule</GatedButton>
    </FormGrid>
  );
}

function SimulateSkillAction({ projectId }: { projectId: string }) {
  const [action, setAction] = useState<string>("import");
  const [skill, setSkill] = useState("");
  const [role, setRole] = useState("");
  const [kind, setKind] = useState<"person" | "agent">("person");
  const [source, setSource] = useState("");
  const [out, setOut] = useState<SkillSimulation | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const run = async () => {
    const r = await simulateSkillRule(projectId, { action, skill_key: skill.trim(), role_key: role.trim() || null,
                                                   actor_kind: kind, source: source.trim() || null });
    if (r.ok) { setOut(parseSkillSimulation(r.body)); setErr(null); } else { setOut(null); setErr(r.error); }
  };
  return (
    <section className="tp-sim" aria-label="Simulate a skill action" data-testid="skill-access-simulate">
      <h4>Simulate</h4>
      <div className="tp-form">
        <label>Action<select aria-label="Simulate skill action" value={action} onChange={(e) => setAction(e.target.value)}>
          {SKILL_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}</select></label>
        <label>Skill<input aria-label="Simulate skill key" value={skill} onChange={(e) => setSkill(e.target.value)} /></label>
        <label>Role<input aria-label="Simulate skill role" value={role} placeholder="optional" onChange={(e) => setRole(e.target.value)} /></label>
        <label>Done by<select aria-label="Simulate skill actor" value={kind} onChange={(e) => setKind(e.target.value as "person" | "agent")}>
          <option value="person">A person</option><option value="agent">An agent</option></select></label>
        <label>From<input aria-label="Simulate skill source" value={source} placeholder="optional" onChange={(e) => setSource(e.target.value)} /></label>
        <GatedButton permission="skill:read" projectId={projectId} className="kit-btn" data-testid="skill-access-simulate-run"
                     disabled={!skill.trim()} onClick={() => void run()}>Simulate</GatedButton>
      </div>
      {err ? <p className="tp-said">{err}</p> : null}
      {out ? (
        <p className="tp-sim-out" data-testid="skill-access-simulate-out">
          <span className={`tp-eff tp-eff-${out.decision}`}>{out.decision.toUpperCase()}</span> {out.explanation}
        </p>
      ) : null}
    </section>
  );
}
