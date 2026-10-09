"use client";

/**
 * APP ▸ SETUP (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.8): one surface for an app's provider-side
 * setup. Three parts, top to bottom:
 *
 *   Templates   each applied template, its version, stack and state (Building · PR open · Applied)
 *   Secrets     every secret its templates declare, per environment (Build · Preview · Deploy):
 *               Test mode / Set / Live / Needs a key; Set test key (build + preview), Set live
 *               key (deploy only, app_secret:live, used once a person approves), Remove
 *   Guide       each template's setup guide with its placeholders filled, and the values to
 *               paste elsewhere (preview URL, redirect URI, webhook URL) each with a Copy button
 *
 * Values are WRITE-ONLY (§2.4): a field clears once stored; nothing ever reads one back. Writes
 * need a fresh second factor (app_secret:write is step-up): useGuardedCall opens the sheet.
 * A viewer sees every control greyed with the reason (components/gated.tsx), and the secrets
 * part says why it is empty when the person cannot read secret metadata.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton, useGate } from "../../gated";
import { useGuardedCall } from "../../identity/identity-provider";
import { IdentityError } from "../../../lib/identity/api";
import { Markdown } from "../../../app/markdown";
import {
  APPLIED_WORD, ENVS, ENV_WORD, refusalWords, resolveAppKey, secretWord, stackLabel, templatesApi,
  type AppSecrets, type Applied, type Guide, type SecretBinding,
} from "../../../lib/templates";
import { CopyValue, SideSheet } from "./side-sheet";

/** What each copy key is called on the screen. */
export const COPY_LABEL: Record<string, string> = {
  preview_url: "Preview URL", redirect_uri: "Redirect URI", public_url: "BOSS address",
};
export function copyLabel(key: string): string {
  if (COPY_LABEL[key]) return COPY_LABEL[key];
  const hook = /^webhook_url:(.+)$/.exec(key);
  return hook ? `Webhook URL (${hook[1]})` : key.replace(/_/g, " ");
}
/** A placeholder the API could not fill ("(start a preview to get its URL)"): said, not copied. */
export const unfilled = (v: string): boolean => v.startsWith("(") || v.includes("<preview>");

/** One secret's cell for one environment. */
export function envCell(o: { env: string; testDefault: string; binding: SecretBinding | undefined }) {
  if (o.env === "deploy" && !o.binding) return { word: "Not set", tone: "off" as const, tip: "A deploy gets no value until one is set" };
  const w = secretWord({ testDefault: o.testDefault, binding: o.binding ?? null });
  return { word: w.word, tone: w.tone, tip: w.tip };
}

type Editing = { name: string; kind: "test" | "live" } | null;

export function AppSetupSheet({ projectId, appKey, runId, appName, onClose }: {
  projectId: string;
  /** The app's key (the factory row's `app_key`); resolved from `runId` when absent. */
  appKey?: string | null; runId: string; appName: string; onClose: () => void;
}) {
  const [key, setKey] = useState<string | null>(appKey ?? null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    if (appKey) { setKey(appKey); return undefined; }
    let alive = true;
    void resolveAppKey(projectId, runId).then((k) => { if (!alive) return; setKey(k); setMissing(!k); });
    return () => { alive = false; };
  }, [projectId, appKey, runId]);
  return (
    <SideSheet title="Setup" sub={appName} onClose={onClose} testId="tpl-setup-sheet" wide>
      {key ? <AppSetup projectId={projectId} appKey={key} />
        : <p className="tpl-muted">{missing ? "This app has no finished build yet." : "Loading…"}</p>}
    </SideSheet>
  );
}

