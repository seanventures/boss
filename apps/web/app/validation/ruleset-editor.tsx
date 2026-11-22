"use client";

/**
 * RULES AND GUIDES (SPEC §4; wireframe D): Project › Settings › Validators. A BOSS ruleset
 * layered on the validator's own rulebook, kept in the repo (`.boss/rulesets/*.yaml`), so the
 * repo history is its version history. A person may switch a rule on or off, change its P-level,
 * set its auto-fix mode and edit the guide the fixing agent follows; saving never edits a hidden
 * setting — it OPENS A CHANGE ("Ruleset northgate v4") that BOSS merges, audited.
 *
 * Data (assumed, not named in the spec): GET /v1/projects/{p}/rulesets and
 * POST /v1/projects/{p}/rulesets/{id}/propose {rules, base_version, base_sha} → {change}.
 */
import React, { useEffect, useMemo, useState } from "react";
import { ArrowSquareOut, Check, Lock, Plus, Scales } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { pxWrite, usePxRead } from "../../lib/project-experience";
import {
  ADAPTERS, AUTOFIXES, isMissingRoute, NEEDS_LATEST, needsNewerEngine, needsSecondApprover, parseRulesets, parseValidatorsFile,
  proposeRulesetPath, proposeValidatorsPath, rulesetCreateBody, rulesetDiff, rulesetIdFor, rulesetsPath, rulesetTemplate, SEVS, shortSha,
  validatorsBody, validatorsPath, validatorTemplate,
  type Autofix, type Ruleset, type RulesetRule, type Sev, type ValidatorDecl,
} from "../../lib/validation-review";
import { RULESET_NORTHGATE } from "../../lib/validation-review-fixtures";
import { actorWords } from "../../lib/actor-words";
import { Ico } from "../projects/kit";
import "./validation-review.css";

const GUIDE_FOR: Partial<Record<Autofix, string>> = { mechanical: "(the validator's own --fix)" };

function RuleRow({ r, was, onChange, nextVersion }: { r: RulesetRule; was: RulesetRule | undefined; onChange: (r: RulesetRule) => void; nextVersion: number | null }) {
  const edited = !was || was.enabled !== r.enabled || was.severity !== r.severity || was.autofix !== r.autofix || was.guide !== r.guide;
  return (
    <tr data-off={r.enabled ? undefined : ""} data-edited={edited ? "" : undefined} data-testid="vr-rule" data-rule={r.id}>
      <td><button type="button" role="switch" className="vr-toggle" aria-checked={r.enabled} aria-label={`${r.enabled ? "Disable" : "Enable"} ${r.id}`}
                  onClick={() => onChange({ ...r, enabled: !r.enabled })} data-testid="vr-rule-toggle" /></td>
      <td>
        {r.custom && !was ? (
          <input className="vr-input" value={r.id} aria-label="Custom rule id" placeholder="custom-rule-id"
                 onChange={(e) => onChange({ ...r, id: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })} data-testid="vr-rule-id" />
        ) : <span className="vr-mono rid">{r.id}</span>}
        {r.custom ? <div style={{ marginTop: 4 }}><span className="vr-kind" data-kind="agent">custom · agent-checked</span></div> : null}
      </td>
      <td><select aria-label={`Level of ${r.id}`} value={r.severity} onChange={(e) => onChange({ ...r, severity: e.target.value as Sev })} data-testid="vr-rule-sev">
        {SEVS.map((s) => <option key={s} value={s}>{s}</option>)}</select></td>
      <td><select aria-label={`Auto-fix of ${r.id}`} value={r.autofix} onChange={(e) => onChange({ ...r, autofix: e.target.value as Autofix })} data-testid="vr-rule-fix">
        {AUTOFIXES.map((a) => <option key={a} value={a}>{a === "human" ? "needs you" : a}</option>)}</select></td>
      <td>
        {r.enabled ? (
          <textarea className="vr-input" rows={1} aria-label={`Guide for ${r.id}`} value={r.guide} placeholder={GUIDE_FOR[r.autofix] ?? "What the fixing agent should do"}
                    onChange={(e) => onChange({ ...r, guide: e.target.value })} data-testid="vr-rule-guide" />
        ) : <span className="vr-mono vr-tiny" style={{ color: "var(--v2-amber)" }}>disabled → findings shown as “suppressed”</span>}
        {edited ? <div className="vr-mono vr-tiny" style={{ color: "var(--v2-amber)", marginTop: 4 }}>edited · ships in v{nextVersion ?? "next"}</div> : null}
        {r.custom ? <div className="vr-mono vr-dim vr-tiny" style={{ marginTop: 4 }}>not executed by the validator · applied by an agent reviewer</div> : null}
      </td>
    </tr>
  );
}

