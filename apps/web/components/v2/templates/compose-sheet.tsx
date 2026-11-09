"use client";

/**
 * SEVERAL CONNECTORS IN ONE BUILD: the compose sheet (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.3,
 * BOSSAPP-526). The composer opens it when two to six connectors are added (one still opens the
 * single connector's TemplateSheet, unchanged). It shows what the ONE build adds: the connectors
 * in the order they are wired, the stack they share, their secrets merged (a test key asked for
 * once), every host the app will talk to, accepted as a whole, the note, Start.
 *
 * START IS ONE GOVERNED BUILD: POST /v1/templates/compose. What it can answer, and what the sheet
 * does (never a silent failure, never a code):
 *   201                       → onStarted(workflow run id)
 *   409 consent_required      → the build consent sheet (withConsent), then the same compose again
 *   409 secrets_needed        → the typed test keys are stored write-only under the app's key
 *                               (step-up, app_secret:write), then the same compose again; a key
 *                               not typed yet is named, with its field
 *   409 connector_conflict    → EVERY conflict listed: what clashes, and between which connectors
 *   409 + readiness           → the checklist with "Start anyway", as the single sheet draws it
 *   anything else             → the API's sentence
 *
 * What the console can know before Start is said before Start, in the footer: no stack shared,
 * two connectors serving the same route (`:id` and `:order_id` are one route, as the server
 * reads them), a connector another needs that is neither added nor in the app.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";

import { GatedButton, useGate } from "../../gated";
import { useGuardedCall } from "../../identity/identity-provider";
import { withConsent } from "../../../lib/identity/consent";
import { IdentityError } from "../../../lib/identity/api";
import {
  composeConflicts, githubAsMeNotice, levelWord, refusalCode, refusalWords, secretWord, stackLabel, templatesApi,
  type BuildWorkflow, type ConnectorConflict, type GithubAsMe, type TemplateCard, type TemplateDetail, type TemplateSecret,
} from "../../../lib/templates";
import { ReadinessPanel, asReadiness, unaccepted, type Readiness } from "../../../app/workflow-readiness-ui";
import { sharedStacks } from "./catalogue";
import { stackTip, useInstalledStacks, useStackLevels } from "./gallery";
import { SideSheet } from "./side-sheet";
import { StackWarning, useStackCheck } from "./stack-warning";
import type { ApplyTarget } from "./template-sheet";

/** `GET /api/orders/:id` and `GET /api/orders/{order_id}` are the same route (compose.route_shape). */
export function routeShape(route: string): string {
  const [method, ...rest] = route.trim().split(" ");
  const path = rest.join(" ").replace(/\/+$/, "");
  const parts = path.split("/").map((seg) => (seg.startsWith(":") || (seg.startsWith("{") && seg.endsWith("}")) ? ":" : seg));
  return `${method.toUpperCase()} ${parts.join("/") || "/"}`;
}

/** The route clashes the console can see from the connectors' manifests, in the server's shape. */
export function routeClashes(details: Pick<TemplateDetail, "key" | "routes">[]): ConnectorConflict[] {
  const owners = new Map<string, { what: string; connectors: string[] }>();
  for (const d of details) {
    for (const r of d.routes) {
      const shape = routeShape(r);
      const o = owners.get(shape) ?? { what: r, connectors: [] };
      if (!o.connectors.includes(d.key)) o.connectors.push(d.key);
      owners.set(shape, o);
    }
  }
  return [...owners.values()].filter((o) => o.connectors.length > 1).map((o) => ({ kind: "route", ...o }));
}

/** A secret of the merged set: needs a key when NO connector declaring it has a test default
 *  (compose.merged_secrets takes the first default that is not `none`). `templateId`: the first
 *  connector declaring it, whose id the write-only PUT names. */
export type MergedSecret = TemplateSecret & { templateId: string; by: string[] };
export function mergedSecrets(cards: Pick<TemplateCard, "id" | "name" | "secrets">[]): MergedSecret[] {
  const out = new Map<string, MergedSecret>();
  for (const c of cards) {
    for (const s of c.secrets) {
      const have = out.get(s.name);
      if (!have) { out.set(s.name, { ...s, templateId: c.id, by: [c.name] }); continue; }
      have.by.push(c.name);
      if (have.testDefault === "none" && s.testDefault !== "none") have.testDefault = s.testDefault;
      if (!have.purpose && s.purpose) have.purpose = s.purpose;
    }
  }
  return [...out.values()];
}

