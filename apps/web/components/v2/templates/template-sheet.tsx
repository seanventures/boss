"use client";

/**
 * A TEMPLATE'S SIDE SHEET (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.8): what it adds · its secrets
 * (each `Test mode` / `Needs a key` / `Set`, with an inline write-only field) · what the app will
 * talk to · the security note (folded) · Start.
 *
 * START IS A GOVERNED BUILD (§2.3): POST /v1/templates/{id}/apply, the same start path as the
 * composer's. What it can answer, and what the sheet does:
 *   201                      → onStarted(workflow run id)
 *   409 consent_required     → the build consent sheet (withConsent), then the same apply again
 *   409 secrets_needed       → the test keys typed here are stored write-only under the app's
 *                              key (step-up, app_secret:write), then the same apply again
 *   409 + readiness          → the checklist with "Start anyway", as the composer draws it
 *   409 stack_mismatch       → the app's own stack is chosen, said in words
 *   anything else            → the API's sentence, never a code
 * A value typed into a secret field is never shown back: the field clears once it is stored.
 */
import React, { useEffect, useMemo, useState } from "react";

import { GatedButton, useGate } from "../../gated";
import { useGuardedCall } from "../../identity/identity-provider";
import { withConsent } from "../../../lib/identity/consent";
import { IdentityError } from "../../../lib/identity/api";
import {
  INSECURE, refusalCode, refusalWords, secretWord, stackLabel, templatesApi, testedWord,
  type TemplateCard, type TemplateDetail,
} from "../../../lib/templates";
import { ReadinessPanel, asReadiness, unaccepted, type Readiness } from "../../../app/workflow-readiness-ui";
import { LabelChips, stackTip, useInstalledStacks } from "./gallery";
import { SideSheet } from "./side-sheet";
import { consoleHref } from "../../../app/console-href";

/** Where the template goes: a new app (named here), or an app that exists. */
export type ApplyTarget =
  | { kind: "new" }
  | { kind: "add"; runId: string; appKey?: string | null; name: string;
      /** The app's stack when its templates say it; unknown, the person picks (never a guess). */
      stack?: string | null };

/** What stops Start, said as the thing to do ("" when nothing does). */
export function applyBlocker(o: {
  target: ApplyTarget; name: string; stack: string; egress: string[]; egressOk: boolean;
  needs: string[]; typed: Record<string, string>; stackOff: boolean;
}): string {
  if (o.target.kind === "new" && !o.name.trim()) return "Name the new app.";
  if (!o.stack) return o.target.kind === "add" ? "Choose the app's stack." : "Choose a stack.";
  if (o.stackOff) return `${stackLabel(o.stack)} is off on this install.`;
  if (o.egress.length && !o.egressOk) return "Allow the hosts it talks to.";
  const missing = o.needs.filter((n) => !(o.typed[n] ?? "").trim());
  if (missing.length) return `Paste a test key for ${missing.join(", ")}.`;
  return "";
}