export function RulesetEditor({ rs, projectId, testid = "vr-ruleset" }: { rs: Ruleset; projectId: string; testid?: string }) {
  const [rules, setRules] = useState<RulesetRule[]>(rs.rules);
  useEffect(() => { setRules(rs.rules); }, [rs]);
  const [said, setSaid] = useState<{ text: string; tone: string; href?: string } | null>(null);
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  // THE CHANGE A PROPOSE OPENED (dry run 2, gap 12: nothing on screen said change #15 opened).
  // Shown under the header, beside the button, until the rules are edited again.
  const [opened, setOpened] = useState<{ change: number | null; version: number | null; href: string; rules: string } | null>(null);
  const rulesKey = JSON.stringify(rules);
  const sameAsOpened = !!opened && opened.rules === rulesKey;
  const was = useMemo(() => Object.fromEntries(rs.rules.map((r) => [r.id, r])), [rs.rules]);
  const diff = rulesetDiff(rs, rules.filter((r) => r.id));
  const second = rules.filter((r) => was[r.id] && needsSecondApprover(was[r.id], r)).map((r) => r.id);
  const next = rs.version != null ? rs.version + 1 : null;
  const set = (i: number) => (r: RulesetRule) => setRules((list) => list.map((x, k) => (k === i ? r : x)));
  const propose = async () => {
    setBusy(true); setSaid(null);
    const r = await pxWrite(proposeRulesetPath(projectId, rs.id), {
      rules: rules.filter((x) => x.id).map(({ id, enabled, severity, autofix, guide, custom, check }) => ({ id, enabled, severity, autofix, guide, ...(custom ? { custom, check: check || "agent" } : {}) })),
      base_version: rs.version, base_sha: rs.sha,
    }, `Propose ruleset ${rs.id} v${next ?? ""}`);
    setBusy(false);
    if (!r.ok && isMissingRoute(r.status)) { setMissing(true); setSaid({ text: "Ruleset changes are not on this BOSS yet (the engine route is not deployed).", tone: "amber" }); return; }
    if (!r.ok) { setSaid({ text: r.error ?? "It did not open.", tone: "red" }); return; }
    if (r.demo) { setSaid({ text: "Sample mode — nothing was sent.", tone: "" }); return; }
    const ch = r.body && typeof r.body.change === "number" ? r.body.change : null;
    const version = r.body && typeof r.body.version === "number" ? r.body.version : next;
    setOpened({ change: ch, version, href: ch != null ? `/changes/${encodeURIComponent(projectId)}/${ch}` : "", rules: rulesKey });
  };
  const merged = rs.merged_at ? new Date(rs.merged_at) : null;
  return (
    <div className="vr" data-testid={testid}>
      <div className="vr-row" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0, fontSize: "0.84211rem", fontWeight: 600 }}>Ruleset {rs.id}</h3>
        <span className="vr-pill" data-testid={`${testid}-version`}>{[rs.version != null ? `v${rs.version}` : "", rs.sha ? shortSha(rs.sha) : "",
          merged && !Number.isNaN(merged.getTime()) ? `merged ${merged.toLocaleDateString([], { month: "short", day: "numeric" })}${rs.merged_by ? ` by ${actorWords(rs.merged_by)}` : ""}` : ""].filter(Boolean).join(" · ")}</span>
        {rs.validator ? <span className="vr-pill">validator <b>{rs.validator}</b></span> : null}
        <span className="vr-grow" />
        <span className="vr-mono vr-muted vr-small vr-hide-sm">{rs.path}</span>
        <GatedButton permission="policy:manage" projectId={projectId} className="vr-btn" data-kind="mint" disabled={busy || !diff.length || missing || sameAsOpened}
                     title={missing ? "Ruleset changes are not on this BOSS yet" : sameAsOpened ? "Already opened: edit a rule to propose again" : diff.length ? "Opens a change with the diff; it takes effect when merged" : "Change a rule first"}
                     onClick={() => void propose()} data-testid={`${testid}-propose`}>{busy ? "Opening…" : sameAsOpened && opened?.change != null ? `Opened as change #${opened.change}` : `Propose v${next ?? "next"} (opens a change)`}</GatedButton>
      </div>
      {opened ? (
        <div className="vr-opened" role="status" data-testid={`${testid}-opened`}>
          <Check size={14} aria-hidden />
          <span><b>Ruleset {opened.version != null ? `v${opened.version}` : rs.id} opened as {opened.change != null ? `change #${opened.change}` : "a change"}</b>
            <span className="vr-muted"> · takes effect when it merges</span></span>
          <span className="vr-grow" />
          {opened.href ? <a href={opened.href} data-testid={`${testid}-opened-link`}>Open change #{opened.change} <ArrowSquareOut size={11} aria-hidden /></a> : null}
        </div>
      ) : null}
      <div className="vr-scroll"><table className="vr-rules">
        <thead><tr><th>On</th><th>Rule</th><th>Level</th><th>Auto-fix</th><th>Guide the fixing agent follows</th></tr></thead>
        <tbody>{rules.map((r, i) => <RuleRow key={i} r={r} was={was[r.id]} onChange={set(i)} nextVersion={next} />)}</tbody>
      </table></div>
      <div className="vr-row" style={{ marginTop: 12 }}>
        <button type="button" className="vr-btn" onClick={() => setRules((l) => [...l, { id: "", enabled: true, severity: "P1", autofix: "agent", guide: "", custom: true, check: "agent" }])}
                data-testid={`${testid}-add`}><Plus size={12} aria-hidden />Add custom rule guide</button>
        <span className="vr-lock" data-testid={`${testid}-lock`} data-hot={second.length ? "" : undefined}><Lock size={12} aria-hidden />
          {second.length ? `${second.join(", ")}: lowering a P0 needs a second approver on the change` : "Lowering a P0, or allowing auto-fix on one, needs a second approver"}</span>
        <span className="vr-grow" />
        <span className="vr-muted vr-small">Who can change: <b style={{ color: "var(--v2-ink)" }}>policy:manage</b>{rs.rules_doc ? ` · why/fix text comes from ${rs.rules_doc}` : ""}</span>
      </div>
      {diff.length ? <p className="vr-small vr-muted" style={{ margin: "8px 0 0" }} data-testid={`${testid}-diff`}>{diff.length} rule{diff.length === 1 ? "" : "s"} changed: {diff.map((d) => `${d.id || "(new)"} (${d.fields.join(", ")})`).join(" · ")}</p> : null}
      {said ? <p className="vr-said" data-tone={said.tone || undefined} role="status" style={{ margin: "8px 0 0" }} data-testid={`${testid}-said`}>{said.text}
        {said.href ? <> <a href={said.href}>Open it <ArrowSquareOut size={11} aria-hidden /></a></> : null}</p> : null}
    </div>
  );
}