/** Every host the connectors' app talks to, once each, in order. */
export function mergedEgress(cards: Pick<TemplateCard, "egress">[]): string[] {
  return [...new Set(cards.flatMap((c) => c.egress))];
}

/** The connectors another one needs that are neither in this build nor in the app. */
export function unmetRequires(details: Pick<TemplateDetail, "key" | "name" | "requires">[], inApp: string[]): string[] {
  const here = new Set([...details.map((d) => d.key), ...inApp]);
  return [...new Set(details.flatMap((d) => d.requires.filter((r) => !here.has(r))))];
}

/** What stops Start, said as the thing to do ("" when nothing does). */
export function composeBlocker(o: {
  target: ApplyTarget; name: string; stacks: string[]; stack: string; stackOff: boolean;
  clashes: ConnectorConflict[]; unmet: string[];
  egress: string[]; egressOk: boolean; needs: string[]; typed: Record<string, string>;
}): string {
  if (!o.stacks.length) return "These connectors share no stack: add them in separate builds.";
  if (o.clashes.length) return "Two connectors serve the same route: leave one out.";
  if (o.unmet.length) return `Add ${o.unmet.join(", ")} too, or first: a connector here needs it.`;
  if (o.target.kind === "new" && !o.name.trim()) return "Name the new app.";
  if (!o.stack) return o.target.kind === "add" ? "Choose the app's stack." : "Choose a stack.";
  if (!o.stacks.includes(o.stack)) return `Not every connector supports ${stackLabel(o.stack)}.`;
  if (o.stackOff) return `${stackLabel(o.stack)} is off on this install.`;
  if (o.egress.length && !o.egressOk) return "Allow the hosts they talk to.";
  const missing = o.needs.filter((n) => !(o.typed[n] ?? "").trim());
  if (missing.length) return `Paste a test key for ${missing.join(", ")}.`;
  return "";
}

/** A conflict as a person reads it: what clashes, and between which connectors. */
export function conflictLine(c: ConnectorConflict, names: Record<string, string> = {}): string {
  const who = c.connectors.map((k) => names[k] ?? k);
  const kind = c.kind === "route" ? "Same route" : c.kind === "webhook" ? "Same webhook" : c.kind === "file" ? "Same file"
    : c.kind === "env" ? "Setting" : c.kind === "secret" ? "Secret" : "Conflict";
  return `${kind}: ${c.what} (${who.join(" and ")})`;
}