export function AppSetup({ projectId, appKey }: { projectId: string; appKey: string }) {
  const [applied, setApplied] = useState<Applied[] | null>(null);
  const [guides, setGuides] = useState<Guide[]>([]);
  const [secrets, setSecrets] = useState<AppSecrets | null>(null);
  const [secretsWhy, setSecretsWhy] = useState("");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Editing>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const readGate = useGate("app_secret:read_metadata", projectId);

  const load = useCallback(async () => {
    try {
      const [a, g] = await Promise.all([templatesApi.applied(projectId, appKey), templatesApi.guides(projectId, appKey)]);
      setApplied(a); setGuides(g); setError("");
    } catch (e) {
      setApplied((prev) => prev ?? []);
      setError(refusalWords(e));
    }
    if (!readGate.allowed) { setSecrets(null); setSecretsWhy(readGate.reason); return; }
    try { setSecrets(await templatesApi.secrets(projectId, appKey)); setSecretsWhy(""); }
    catch (e) { setSecrets(null); setSecretsWhy(refusalWords(e)); }
  }, [projectId, appKey, readGate.allowed, readGate.reason]);
  useEffect(() => { void load(); }, [load]);

  async function save() {
    if (!editing || !value.trim() || busy) return;
    setBusy(true); setError(""); setNote("");
    const envs = editing.kind === "live" ? ["deploy"] : ["build", "preview"];
    try {
      let decision = false;
      for (const env of envs) {
        const r = await guarded(`Store ${editing.name}.`, () => templatesApi.putSecret(projectId, appKey, editing.name,
          { env, mode: editing.kind, value: value.trim() }));
        if ((r as { live_decision_id?: unknown }).live_decision_id) decision = true;
      }
      setNote(decision ? `${editing.name} stored. A deploy uses it only after a person approves it in Approvals.`
        : `${editing.name} stored for builds and previews.`);
      setEditing(null);
      setValue("");
      void load();
    } catch (e) {
      setError(e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not stored: you cancelled the check." : refusalWords(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(name: string) {
    if (busy) return;
    setBusy(true); setError(""); setNote("");
    try {
      for (const b of (secrets?.bindings ?? []).filter((x) => x.name === name)) {
        await guarded(`Remove ${name}.`, () => templatesApi.deleteSecret(projectId, appKey, name, b.env));
      }
      setNote(`${name} removed.`);
      setConfirmRemove(null);
      void load();
    } catch (e) {
      setError(refusalWords(e));
    } finally {
      setBusy(false);
    }
  }

  const declared = secrets?.declared ?? [];
  return (
    <div className="tpl-setup" data-testid="tpl-setup">
      {error ? <p className="tpl-err" role="alert" data-user-text>{error}</p> : null}

      <section className="tpl-sec" aria-label="Templates" data-testid="tpl-setup-applied">
        <h3>Templates</h3>
        {applied === null ? <span className="tpl-muted">Loading…</span> : null}
        {applied && !applied.length ? (
          <span className="tpl-muted" data-testid="tpl-setup-none">No template applied yet. Add one from the app&apos;s Add a feature, then + Template.</span>
        ) : null}
        {applied?.length ? (
          <ul className="tpl-rows">
            {applied.map((a) => (
              <li key={a.id} className="tpl-row" data-testid={`tpl-applied-${a.key}`}>
                <span className="tpl-row-main">
                  <span className="tpl-row-name">{a.name} <span className="tpl-faint">v{a.version}</span></span>
                  <span className="tpl-faint">{stackLabel(a.stack)}</span>
                </span>
                <span className="tpl-chip" data-tone={a.status === "applied" ? "green" : a.status === "failed" ? "red" : undefined}
                      title={a.status === "pr_open" ? "Its build opened a PR: merge it to finish" : a.status === "applied" ? "Merged into the app" : undefined}>
                  {APPLIED_WORD[a.status] ?? a.status}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section className="tpl-sec" aria-label="Secrets" data-testid="tpl-setup-secrets">
        <h3 title="Write-only: stored in the vault, never shown again">Secrets</h3>
        {secretsWhy ? <span className="tpl-muted" data-testid="tpl-setup-secrets-why">{secretsWhy}</span> : null}
        {secrets && !declared.length ? <span className="tpl-muted">No template of this app needs a secret.</span> : null}
        {declared.length ? (
          <ul className="tpl-rows">
            {declared.map((d) => {
              const mine = (secrets?.bindings ?? []).filter((b) => b.name === d.name);
              const open = editing?.name === d.name;
              return (
                <li key={d.name} className="tpl-row" data-testid={`tpl-setup-secret-${d.name}`}>
                  <span className="tpl-row-main">
                    <span className="tpl-row-name tpl-mono">{d.name}</span>
                    {d.purpose ? <span className="tpl-faint" data-user-text>{d.purpose}</span> : null}
                  </span>
                  <span className="tpl-envs" aria-label={`${d.name} per environment`}>
                    {ENVS.map((env) => {
                      const c = envCell({ env, testDefault: d.testDefault, binding: mine.find((b) => b.env === env) });
                      return (
                        <span key={env} className="tpl-chip" data-tone={c.tone === "warn" ? "amber" : c.tone === "ok" || c.tone === "live" ? "green" : undefined}
                              data-off={c.tone === "off" || undefined} title={c.tip} data-testid={`tpl-env-${d.name}-${env}`}>
                          {ENV_WORD[env]} · {c.word}
                        </span>
                      );
                    })}
                  </span>
                  <span className="tpl-row-acts">
                    <GatedButton permission="app_secret:write" projectId={projectId} type="button" className="ag-link"
                                 data-testid={`tpl-set-test-${d.name}`} disabled={busy}
                                 title="Paste a test key for builds and previews (asks you to confirm it is you)"
                                 onClick={() => { setEditing({ name: d.name, kind: "test" }); setValue(""); setNote(""); }}>
                      {mine.some((b) => b.mode === "test") ? "Replace test key" : "Set test key"}
                    </GatedButton>
                    <GatedButton permission="app_secret:live" projectId={projectId} type="button" className="ag-link"
                                 data-testid={`tpl-set-live-${d.name}`} disabled={busy}
                                 title="A live key, for deploys only: no deploy uses it until a person approves"
                                 onClick={() => { setEditing({ name: d.name, kind: "live" }); setValue(""); setNote(""); }}>
                      Set live key
                    </GatedButton>
                    {mine.some((b) => b.mode === "test" || b.mode === "live") ? (
                      confirmRemove === d.name ? (
                        <>
                          <span className="tpl-warn">Remove {d.name} everywhere?</span>
                          <GatedButton permission="app_secret:write" projectId={projectId} type="button" className="ag-btn"
                                       data-testid={`tpl-remove-yes-${d.name}`} disabled={busy} onClick={() => void remove(d.name)}>Remove</GatedButton>
                          <button type="button" className="ag-link" onClick={() => setConfirmRemove(null)}>Keep</button>
                        </>
                      ) : (
                        <GatedButton permission="app_secret:write" projectId={projectId} type="button" className="ag-link"
                                     data-testid={`tpl-remove-${d.name}`} disabled={busy} title="Remove its stored values"
                                     onClick={() => setConfirmRemove(d.name)}>Remove</GatedButton>
                      )
                    ) : null}
                  </span>
                  {open ? (
                    <form className="tpl-row-set" onSubmit={(e) => { e.preventDefault(); void save(); }} data-testid={`tpl-secret-form-${d.name}`}>
                      <input className="tpl-input" type="password" autoComplete="off" spellCheck={false} autoFocus value={value}
                             aria-label={`${editing?.kind === "live" ? "Live" : "Test"} key for ${d.name}`}
                             placeholder={editing?.kind === "live" ? "Paste the live key (deploys only)" : "Paste a test key"}
                             data-testid={`tpl-secret-value-${d.name}`} onChange={(e) => setValue(e.target.value)} />
                      <GatedButton permission={editing?.kind === "live" ? "app_secret:live" : "app_secret:write"} projectId={projectId}
                                   type="submit" className="ag-btn ag-btn-primary" disabled={busy || !value.trim()}
                                   title={value.trim() ? "Stored write-only: never shown again" : "Paste the value first"}
                                   data-testid={`tpl-secret-save-${d.name}`}>{busy ? "Saving…" : "Save"}</GatedButton>
                      <button type="button" className="ag-link" onClick={() => { setEditing(null); setValue(""); }}>Cancel</button>
                    </form>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : null}
        {note ? <span className="tpl-muted" role="status" data-testid="tpl-setup-note">{note}</span> : null}
      </section>

      <section className="tpl-sec" aria-label="Setup guide" data-testid="tpl-setup-guides">
        <h3>Guide</h3>
        {applied && !guides.length ? <span className="tpl-muted">Nothing to set up by hand.</span> : null}
        {guides.map((g) => {
          const copies = Object.entries(g.copy);
          return (
            <article key={g.key} className="tpl-guide" data-testid={`tpl-guide-${g.key}`}>
              <div className="tpl-guide-head"><b>{g.name}</b><span className="tpl-faint">v{g.version} · {APPLIED_WORD[g.status] ?? g.status}</span></div>
              {copies.length ? (
                <div className="tpl-guide-copies">
                  {copies.map(([k, v]) => (
                    <React.Fragment key={k}>
                      <span>{copyLabel(k)}</span>
                      {unfilled(v) ? <span className="tpl-faint" data-user-text>{v.replace(/^\(|\)$/g, "")}</span>
                        : <CopyValue value={v} label={copyLabel(k)} testId={`tpl-copy-${g.key}-${k}`} />}
                    </React.Fragment>
                  ))}
                </div>
              ) : null}
              <details className="tpl-fold" open>
                <summary>Steps</summary>
                <div className="tpl-guide-text md" data-user-text><Markdown text={g.guide} /></div>
              </details>
            </article>
          );
        })}
      </section>
    </div>
  );
}