/**
 * NO RULESET OR NO validators.yaml YET: "Create ruleset" and "Add validator", each pre-filled with
 * a template, open a change that adds the file (the same propose route a ruleset edit uses). An
 * engine whose route cannot create files answers 404: the button then says it needs the latest
 * BOSS and stays disabled.
 */
export function CreateValidation({ projectId, hasRuleset, hasValidators = false, validatorsSha = null, rulesetId: existing = "", testid = "vr-create" }: {
  projectId: string; hasRuleset: boolean;
  /** `.boss/validators.yaml` already declares a validator (then only Create ruleset is offered). */
  hasValidators?: boolean; validatorsSha?: string | null; rulesetId?: string; testid?: string;
}) {
  const [mode, setMode] = useState<"" | "ruleset" | "validator">("");
  const [id, setId] = useState(existing || rulesetIdFor(projectId));
  const [val, setVal] = useState<ValidatorDecl>(() => validatorTemplate(existing || rulesetIdFor(projectId)));
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; tone: string; href?: string } | null>(null);
  const rules = useMemo(() => rulesetTemplate(), []);
  const send = async () => {
    if (busy || missing) return;
    setBusy(true); setSaid(null);
    const target = id;
    // Create ruleset: .boss/rulesets/<id>.yaml, layered on the validator. Add validator: the
    // .boss/validators.yaml entry, naming the ruleset.
    const r = mode === "validator"
      ? await pxWrite(proposeValidatorsPath(projectId), validatorsBody({ ...val, ruleset: target }, validatorsSha), "Add validator")
      : await pxWrite(proposeRulesetPath(projectId, target), rulesetCreateBody(rules, val.id), "Create ruleset");
    setBusy(false);
    if (!r.ok && needsNewerEngine(r.status, r.error)) { setMissing(true); setSaid({ text: NEEDS_LATEST, tone: "amber" }); return; }
    if (!r.ok) { setSaid({ text: r.error ?? "It did not open.", tone: "red" }); return; }
    if (r.demo) { setSaid({ text: "Sample mode — nothing was sent.", tone: "" }); return; }
    const ch = r.body && typeof r.body.change === "number" ? r.body.change : null;
    setSaid({ text: `Opened ${ch != null ? `change #${ch}` : "a change"} adding ${mode === "validator" ? ".boss/validators.yaml" : `.boss/rulesets/${target}.yaml`}. It takes effect when it merges.`, tone: "",
              href: ch != null ? `/changes/${encodeURIComponent(projectId)}/${ch}` : undefined });
  };
  const setV = (k: keyof ValidatorDecl) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setVal((v) => ({ ...v, [k]: k === "applies_to" ? e.target.value.split(",").map((x) => x.trim()).filter(Boolean) : e.target.value }));
  const offTip = missing ? NEEDS_LATEST : undefined;
  return (
    <div className="vr-create" data-testid={testid}>
      <div className="vr-row">
        {!hasRuleset ? (
          <GatedButton permission="policy:manage" projectId={projectId} className="vr-btn" data-kind={mode === "ruleset" ? "mint" : undefined} disabled={missing}
                       title={offTip ?? "A first ruleset: each rule's P-level, auto-fix and guide"} aria-expanded={mode === "ruleset"}
                       onClick={() => { setMode(mode === "ruleset" ? "" : "ruleset"); setSaid(null); }} data-testid={`${testid}-ruleset`}><Plus size={12} aria-hidden />Create ruleset</GatedButton>
        ) : null}
        {!hasValidators ? (
          <GatedButton permission="policy:manage" projectId={projectId} className="vr-btn" data-kind={mode === "validator" ? "mint" : undefined} disabled={missing}
                       title={offTip ?? "Declare the validator in .boss/validators.yaml"} aria-expanded={mode === "validator"}
                       onClick={() => { setMode(mode === "validator" ? "" : "validator"); setSaid(null); }} data-testid={`${testid}-validator`}><Plus size={12} aria-hidden />Add validator</GatedButton>
        ) : null}
      </div>
      {mode ? (
        <form className="vr-form vr-create-form" onSubmit={(e) => { e.preventDefault(); void send(); }} data-testid={`${testid}-form`}>
          {mode === "ruleset" || !hasRuleset ? (
            <label className="vr-muted vr-small">{mode === "ruleset" ? "Ruleset id" : "Checked against ruleset"} <span className="vr-dim">(.boss/rulesets/&lt;id&gt;.yaml)</span>
              <input value={id} onChange={(e) => { const x = e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"); setId(x); setVal((v) => ({ ...v, ruleset: x })); }} data-testid={`${testid}-id`} /></label>
          ) : null}
          {mode === "ruleset" ? (
            <div className="vr-scroll"><table className="vr-rules" data-testid={`${testid}-rules`}>
              <thead><tr><th>Rule</th><th>Level</th><th>Auto-fix</th></tr></thead>
              <tbody>{rules.map((r) => <tr key={r.id}><td className="vr-mono rid">{r.id}</td><td className="vr-mono">{r.severity}</td><td className="vr-mono">{r.autofix === "human" ? "needs you" : r.autofix}</td></tr>)}</tbody>
            </table></div>
          ) : null}
          {mode === "validator" ? (
            <div className="vr-create-grid" data-testid={`${testid}-validator-fields`}>
              <label className="vr-muted vr-small">Validator id<input value={val.id} onChange={setV("id")} data-testid={`${testid}-v-id`} /></label>
              <label className="vr-muted vr-small">Applies to <span className="vr-dim">(globs, comma separated)</span><input value={val.applies_to.join(", ")} onChange={setV("applies_to")} data-testid={`${testid}-v-applies`} /></label>
              <label className="vr-muted vr-small">Run<input value={val.run} onChange={setV("run")} data-testid={`${testid}-v-run`} /></label>
              <label className="vr-muted vr-small">Fix <span className="vr-dim">(optional · mechanical fixes)</span><input value={val.fix} onChange={setV("fix")} data-testid={`${testid}-v-fix`} /></label>
              <label className="vr-muted vr-small">Output adapter<select value={val.adapter} onChange={setV("adapter")} data-testid={`${testid}-v-adapter`}>{ADAPTERS.map((x) => <option key={x}>{x}</option>)}</select></label>
              <label className="vr-muted vr-small">Rules doc<input value={val.rules_doc} onChange={setV("rules_doc")} data-testid={`${testid}-v-doc`} /></label>
            </div>
          ) : null}
          <div className="vr-row"><span className="vr-dim vr-tiny">Opens a change with the file; it takes effect when it merges.</span><span className="vr-grow" />
            <button type="button" className="vr-btn" data-kind="ghost" onClick={() => setMode("")}>Cancel</button>
            <GatedButton permission="policy:manage" projectId={projectId} type="submit" className="vr-btn" data-kind="mint"
                         disabled={busy || missing || !id || (mode === "validator" && (!val.id || !val.run || !val.applies_to.length))}
                         title={offTip ?? "Opens a change adding the file"} data-testid={`${testid}-send`}>{busy ? "Opening…" : "Propose (opens a change)"}</GatedButton></div>
        </form>
      ) : null}
      {said ? <p className="vr-said" data-tone={said.tone || undefined} role={said.tone === "red" ? "alert" : "status"} style={{ margin: "8px 0 0" }} data-testid={`${testid}-said`}>{said.text}
        {said.href ? <> <a href={said.href}>Open it <ArrowSquareOut size={11} aria-hidden /></a></> : null}</p> : null}
    </div>
  );
}

/** Settings › Validators: every ruleset in the repo, or a clean empty state that offers to
 *  create the ruleset and the validator (each missing file gets its own button). */
export function ValidatorsCard({ projectId, demo = false }: { projectId: string; demo?: boolean }) {
  const read = usePxRead<Ruleset[]>(rulesetsPath(projectId), parseRulesets, { everyMs: 120_000, demo: () => parseRulesets([RULESET_NORTHGATE]) });
  // .boss/validators.yaml: null from an engine without the route (then Add validator is offered
  // and says what it needs when pressed).
  const vf = usePxRead(validatorsPath(projectId), parseValidatorsFile, { everyMs: 120_000, demo: () => ({ exists: true, sha: "d1", path: ".boss/validators.yaml", ids: ["gwlint"] }) });
  const list = read.data ?? [];
  const file = vf.data ?? null;
  const hasValidators = !!file?.exists && file.ids.length > 0;
  return (
    <section className="px-card" aria-labelledby="px-set-validators-h" data-testid="px-set-validators" id="px-set-validators">
      <div className="px-card-h">
        <Ico icon={Scales} c="violet" /><h2 id="px-set-validators-h">Validators · rules &amp; guides</h2>
      </div>
      {list.length ? (
        <>
          {list.map((rs) => <RulesetEditor key={rs.id} rs={rs} projectId={projectId} />)}
          {file && !hasValidators ? <div className="vr" style={{ marginTop: 12 }}><div className="vr-empty" data-testid="px-set-validators-none">No validator in .boss/validators.yaml yet.</div>
            <CreateValidation projectId={projectId} hasRuleset rulesetId={list[0].id} validatorsSha={file.sha} /></div> : null}
        </>
      ) : read.state === "loading" && !demo ? <div className="vr"><div className="vr-empty" role="status">Reading the rulesets…</div></div>
        : <div className="vr"><div className="vr-empty" data-testid="px-set-validators-empty"><Scales size={16} aria-hidden />
            No ruleset in this repository yet. Create one to set each rule&apos;s P-level, auto-fix and guide{hasValidators ? "" : ", and declare the validator that checks it"}.</div>
            <CreateValidation projectId={projectId} hasRuleset={false} hasValidators={hasValidators} validatorsSha={file?.sha ?? null} /></div>}
    </section>
  );
}
