"use client";

/**
 * KIT ▸ TEMPLATES (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.5, §2.8): one surface for managing the
 * org's templates. A list (name · built-in or imported · version · status · tested), `Import from
 * git` (URL + ref), and a review sheet per template: what it adds, its secrets and hosts, the file
 * tree, the brief AS TEXT (an imported brief is untrusted, never rendered as markup), the
 * validation report and the trial; Approve / Reject for one pending review, Trial, Hide, Delete.
 *
 * D3: only an Owner or Admin approves (template:approve), and never their own import: for the
 * importer the buttons are greyed with that reason. Approve, Reject, Trial, Hide and Delete need a
 * fresh second factor (template:approve is step-up): useGuardedCall opens the sheet.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton, useGate } from "../../gated";
import { useGuardedCall, useIdentity } from "../../identity/identity-provider";
import { IdentityError } from "../../../lib/identity/api";
import {
  INSECURE, STATUS_WORD, refusalWords, stackLabel, templatesApi, testedWord, trialWord,
  type TemplateCard, type TemplateDetail,
} from "../../../lib/templates";
import { LabelChips, StackChips, useInstalledStacks, useStackLevels } from "./gallery";
import { SideSheet } from "./side-sheet";

/** Every template the org has, newest status first: pending review, approved, hidden, rejected. */
const ORDER: Record<string, number> = { pending_review: 0, active: 1, hidden: 2, rejected: 3 };
export function kitRows(lists: TemplateCard[][]): TemplateCard[] {
  const seen = new Map<string, TemplateCard>();
  for (const t of lists.flat()) if (!seen.has(t.id)) seen.set(t.id, t);
  return [...seen.values()].sort((a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || a.name.localeCompare(b.name));
}

/** Why this person cannot approve this import ("" when they can, as far as the console knows). */
export function approveBlocker(t: Pick<TemplateCard, "importedBy" | "status" | "source">, me: string | null): string {
  if (t.source !== "git") return "";
  if (t.status !== "pending_review") return "";
  if (me && t.importedBy && t.importedBy === me) return "You imported it: another Owner or Admin approves it.";
  return "";
}

/** The import's report, in words: why it was refused, without spec references. */
export function importReasons(v: unknown): string[] {
  const r = (v && typeof v === "object" ? (v as { report?: { reasons?: unknown } }).report : null) ?? null;
  const reasons = Array.isArray(r?.reasons) ? r!.reasons.map(String) : [];
  return reasons.map((x) => x.replace(/\s*\((?:D\d+|§[\d.]+[^)]*)\)/g, "").trim());
}