export function TemplateSheet({ projectId, card, target, onStarted, onClose }: {
  projectId: string;
  card: TemplateCard;
  target: ApplyTarget;
  onStarted: (workflowRunId: string | null) => void;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<TemplateDetail | null>(null);
  const [loadError, setLoadError] = useState("");
  const installed = useInstalledStacks(projectId);
  const [stack, setStack] = useState<string>("");
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [egressOk, setEgressOk] = useState(false);
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [stored, setStored] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  const guarded = useGuardedCall();
  const startGate = useGate("workflow_run:start", projectId);
  const secretGate = useGate("app_secret:write", projectId);

  useEffect(() => {
    let alive = true;
    templatesApi.describe(card.id).then((d) => { if (alive) setDetail(d); })
      .catch((e) => { if (alive) setLoadError(refusalWords(e)); });
    return () => { alive = false; };
  }, [card.id]);

  // The first stack this install can build — chosen again when the toolchains arrive, until the
  // person picks one themselves.
  const [picked, setPicked] = useState(false);
  useEffect(() => {
    if (picked) return;
    // INTO AN APP, THE APP'S STACK: a Java template applied to a Node app because Java was first
    // in the list (walk). Its stack when known; else none, and the person picks.
    if (target.kind === "add") { setStack(target.stack && card.stacks.includes(target.stack) ? target.stack : ""); return; }
    setStack(card.stacks.find((s) => !installed || installed.has(s)) ?? card.stacks[0] ?? "");
  }, [card.stacks, installed, picked, target]);

  const secrets = detail?.secrets ?? card.secrets;
  const egress = detail?.egress ?? card.egress;
  /** The secrets a build cannot start without: no test default, and not stored yet here. */
  const needs = useMemo(() => secrets.filter((s) => s.testDefault === "none" && !stored.has(s.name)).map((s) => s.name), [secrets, stored]);
  const insecure = card.labels.includes(INSECURE);
  const stackOff = !!installed && !!stack && !installed.has(stack);
  // A new app has no keys yet, so its test keys are asked for up front; an existing app may
  // hold them already, and the API says which it still needs (secrets_needed).
  const blocker = applyBlocker({ target, name, stack, egress, egressOk, needs: target.kind === "new" ? needs : [], typed, stackOff })
    || (unaccepted(refusal, startAnyway).length ? "Not ready here yet: see the checklist." : "");

  const body = () => ({
    project_id: projectId, stack,
    ...(target.kind === "new" ? { new_app: { name: name.trim() } } : { extends_run_id: target.runId }),
    ...(note.trim() ? { note: note.trim() } : {}),
    ...(egress.length ? { accept_egress: egress } : {}),
    ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
  });

  /** Store the typed test keys for `names` under the app's key, build and preview, write-only. */
  async function storeKeys(appKey: string, names: string[]): Promise<boolean> {
    const missing = names.filter((n) => !(typed[n] ?? "").trim());
    if (missing.length) { setError(`Paste a test key for ${missing.join(", ")}, then Start again.`); return false; }
    if (!secretGate.allowed) { setError(secretGate.reason); return false; }
    for (const n of names) {
      for (const env of ["build", "preview"]) {
        await guarded(`Store the test key ${n}.`, () => templatesApi.putSecret(projectId, appKey, n, {
          env, mode: "test", value: typed[n].trim(), template_id: card.id }));
      }
    }
    setStored((s) => new Set([...s, ...names]));
    setTyped((t) => Object.fromEntries(Object.entries(t).filter(([k]) => !names.includes(k))));
    return true;
  }

  async function start() {
    if (busy || blocker) return;
    if (!startGate.allowed) { setError(startGate.reason); return; }
    setBusy(true);
    setError("");
    const apply = () => withConsent(() => templatesApi.apply(card.id, body()));
    try {
      let started;
      try {
        started = await apply();
      } catch (e) {
        if (refusalCode(e) !== "secrets_needed") throw e;
        const b = (e as IdentityError).body;
        const appKey = String(b.app_key ?? (target.kind === "add" ? target.appKey ?? "" : ""));
        const names = Array.isArray(b.secrets) ? b.secrets.map(String) : needs;
        if (!appKey || !(await storeKeys(appKey, names))) return;
        started = await apply();
      }
      if (started?.lab?.project_id) {
        // AN INSECURE TEMPLATE IS BUILT IN ITS OWN LAB PROJECT: open its run THERE. Opening it in
        // this project's Workflows found nothing, and the lab run was nowhere to be seen.
        window.location.assign(labRunHref(started.lab.project_id, started.workflow_run_id ?? null));
        return;
      }
      onStarted(started?.workflow_run_id ?? null);
    } catch (e) {
      const code = refusalCode(e);
      const b = e instanceof IdentityError ? e.body : {};
      const detail = (b.detail && typeof b.detail === "object" ? b.detail : {}) as Record<string, unknown>;
      const readiness = asReadiness(b.readiness ?? detail.readiness);
      // The checklist says what is missing, with "Start anyway"; the footer says to look at it.
      if (readiness) {
        setRefusal(readiness);
        window.setTimeout(() => document.querySelector("[data-testid=tpl-readiness]")?.scrollIntoView({ block: "nearest" }), 0);
        return;
      }
      if (code === "stack_mismatch" && Array.isArray(b.app_stacks)) {
        const fit = (b.app_stacks as string[]).find((s) => card.stacks.includes(s));
        if (fit) { setStack(fit); setPicked(true); }
        setError(fit ? `${refusalWords(e)}. ${stackLabel(fit)} is chosen now: press Start again.` : refusalWords(e));
        return;
      }
      if (e instanceof IdentityError && e.code === "step_up_cancelled") { setError("Not stored: you cancelled the check."); return; }
      setError(refusalWords(e));
    } finally {
      setBusy(false);
    }
  }

  const trial = detail?.trial;
  const foot = (
    <>
      {blocker ? <span className="tpl-why" role="status" data-testid="tpl-blocked">{blocker}</span> : <span className="tpl-push" />}
      <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
      <GatedButton permission="workflow_run:start" projectId={projectId} type="button" className="ag-btn ag-btn-primary"
                   data-testid="tpl-start" disabled={busy || !!blocker} aria-busy={busy || undefined}
                   title={target.kind === "new" ? "A governed build: tests, review and a PR, like any other" : `Build it into ${target.name}, on its branch`}
                   onClick={() => void start()}>
        {busy ? "Starting…" : "Start"}
      </GatedButton>
    </>
  );

  return (
    <SideSheet title={card.name} sub={`v${card.version}${card.source === "git" ? " · imported" : ""}`} onClose={onClose}
               testId="tpl-sheet" foot={foot}>
      <p className="tpl-muted" data-user-text={card.source === "git" || undefined}>{card.summary}</p>
      <LabelChips labels={card.labels} />
      {insecure ? (
        <p className="tpl-warn" data-testid="tpl-insecure-note">
          Deliberately vulnerable, for security practice. It goes into a new app only, and BOSS refuses to deploy it.
        </p>
      ) : null}
      {loadError ? <p className="tpl-err" role="alert">{loadError}</p> : null}

      <section className="tpl-sec" aria-label="Where">
        <h3>{target.kind === "new" ? "New app" : "Into"}</h3>
        {target.kind === "new" ? (
          <label className="tpl-field"><span>Name</span>
            <input value={name} maxLength={80} placeholder={card.name} data-testid="tpl-app-name" data-autofocus
                   onChange={(e) => setName(e.target.value)} />
          </label>
        ) : <span className="tpl-row-name" data-user-text>{target.name}</span>}
        <span className="tpl-chips" role="radiogroup" aria-label="Stack">
          <span className="tpl-stack-label" aria-hidden>Stack</span>
          {card.stacks.map((s) => {
            const off = !!installed && !installed.has(s);
            return (
              <button key={s} type="button" role="radio" className="tpl-chip" aria-checked={stack === s}
                      aria-disabled={off || undefined} data-off={off || undefined} title={stackTip(s, installed)}
                      data-testid={`tpl-stack-${s}`} onClick={() => { if (!off) { setStack(s); setPicked(true); } }}>
                {stackLabel(s)}{off ? " · off" : ""}
              </button>
            );
          })}
          {card.tested.length ? <span className="tpl-tested">{testedWord(card.tested)}</span> : null}
        </span>
      </section>

      <section className="tpl-sec" aria-label="What it adds">
        <h3>What it adds</h3>
        <span className="tpl-chips">{card.adds.map((a) => <span key={a} className="tpl-chip">{a.replace(/_/g, " ")}</span>)}</span>
        {detail?.routes.length ? (
          <ul className="tpl-tree" aria-label="Routes">{detail.routes.map((r) => <li key={r} data-user-text>{r}</li>)}</ul>
        ) : null}
        {detail?.requires.length ? <span className="tpl-faint">Needs first: {detail.requires.join(", ")}</span> : null}
      </section>

      <section className="tpl-sec" aria-label="Secrets" data-testid="tpl-secrets">
        <h3>Secrets</h3>
        {!secrets.length ? <span className="tpl-muted">None</span> : (
          <ul className="tpl-rows">
            {secrets.map((s) => {
              const w = stored.has(s.name) ? { word: "Set", tone: "ok" as const, tip: "Stored in the vault. It is never shown again." }
                : secretWord({ testDefault: s.testDefault });
              const field = s.testDefault === "none" && !stored.has(s.name);
              return (
                <li key={s.name} className="tpl-row" data-testid={`tpl-secret-${s.name}`}>
                  <span className="tpl-row-main">
                    <span className="tpl-row-name tpl-mono">{s.name}</span>
                    {s.purpose ? <span className="tpl-faint" data-user-text>{s.purpose}</span> : null}
                  </span>
                  <span className="tpl-chip" data-tone={w.tone === "warn" ? "amber" : "green"} title={w.tip}>{w.word}</span>
                  {field ? (
                    <span className="tpl-row-set">
                      <input className="tpl-input" type="password" autoComplete="off" spellCheck={false}
                             aria-label={`Test key for ${s.name}`} placeholder="Paste a test key"
                             value={typed[s.name] ?? ""} data-testid={`tpl-secret-input-${s.name}`}
                             title="Write-only: stored in the vault and never shown again. Live keys are set later, in App › Setup."
                             onChange={(e) => setTyped((t) => ({ ...t, [s.name]: e.target.value }))} />
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
        {needs.length ? <span className="tpl-faint">Test keys only here. Live keys are set later in the app&apos;s Setup.</span> : null}
      </section>

      <section className="tpl-sec" aria-label="Talks to" data-testid="tpl-egress">
        <h3 title="Hosts the running app calls">Talks to</h3>
        {!egress.length ? <span className="tpl-muted">Nothing outside BOSS</span> : (
          <>
            <span className="tpl-chips">{egress.map((h) => <span key={h} className="tpl-chip tpl-mono">{h}</span>)}</span>
            <label className="tpl-check">
              <input type="checkbox" checked={egressOk} data-testid="tpl-egress-ok" onChange={(e) => setEgressOk(e.target.checked)} />
              Let the app call {egress.length === 1 ? "this host" : "these hosts"}
            </label>
          </>
        )}
      </section>

      {detail?.securityNote ? (
        <details className="tpl-fold" data-testid="tpl-security-note">
          <summary>Security note</summary>
          <pre className="tpl-pre" data-user-text>{detail.securityNote}</pre>
        </details>
      ) : null}
      {trial && trial.status !== "passed" ? <span className="tpl-faint">Trial: {trial.status}</span> : null}

      <label className="tpl-field"><span>Note for the builder <em className="tpl-faint">optional</em></span>
        <textarea rows={2} maxLength={4000} value={note} data-testid="tpl-note" placeholder="Anything about your app it should know"
                  onChange={(e) => setNote(e.target.value)} />
      </label>

      {refusal ? (
        <div data-testid="tpl-readiness">
          <ReadinessPanel readiness={refusal} accepted={startAnyway} projectId={projectId} onAccept={setStartAnyway} />
        </div>
      ) : null}
      {error ? <p className="tpl-err" role="alert" data-testid="tpl-error" data-user-text>{error}</p> : null}
    </SideSheet>
  );
}

/** Where an apply that made a lab project goes next: that project's Workflows, on the run. */
export function labRunHref(labProjectId: string, workflowRunId: string | null): string {
  return consoleHref({ project_id: labProjectId, area: "delivery_workflows",
                       ...(workflowRunId ? { wfr: workflowRunId } : {}) });
}