export function ComposeSheet({ projectId, cards, target, inApp = [], onStarted, onClose,
                               initialName = "", initialNote = "", initialWorkflow = "", initialStack = "" }: {
  projectId: string;
  /** Two to six connectors, in the order picked. */
  cards: TemplateCard[];
  target: ApplyTarget;
  /** Template keys the app already carries (Add a feature): a `requires` they meet is met. */
  inApp?: string[];
  onStarted: (workflowRunId: string | null) => void;
  onClose: () => void;
  initialName?: string;
  initialNote?: string;
  initialWorkflow?: string;
  initialStack?: string;
}) {
  const [details, setDetails] = useState<TemplateDetail[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const installed = useInstalledStacks(projectId);
  const levels = useStackLevels();
  const [stack, setStack] = useState("");
  const [picked, setPicked] = useState(false);
  const [name, setName] = useState(initialName);
  const [note, setNote] = useState(initialNote);
  const [egressOk, setEgressOk] = useState(false);
  const [typed, setTyped] = useState<Record<string, string>>({});
  const [stored, setStored] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflicts, setConflicts] = useState<ConnectorConflict[]>([]);
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  const [workflows, setWorkflows] = useState<BuildWorkflow[]>([]);
  const [githubAsMe, setGithubAsMe] = useState<GithubAsMe | null>(null);
  const [workflowKey, setWorkflowKey] = useState(initialWorkflow);
  const conflictRef = useRef<HTMLDivElement>(null);
  const guarded = useGuardedCall();
  const startGate = useGate("workflow_run:start", projectId);
  const secretGate = useGate("app_secret:write", projectId);
  const ids = cards.map((c) => c.id).join(",");

  useEffect(() => {
    let alive = true;
    Promise.all(cards.map((c) => templatesApi.describe(c.id)))
      .then((d) => { if (alive) setDetails(d); })
      .catch((e) => { if (alive) setLoadError(refusalWords(e)); });
    return () => { alive = false; };
  }, [ids]);
  useEffect(() => {
    let alive = true;
    templatesApi.buildContext(projectId).then((c) => {
      if (alive) { setWorkflows(c.workflows); setGithubAsMe(c.githubAsMe); }
    }).catch(() => undefined);
    return () => { alive = false; };
  }, [projectId]);
  const defaultWorkflow = workflows.find((w) => w.isDefault);

  const stacks = useMemo(() => sharedStacks(cards), [cards]);
  useEffect(() => {
    if (picked) return;
    // INTO AN APP, THE APP'S STACK (never the first in a list); a new app, the composer's choice,
    // else the first shared stack this install builds.
    if (target.kind === "add") { setStack(target.stack && stacks.includes(target.stack) ? target.stack : ""); return; }
    const fits = (s: string) => !installed || installed.has(s);
    setStack(initialStack && stacks.includes(initialStack) && fits(initialStack) ? initialStack : stacks.find(fits) ?? stacks[0] ?? "");
  }, [stacks, installed, picked, target, initialStack]);

  const parts = details ?? cards;
  const secrets = useMemo(() => mergedSecrets(parts), [parts]);
  const egress = useMemo(() => mergedEgress(parts), [parts]);
  const needs = useMemo(() => secrets.filter((s) => s.testDefault === "none" && !stored.has(s.name)).map((s) => s.name), [secrets, stored]);
  const clashes = useMemo(() => (details ? routeClashes(details) : []), [details]);
  const unmet = useMemo(() => (details ? unmetRequires(details, inApp) : []), [details, inApp]);
  const names = useMemo(() => Object.fromEntries(cards.map((c) => [c.key, c.name])), [cards]);
  const stackOff = !!installed && !!stack && !installed.has(stack);
  const stackCheck = useStackCheck(projectId, note);
  const blocker = composeBlocker({ target, name, stacks, stack, stackOff, clashes, unmet, egress, egressOk,
                                   needs: target.kind === "new" ? needs : [], typed })
    || (stackCheck.blocks ? "BOSS cannot build the stack the note asks for." : "")
    || (unaccepted(refusal, startAnyway).length ? "Not ready here yet: see the checklist." : "");
  const shownConflicts = conflicts.length ? conflicts : clashes;

  const body = () => ({
    project_id: projectId, stack, templates: cards.map((c) => c.id),
    ...(target.kind === "new" ? { new_app: { name: name.trim() } } : { extends_run_id: target.runId }),
    ...(note.trim() ? { note: note.trim() } : {}),
    ...(workflowKey && workflowKey !== defaultWorkflow?.key ? { workflow_key: workflowKey } : {}),
    ...(egress.length ? { accept_egress: egress } : {}),
    ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
    ...(stackCheck.refused && stackCheck.allowed ? { allow_unsupported_stack: true } : {}),
  });

  async function storeKeys(appKey: string, wanted: string[]): Promise<boolean> {
    const missing = wanted.filter((n) => !(typed[n] ?? "").trim());
    if (missing.length) { setError(`Paste a test key for ${missing.join(", ")}, then Start again.`); return false; }
    if (!secretGate.allowed) { setError(secretGate.reason); return false; }
    for (const n of wanted) {
      const owner = secrets.find((s) => s.name === n)?.templateId ?? cards[0].id;
      await guarded(`Store the test key ${n}.`, () => templatesApi.putSecret(projectId, appKey, n, {
        envs: ["build", "preview"], mode: "test", value: typed[n].trim(), template_id: owner }));
    }
    setStored((s) => new Set([...s, ...wanted]));
    setTyped((t) => Object.fromEntries(Object.entries(t).filter(([k]) => !wanted.includes(k))));
    return true;
  }

  async function start() {
    if (busy || blocker) return;
    if (!startGate.allowed) { setError(startGate.reason); return; }
    setBusy(true);
    setError("");
    setConflicts([]);
    const compose = () => withConsent(() => templatesApi.compose(body()));
    try {
      let started;
      try {
        started = await compose();
      } catch (e) {
        if (refusalCode(e) !== "secrets_needed") throw e;
        const b = (e as IdentityError).body;
        const appKey = String(b.app_key ?? (target.kind === "add" ? target.appKey ?? "" : ""));
        const wanted = Array.isArray(b.secrets) ? b.secrets.map(String) : needs;
        if (!appKey || !(await storeKeys(appKey, wanted))) return;
        started = await compose();
      }
      onStarted(started?.workflowRunId ?? null);
    } catch (e) {
      const b = e instanceof IdentityError ? e.body : {};
      const detail = (b.detail && typeof b.detail === "object" ? b.detail : {}) as Record<string, unknown>;
      const readiness = asReadiness(b.readiness ?? detail.readiness);
      if (readiness) {
        setRefusal(readiness);
        window.setTimeout(() => document.querySelector("[data-testid=cmp-readiness]")?.scrollIntoView({ block: "nearest" }), 0);
        return;
      }
      const found = composeConflicts(e);
      if (found.length) {
        setConflicts(found);
        setError("These connectors cannot go into one build as they are. Leave one out, or add them one at a time.");
        window.setTimeout(() => conflictRef.current?.focus(), 0);
        return;
      }
      if (e instanceof IdentityError && e.code === "step_up_cancelled") { setError("Not stored: you cancelled the check."); return; }
      setError(refusalWords(e));
    } finally {
      setBusy(false);
    }
  }

  const title = cards.map((c) => c.name).join(" + ");
  const foot = (
    <>
      {blocker ? <span className="tpl-why" role="status" data-testid="cmp-blocked">{blocker}</span> : <span className="tpl-push" />}
      <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
      <GatedButton permission="workflow_run:start" projectId={projectId} type="button" className="ag-btn ag-btn-primary"
                   data-testid="cmp-start" disabled={busy || !!blocker} aria-busy={busy || undefined}
                   title={`One governed build adds all ${cards.length}: tests, review and one PR${target.kind === "add" ? `, on ${target.name}'s branch` : ""}`}
                   onClick={() => void start()}>
        {busy ? "Starting…" : `Start one build`}
      </GatedButton>
    </>
  );

  return (
    <SideSheet title={title} sub={`${cards.length} connectors, one build`} onClose={onClose} testId="cmp-sheet" foot={foot}>
      <p className="tpl-muted">One build adds them all, wired in this order. A connector another needs goes first.</p>
      {loadError ? <p className="tpl-err" role="alert">{loadError}</p> : null}
      {githubAsMeNotice(githubAsMe) ? (
        <p className="tpl-warn" role="status" data-testid="cmp-github-as-me">
          {githubAsMeNotice(githubAsMe)}{" "}
          <a href={githubAsMe?.href ?? "/account/agents"}>Link GitHub</a>
        </p>
      ) : null}

      <section className="tpl-sec" aria-label="Connectors">
        <h3>Connectors</h3>
        <ol className="tpl-rows" data-testid="cmp-order">
          {cards.map((c) => {
            const d = details?.find((x) => x.id === c.id);
            return (
              <li key={c.id} className="tpl-row" data-testid={`cmp-connector-${c.key}`}>
                <span className="tpl-row-main">
                  <span className="tpl-row-name" data-user-text={c.source === "git" || undefined}>{c.name}</span>
                  <span className="tpl-faint" data-user-text={c.source === "git" || undefined}>{c.summary}</span>
                  {d?.routes.length ? <span className="tpl-faint tpl-mono" data-user-text>{d.routes.join(", ")}</span> : null}
                </span>
              </li>
            );
          })}
        </ol>
      </section>

      {shownConflicts.length ? (
        <div ref={conflictRef} tabIndex={-1} className="tpl-sec" role="alert" aria-label="Conflicts" data-testid="cmp-conflicts">
          <h3>{conflicts.length ? "They conflict" : "These look like they conflict"}</h3>
          <ul className="tpl-tree">
            {shownConflicts.map((c) => (
              <li key={`${c.kind}:${c.what}`} data-user-text>{conflictLine(c, names)}</li>
            ))}
          </ul>
          <span className="tpl-faint">Leave one out, or add them one at a time with Add a feature.</span>
        </div>
      ) : null}

      <section className="tpl-sec" aria-label="Where">
        <h3>{target.kind === "new" ? "New app" : "Into"}</h3>
        {target.kind === "new" ? (
          <label className="tpl-field"><span>Name</span>
            <input value={name} maxLength={80} placeholder="Your app's name" data-testid="cmp-app-name" data-autofocus
                   onChange={(e) => setName(e.target.value)} />
          </label>
        ) : <span className="tpl-row-name" data-user-text>{target.name}</span>}
        <span className="tpl-chips" role="radiogroup" aria-label="Stack">
          <span className="tpl-stack-label" aria-hidden>Stack</span>
          {stacks.length ? stacks.map((s) => {
            const off = !!installed && !installed.has(s);
            return (
              <button key={s} type="button" role="radio" className="tpl-chip" aria-checked={stack === s}
                      aria-disabled={off || undefined} data-off={off || undefined} data-level={levels[s] || undefined}
                      title={stackTip(s, installed) + (levels[s] ? ` Support: ${levelWord(levels[s])}.` : "")}
                      data-testid={`cmp-stack-${s}`} onClick={() => { if (!off) { setStack(s); setPicked(true); } }}>
                {stackLabel(s)}{levels[s] ? `, ${levelWord(levels[s])}` : ""}{off ? ", off" : ""}
              </button>
            );
          }) : <span className="tpl-muted">None shared</span>}
        </span>
        {workflows.length > 1 ? (
          <label className="tpl-field" title="The workflow that builds it: its steps, reviews and approvals. Only workflows with a build step are listed.">
            <span>Workflow</span>
            <select value={workflowKey || defaultWorkflow?.key || ""} data-testid="cmp-workflow"
                    onChange={(e) => setWorkflowKey(e.target.value)}>
              {defaultWorkflow ? null : <option value="">Default</option>}
              {workflows.map((w) => (
                <option key={w.key} value={w.key}>{w.name}{w.isDefault ? " (default)" : ""}</option>
              ))}
            </select>
          </label>
        ) : null}
      </section>

      <section className="tpl-sec" aria-label="Secrets" data-testid="cmp-secrets">
        <h3>Secrets</h3>
        {!secrets.length ? <span className="tpl-muted">None</span> : (
          <ul className="tpl-rows">
            {secrets.map((s) => {
              const w = stored.has(s.name) ? { word: "Set", tone: "ok" as const, tip: "Stored in the vault. It is never shown again." }
                : secretWord({ testDefault: s.testDefault });
              const field = s.testDefault === "none" && !stored.has(s.name);
              return (
                <li key={s.name} className="tpl-row" data-testid={`cmp-secret-${s.name}`}>
                  <span className="tpl-row-main">
                    <span className="tpl-row-name tpl-mono">{s.name}</span>
                    <span className="tpl-faint" data-user-text>{[s.purpose, s.by.join(", ")].filter(Boolean).join(", ")}</span>
                  </span>
                  <span className="tpl-chip" data-tone={w.tone === "warn" ? "amber" : "green"} title={w.tip}>{w.word}</span>
                  {field ? (
                    <span className="tpl-row-set">
                      <input className="tpl-input" type="password" autoComplete="off" spellCheck={false}
                             aria-label={`Test key for ${s.name}`} placeholder="Paste a test key"
                             value={typed[s.name] ?? ""} data-testid={`cmp-secret-input-${s.name}`}
                             title="Write-only: stored in the vault and never shown again. Live keys are set later, in App › Setup."
                             onChange={(e) => setTyped((t) => ({ ...t, [s.name]: e.target.value }))} />
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
        {needs.length ? <span className="tpl-faint">Each key is asked for once, for every connector that uses it.</span> : null}
      </section>

      <section className="tpl-sec" aria-label="Talks to" data-testid="cmp-egress">
        <h3 title="Hosts the running app calls">Talks to</h3>
        {!egress.length ? <span className="tpl-muted">Nothing outside BOSS</span> : (
          <>
            <span className="tpl-chips">{egress.map((h) => <span key={h} className="tpl-chip tpl-mono">{h}</span>)}</span>
            <label className="tpl-check">
              <input type="checkbox" checked={egressOk} data-testid="cmp-egress-ok" onChange={(e) => setEgressOk(e.target.checked)} />
              Let the app call {egress.length === 1 ? "this host" : `these ${egress.length} hosts`}
            </label>
          </>
        )}
      </section>

      <label className="tpl-field"><span>Note for the builder <em className="tpl-faint">optional</em></span>
        <textarea rows={2} maxLength={4000} value={note} data-testid="cmp-note" placeholder="Anything about your app it should know"
                  onChange={(e) => setNote(e.target.value)} />
      </label>
      <StackWarning state={stackCheck} testId="cmp-stack-warning" />

      {refusal ? (
        <div data-testid="cmp-readiness">
          <ReadinessPanel readiness={refusal} accepted={startAnyway} projectId={projectId} onAccept={setStartAnyway} />
        </div>
      ) : null}
      {error ? <p className="tpl-err" role="alert" data-testid="cmp-error" data-user-text>{error}</p> : null}
    </SideSheet>
  );
}