export function KitTemplates({ projectId }: { projectId: string }) {
  const [rows, setRows] = useState<TemplateCard[] | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<TemplateCard | null>(null);
  const [importing, setImporting] = useState(false);
  const installed = useInstalledStacks(projectId);
  const levels = useStackLevels();

  const load = useCallback(async () => {
    try {
      const lists = await Promise.all([
        templatesApi.list(), templatesApi.list({ status: "hidden" }), templatesApi.list({ status: "rejected" }),
      ]);
      setRows(kitRows(lists));
      setError("");
    } catch (e) {
      setRows((r) => r ?? []);
      setError(refusalWords(e));
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const pending = (rows ?? []).filter((t) => t.status === "pending_review").length;

  return (
    <div className="tpl-kit" data-testid="kit-templates">
      <div className="tpl-kit-head">
        <h3>Templates</h3>
        <span className="tpl-muted">{rows ? `${rows.length} · ${pending ? `${pending} to review` : "none to review"}` : ""}</span>
        <span className="tpl-push" />
        <GatedButton permission="template:import" type="button" className="ag-btn" data-testid="tpl-import-open"
                     title="Import a template from a git URL. Nothing in it runs until an Owner or Admin approves it."
                     onClick={() => setImporting(true)}>Import from git</GatedButton>
      </div>
      {error ? <p className="tpl-err" role="alert">{error}</p> : null}
      {rows === null ? <p className="tpl-muted">Loading…</p> : null}
      {rows?.length ? (
        <ul className="tpl-rows" aria-label="Templates">
          {rows.map((t) => (
            <li key={t.id} className="tpl-row" data-testid={`tpl-kit-row-${t.key}`}>
              <span className="tpl-row-main">
                <button type="button" className="ag-link tpl-row-name" style={{ textAlign: "left" }} data-testid={`tpl-kit-open-${t.key}`}
                        onClick={() => setOpen(t)} data-user-text={t.source === "git" || undefined}>{t.name}</button>
                <span className="tpl-faint">{t.source === "git" ? "Imported" : "Built-in"} · v{t.version}{t.summary ? ` · ${t.summary}` : ""}</span>
              </span>
              <StackChips stacks={t.stacks} installed={installed} levels={levels} />
              {t.tested.length ? <span className="tpl-tested">{testedWord(t.tested)}</span> : null}
              <span className="tpl-chip" data-tone={t.status === "pending_review" ? "amber" : t.status === "rejected" ? "red" : t.status === "active" ? "green" : undefined}
                    data-off={t.status === "hidden" || undefined}>{STATUS_WORD[t.status] ?? t.status}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {importing ? <ImportSheet onClose={() => setImporting(false)} onDone={() => { setImporting(false); void load(); }} /> : null}
      {open ? <ReviewSheet card={open} installed={installed} onClose={() => setOpen(null)} onChanged={() => { setOpen(null); void load(); }} /> : null}
    </div>
  );
}

function ImportSheet({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reasons, setReasons] = useState<string[]>([]);
  const [done, setDone] = useState("");
  const gate = useGate("template:import");
  const canApprove = useGate("template:approve").allowed;
  const urlWhy = importUrlProblem(url);
  const valid = !urlWhy;
  async function go() {
    if (!valid || busy) return;
    if (!gate.allowed) { setError(gate.reason); return; }
    setBusy(true); setError(""); setReasons([]); setDone("");
    try {
      const r = await templatesApi.importFromGit(url.trim(), ref, ack);
      setDone(`${String(r.template_key ?? "The template")} ${String(r.version ?? "")} is waiting for review. Another Owner or Admin approves it.`);
    } catch (e) {
      const why = e instanceof IdentityError ? importReasons(e.body) : [];
      setReasons(why);
      setError(why.length ? "Refused: it did not pass the checks." : refusalWords(e));
    } finally {
      setBusy(false);
    }
  }
  const foot = done ? (
    <><span className="tpl-push" /><button type="button" className="ag-btn ag-btn-primary" data-testid="tpl-import-done" onClick={onDone}>Done</button></>
  ) : (
    <>
      {!valid ? <span className="tpl-why" data-testid="tpl-import-why">{urlWhy}</span> : <span className="tpl-push" />}
      <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
      <GatedButton permission="template:import" type="button" className="ag-btn ag-btn-primary" data-testid="tpl-import-go"
                   disabled={!valid || busy} aria-busy={busy || undefined} title={urlWhy || "Fetch and check it. Nothing in it runs."}
                   onClick={() => void go()}>{busy ? "Checking…" : "Import"}</GatedButton>
    </>
  );
  return (
    <SideSheet title="Import a template" onClose={onClose} testId="tpl-import-sheet" foot={foot}>
      <p className="tpl-muted">BOSS fetches it, checks it without running anything, and holds it for review.</p>
      <label className="tpl-field"><span>Git URL</span>
        <input value={url} placeholder="https://github.com/acme/boss-template-health" data-testid="tpl-import-url" data-autofocus
               inputMode="url" spellCheck={false} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label className="tpl-field"><span>Branch, tag or commit <em className="tpl-faint">optional</em></span>
        <input value={ref} placeholder="main" data-testid="tpl-import-ref" spellCheck={false} onChange={(e) => setRef(e.target.value)} />
      </label>
      <label className="tpl-check" title={canApprove ? "A deliberately vulnerable template, for security practice" : "Only an Owner or Admin can import one labelled insecure by design"}>
        <input type="checkbox" checked={ack} disabled={!canApprove} data-testid="tpl-import-ack" onChange={(e) => setAck(e.target.checked)} />
        It is insecure by design, and I know it
      </label>
      {done ? <p className="tpl-muted" role="status" data-testid="tpl-import-result">{done}</p> : null}
      {error ? <p className="tpl-err" role="alert" data-testid="tpl-import-error" data-user-text>{error}</p> : null}
      {reasons.length ? <ul className="tpl-tree" data-testid="tpl-import-reasons">{reasons.map((r) => <li key={r} data-user-text>{r}</li>)}</ul> : null}
    </SideSheet>
  );
}

function ReviewSheet({ card, installed, onClose, onChanged }: {
  card: TemplateCard; installed: Set<string> | null; onClose: () => void; onChanged: () => void;
}) {
  const levels = useStackLevels();
  const [d, setD] = useState<TemplateDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string>("");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [confirm, setConfirm] = useState<"hide" | "delete" | null>(null);
  const [trialNote, setTrialNote] = useState("");
  const guarded = useGuardedCall();
  const me = useIdentity().me?.principal_id ?? null;
  useEffect(() => {
    let alive = true;
    templatesApi.describe(card.id).then((x) => { if (alive) setD(x); }).catch((e) => { if (alive) setError(refusalWords(e)); });
    return () => { alive = false; };
  }, [card.id]);
  const t = d ?? card;
  const seat = approveBlocker(t, me);

  async function act(kind: string, fn: () => Promise<unknown>, reasonWords: string) {
    setBusy(kind); setError("");
    try {
      await guarded(reasonWords, fn);
      onChanged();
    } catch (e) {
      setError(e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not done: you cancelled the check." : refusalWords(e));
    } finally {
      setBusy("");
    }
  }
  async function trial() {
    setBusy("trial"); setError(""); setTrialNote("");
    try {
      const r = await guarded("Trial this template.", () => templatesApi.trial(card.id));
      setTrialNote(trialWord(r));
      setD((x) => (x ? { ...x, trial: r } : x));
    } catch (e) {
      setError(refusalWords(e));
    } finally {
      setBusy("");
    }
  }
  const pending = t.status === "pending_review";
  const imported = t.source === "git";
  const foot = (
    <>
      {seat ? <span className="tpl-why" data-testid="tpl-seat">{seat}</span> : <span className="tpl-push" />}
      {pending ? (
        <>
          <GatedButton permission="template:approve" type="button" className="ag-btn" data-testid="tpl-reject"
                       disabled={!!busy || !!seat} title={seat || "Refuse it: its files are dropped"}
                       onClick={() => setRejecting(true)}>Reject</GatedButton>
          <GatedButton permission="template:approve" type="button" className="ag-btn ag-btn-primary" data-testid="tpl-approve"
                       disabled={!!busy || !!seat || !(d?.validation.ok)} aria-busy={busy === "approve" || undefined}
                       title={seat || (d && !d.validation.ok ? "It did not pass the checks" : "Pin this commit and make it usable by your team")}
                       onClick={() => void act("approve", () => templatesApi.approve(card.id), `Approve ${t.name}.`)}>
            {busy === "approve" ? "Approving…" : "Approve"}
          </GatedButton>
        </>
      ) : null}
      {t.status === "active" ? (
        <>
          <GatedButton permission="template:approve" type="button" className="ag-btn" data-testid="tpl-trial" disabled={!!busy}
                       aria-busy={busy === "trial" || undefined} title="Run its own tests in a throwaway sandbox, per stack. Can take minutes."
                       onClick={() => void trial()}>{busy === "trial" ? "Trialling…" : "Trial"}</GatedButton>
          <GatedButton permission="template:approve" type="button" className="ag-btn" data-testid="tpl-hide" disabled={!!busy}
                       title="Take it out of the gallery. Apps that used it keep it." onClick={() => setConfirm("hide")}>Hide</GatedButton>
        </>
      ) : null}
      {imported && !pending && t.status !== "active" ? (
        <GatedButton permission="template:approve" type="button" className="ag-btn" data-testid="tpl-delete" disabled={!!busy}
                     title="Delete the import (hidden instead when an app applied it)" onClick={() => setConfirm("delete")}>Delete</GatedButton>
      ) : null}
    </>
  );
  return (
    <SideSheet title={t.name} sub={`${imported ? "Imported" : "Built-in"} · v${t.version} · ${STATUS_WORD[t.status] ?? t.status}`}
               onClose={onClose} testId="tpl-review-sheet" foot={foot} wide>
      {error ? <p className="tpl-err" role="alert" data-testid="tpl-review-error" data-user-text>{error}</p> : null}
      <p className="tpl-muted" data-user-text={imported || undefined}>{t.summary}</p>
      <LabelChips labels={t.labels} />
      {t.labels.includes(INSECURE) ? <p className="tpl-warn">Deliberately vulnerable. It applies to a new app only and is never deployed.</p> : null}
      {rejecting ? (
        <form className="tpl-sec" onSubmit={(e) => { e.preventDefault(); void act("reject", () => templatesApi.reject(card.id, reason.trim()), `Reject ${t.name}.`); }}>
          <label className="tpl-field"><span>Why <em className="tpl-faint">the importer sees it</em></span>
            <input value={reason} maxLength={500} data-testid="tpl-reject-reason" autoFocus onChange={(e) => setReason(e.target.value)} />
          </label>
          <span className="tpl-row-acts">
            <GatedButton permission="template:approve" type="submit" className="ag-btn" data-testid="tpl-reject-yes" disabled={!!busy}>Reject it</GatedButton>
            <button type="button" className="ag-link" onClick={() => setRejecting(false)}>Cancel</button>
          </span>
        </form>
      ) : null}
      {confirm ? (
        <div className="tpl-row-acts" role="alertdialog" aria-label="Confirm">
          <span className="tpl-warn">{confirm === "hide" ? `Hide ${t.name} from the gallery?` : `Delete ${t.name}?`}</span>
          <GatedButton permission="template:approve" type="button" className="ag-btn" data-testid="tpl-confirm-yes" disabled={!!busy}
                       onClick={() => void act(confirm, () => (confirm === "hide" ? templatesApi.hide(card.id) : templatesApi.remove(card.id)),
                                               `${confirm === "hide" ? "Hide" : "Delete"} ${t.name}.`)}>
            {confirm === "hide" ? "Hide" : "Delete"}
          </GatedButton>
          <button type="button" className="ag-link" onClick={() => setConfirm(null)}>Keep</button>
        </div>
      ) : null}

      <section className="tpl-sec" aria-label="What it is">
        <h3>What it is</h3>
        <span className="tpl-chips"><StackChips stacks={t.stacks} installed={installed} levels={levels} /></span>
        {t.adds.length ? <span className="tpl-faint">Adds: {t.adds.map((a) => a.replace(/_/g, " ")).join(", ")}</span> : null}
        {t.secrets.length ? <span className="tpl-faint">Secrets: {t.secrets.map((s) => s.name).join(", ")}</span> : null}
        <span className="tpl-faint">Talks to: {t.egress.length ? t.egress.join(", ") : "nothing outside BOSS"}</span>
        {imported ? <span className="tpl-faint tpl-mono" data-user-text>{t.gitUrl}{t.commitSha ? ` @ ${t.commitSha.slice(0, 12)}` : ""}</span> : null}
      </section>

      <section className="tpl-sec" aria-label="Checks" data-testid="tpl-validation">
        <h3>Checks</h3>
        {d ? (d.validation.ok
          ? <span className="tpl-chip" data-tone="green">Passed · {d.validation.fileCount} files</span>
          : <ul className="tpl-tree">{d.validation.reasons.map((r) => <li key={r} data-user-text>{r.replace(/\s*\((?:D\d+|§[\d.]+[^)]*)\)/g, "")}</li>)}</ul>)
          : <span className="tpl-muted">Loading…</span>}
        {d?.validation.rejected ? <span className="tpl-faint" data-user-text>Rejected: {d.validation.rejected.reason || "no reason given"}</span> : null}
      </section>

      <section className="tpl-sec" aria-label="Trial" data-testid="tpl-trial-result">
        <h3>Trial</h3>
        <span className="tpl-muted">{trialNote || trialWord(d?.trial ?? null)}</span>
        {d?.trial ? (
          <ul className="tpl-tree">
            {Object.entries(d.trial.stacks).map(([s, r]) => (
              <li key={s}>{stackLabel(s)}: {r.ok ? `✓ ${r.passed} of ${r.tests} tests` : `✗ ${r.error || `${r.passed} of ${r.tests} tests`}`}</li>
            ))}
          </ul>
        ) : null}
      </section>

      {d?.files.length ? (
        <details className="tpl-fold">
          <summary>Files · {d.files.length}</summary>
          <ul className="tpl-tree" data-testid="tpl-files">{d.files.map((f) => <li key={f.path} data-user-text>{f.path} <span className="tpl-faint">{f.size} B</span></li>)}</ul>
        </details>
      ) : null}
      {d?.brief ? (
        <details className="tpl-fold">
          <summary title={imported ? "Untrusted: shown as text, and the build agent reads it as reference only" : undefined}>Brief{imported ? " · untrusted" : ""}</summary>
          <pre className="tpl-pre" data-testid="tpl-brief" data-user-text>{d.brief}</pre>
        </details>
      ) : null}
      {d?.securityNote ? (
        <details className="tpl-fold">
          <summary>Security note</summary>
          <pre className="tpl-pre" data-user-text>{d.securityNote}</pre>
        </details>
      ) : null}
      {d && d.versions.length > 1 ? (
        <span className="tpl-faint">Versions: {d.versions.map((v) => `${v.version} (${STATUS_WORD[v.status] ?? v.status})`).join(", ")}</span>
      ) : null}
    </SideSheet>
  );
}

/** Why Import is off for this URL, in words ("" when it may be imported). An http:// URL used to
 *  grey the button with only the generic tooltip; it now says to use https. */
export function importUrlProblem(raw: string): string {
  const url = raw.trim();
  if (!url) return "Paste the template's https:// git URL.";
  if (/^http:\/\//i.test(url)) return "Use an https:// URL: BOSS does not fetch templates over plain http.";
  if (/^(https:\/\/|file:\/\/)\S{4,}$/.test(url)) return "";
  if (/^(git@|ssh:\/\/|git:\/\/)/i.test(url)) return "Use an https:// URL: BOSS fetches templates over https only.";
  return "Use an https:// git URL, like https://github.com/acme/boss-template-health.";
}
