"use client";

/**
 * The `App target` sheet (spec §4.5): one form where there were three (the stack-target form,
 * the OnboardWizard, the registry's Register deployment target). Step 1 is `Kind`:
 *
 *   On a machine   VM, Name, Environment, Repository, Public URL, Manifest
 *                  → POST /v1/deployments/stack/targets (bound to its VM in one write)
 *                  `More` (the OnboardWizard's fields: health scheme, port, publishable ports,
 *                  app entry, credential) → then PATCH /v1/deployment-targets/{id} (merged, never
 *                  replacing what the target declared) and the credential bound as `ssh_key`
 *   Cloud account  App, Repository, Environment, Account, Kind, Ref, Domains, Secrets
 *                  → POST /v1/deployments (the registry target)
 *
 * All the routes stay; only the console converges. The stack-target rules are the old form's
 * (lib/stack-deploy targetFormProblem), said in the operator's words.
 */
import React, { useEffect, useState } from "react";

import type { Rack } from "../../lib/infra-view";
import { DEFAULT_MANIFEST_PATH, targetFormProblem } from "../../lib/stack-deploy";
import { showToast } from "../client-forms";
import {
  afterWrite, bindSecret, createRegistryTarget, createStackTarget, listRepositories, read, updateTarget,
} from "./infra-actions";
import { ErrorLine } from "./infra-util";
import { Sheet } from "./kit";
import { useSecrets } from "./secrets-sheet";

type Kind = "machine" | "cloud";
const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

export function AppTargetSheet({ projectId, actor, racks, hostId, onClose, onCreated }: {
  projectId: string; actor: string; racks: Rack[]; hostId?: string | null;
  onClose: () => void; onCreated: (targetId: string | null) => void;
}) {
  const vms = racks.filter((r) => r.state.tone === "green" && r.id !== "this");
  const [kind, setKind] = useState<Kind>("machine");
  const [more, setMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repos, setRepos] = useState<{ full_name: string }[]>([]);
  const [suggested, setSuggested] = useState("");
  const [accounts, setAccounts] = useState<{ id: string; provider: string; account_label: string }[]>([]);
  const { secrets } = useSecrets(more ? projectId : "");

  useEffect(() => {
    void listRepositories(projectId).then((r) => {
      if (r.ok) { setRepos((r.data.repositories ?? []).filter((x) => x.full_name && !x.archived)); setSuggested(r.data.suggested ?? ""); }
    });
  }, [projectId]);
  useEffect(() => {
    if (kind !== "cloud" || accounts.length) return;
    void read<{ items?: { id: string; provider: string; account_label: string }[] }>(`/v1/providers?project_id=${encodeURIComponent(projectId)}`)
      .then((r) => { if (r.ok) setAccounts(r.data.items ?? []); });
  }, [kind, projectId, accounts.length]);

  const submit = async (f: FormData) => {
    const v = (k: string) => String(f.get(k) ?? "").trim();
    setError(null);
    if (kind === "machine") {
      const input = { hostId: v("host_id"), name: v("name"), environment: v("environment"), repository: v("repository"),
                      publicUrl: v("public_url"), manifestPath: v("manifest_path") || DEFAULT_MANIFEST_PATH };
      const problem = targetFormProblem(input);
      if (problem) { setError(problem); return; }
      setBusy(true);
      const r = await createStackTarget(projectId, actor, { host_id: input.hostId, app_name: input.name, environment: input.environment,
        repository: input.repository, public_url: input.publicUrl || null, manifest_path: input.manifestPath });
      if (!r.ok) { setBusy(false); setError(r.error); return; }
      const id = r.data.id ? String(r.data.id) : null;
      if (id && more) {
        const patch: Record<string, unknown> = {};
        if (v("app_port")) patch.app_port = Number(v("app_port"));
        if (v("publishable_ports")) patch.publishable_ports = v("publishable_ports");
        if (v("app_entry")) patch.app_entry = v("app_entry");
        if (v("scheme")) patch.health_checks = [{ kind: "http", path: "/", expect: 200, scheme: v("scheme") }];
        if (Object.keys(patch).length) {
          const p = await updateTarget(id, actor, patch);
          if (!p.ok) { setBusy(false); setError(`Created, but More was not saved: ${p.error}`); afterWrite(); return; }
        }
        if (v("secret_ref_id")) {
          const b = await bindSecret(projectId, id, actor, "ssh_key", v("secret_ref_id"));
          if (!b.ok) { setBusy(false); setError(`Created, but the credential was not bound: ${b.error}`); afterWrite(); return; }
        }
      }
      setBusy(false);
      showToast("success", "Target created");
      afterWrite();
      onCreated(id);
      return;
    }
    if (!v("app_name") || !v("repo") || !v("target_kind") || !v("target_ref")) { setError("App, repository, kind and ref"); return; }
    setBusy(true);
    const r = await createRegistryTarget(projectId, actor, {
      app_name: v("app_name"), repo: v("repo"), environment: v("environment"),
      provider_account_id: v("provider_account_id") || undefined, target_kind: v("target_kind"), target_ref: v("target_ref"),
      domains: lines(v("domains")), secret_refs: lines(v("secret_refs")),
    });
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Target created");
    afterWrite();
    onCreated(r.data.id ? String(r.data.id) : null);
  };

  const initialRepo = repos.some((r) => r.full_name === suggested) ? suggested : repos[0]?.full_name ?? "";
  return (
    <Sheet title="App target" testId="inf-target-sheet" onClose={onClose}>
      <div className="inf-seg" role="radiogroup" aria-label="Kind">
        <button type="button" role="radio" aria-checked={kind === "machine"} className="inf-seg-btn" data-testid="inf-target-kind-machine"
                title="A stack on a machine BOSS manages" onClick={() => setKind("machine")}>On a machine</button>
        <button type="button" role="radio" aria-checked={kind === "cloud"} className="inf-seg-btn" data-testid="inf-target-kind-cloud"
                title="An app a cloud provider runs" onClick={() => setKind("cloud")}>Cloud account</button>
      </div>
      <form className="inf-form" key={kind} onSubmit={(e) => { e.preventDefault(); void submit(new FormData(e.currentTarget)); }}>
        {kind === "machine" ? (
          <>
            <div className="inf-form-row">
              <label className="inf-field"><span>VM</span>
                {vms.length ? (
                  <select name="host_id" defaultValue={hostId && vms.some((h) => h.id === hostId) ? hostId : vms[0].id} data-testid="inf-target-vm">
                    {vms.map((h) => <option key={h.id} value={h.id}>{h.name}{h.address ? ` · ${h.address}` : ""}</option>)}
                  </select>
                ) : <span className="inf-muted" title="A stack runs on a verified machine">No ready machine</span>}
              </label>
              <label className="inf-field" title="Lowercase letters, digits and dashes"><span>Name</span>
                <input name="name" placeholder="invoice" autoComplete="off" spellCheck={false} data-testid="inf-target-name" /></label>
              <label className="inf-field"><span>Environment</span>
                <input name="environment" defaultValue="production" autoComplete="off" data-testid="inf-target-env" /></label>
            </div>
            <div className="inf-form-row">
              <label className="inf-field"><span>Repository</span>
                {repos.length ? (
                  <select name="repository" key={initialRepo} defaultValue={initialRepo} data-testid="inf-target-repo">
                    {repos.map((r) => <option key={r.full_name} value={r.full_name}>{r.full_name}</option>)}
                  </select>
                ) : <input name="repository" placeholder="owner/name" autoComplete="off" spellCheck={false} data-testid="inf-target-repo" />}
              </label>
              <label className="inf-field"><span>Public URL</span>
                <input name="public_url" placeholder="https://app.example.com" autoComplete="off" spellCheck={false} data-testid="inf-target-url" /></label>
              <label className="inf-field" title="A file inside the repository"><span>Manifest</span>
                <input name="manifest_path" defaultValue={DEFAULT_MANIFEST_PATH} autoComplete="off" spellCheck={false} data-testid="inf-target-manifest" /></label>
            </div>
            <button type="button" className="inf-link" aria-expanded={more} data-testid="inf-target-more" onClick={() => setMore((m) => !m)}
                    title="Port, health check, app entry, credential">More {more ? "▴" : "▾"}</button>
            {more ? (
              <div className="inf-form-row" data-testid="inf-target-more-fields">
                <label className="inf-field" title="The port the app listens on"><span>Port</span>
                  <input name="app_port" type="number" min={1} max={65535} placeholder="3010" data-testid="inf-target-port" /></label>
                <label className="inf-field" title="Comma-separated, ranges allowed, e.g. 3003,4000,10000-10001"><span>Publishable ports</span>
                  <input name="publishable_ports" placeholder="3003,4000" autoComplete="off" data-testid="inf-target-publish" /></label>
                <label className="inf-field" title="Health check scheme"><span>Health</span>
                  <select name="scheme" defaultValue="" data-testid="inf-target-scheme">
                    <option value="">Default</option><option value="http">http</option><option value="https">https</option>
                  </select></label>
                <label className="inf-field" title="The file or directory that serves this app, when the repo has several"><span>App entry</span>
                  <input name="app_entry" placeholder="apps/web" autoComplete="off" spellCheck={false} data-testid="inf-target-entry" /></label>
                <label className="inf-field" title="Bound as ssh_key; blank falls back to the machine's key"><span>Credential</span>
                  <select name="secret_ref_id" defaultValue="" data-testid="inf-target-credential">
                    <option value="">Machine key</option>
                    {secrets.filter((s) => s.secret_kind.includes("private_key")).map((s) => (
                      <option key={s.secret_ref_id} value={s.secret_ref_id}>{s.name.split("/").pop()}</option>
                    ))}
                  </select></label>
              </div>
            ) : null}
          </>
        ) : (
          <>
            <div className="inf-form-row">
              <label className="inf-field"><span>App</span>
                <input name="app_name" required placeholder="billing" autoComplete="off" data-testid="inf-target-app" /></label>
              <label className="inf-field"><span>Repository</span>
                <input name="repo" required placeholder="owner/repository" defaultValue={initialRepo} autoComplete="off" spellCheck={false} data-testid="inf-target-reg-repo" /></label>
              <label className="inf-field"><span>Environment</span>
                <select name="environment" defaultValue="staging" data-testid="inf-target-reg-env">
                  {["local", "preview", "staging", "production"].map((e) => <option key={e} value={e}>{e}</option>)}
                </select></label>
            </div>
            <div className="inf-form-row">
              <label className="inf-field"><span>Account</span>
                <select name="provider_account_id" defaultValue="" data-testid="inf-target-account">
                  <option value="">None</option>
                  {accounts.map((a) => <option key={a.id} value={a.id}>{a.provider} · {a.account_label}</option>)}
                </select></label>
              <label className="inf-field" title="The provider's deploy kind, such as digitalocean_app"><span>Kind</span>
                <input name="target_kind" required placeholder="digitalocean_app" autoComplete="off" spellCheck={false} data-testid="inf-target-reg-kind" /></label>
              <label className="inf-field" title="The app id or service name at the provider"><span>Ref</span>
                <input name="target_ref" required placeholder="app-id" autoComplete="off" spellCheck={false} data-testid="inf-target-reg-ref" /></label>
            </div>
            <div className="inf-form-row">
              <label className="inf-field" title="One per line"><span>Domains</span>
                <textarea name="domains" rows={2} placeholder="staging.example.com" data-testid="inf-target-domains" /></label>
              <label className="inf-field" title="One secret name per line"><span>Secrets</span>
                <textarea name="secret_refs" rows={2} placeholder="sec_database_url" data-testid="inf-target-secret-refs" /></label>
            </div>
          </>
        )}
        <div className="inf-actions">
          <button type="submit" className="ag-btn ag-btn-primary" disabled={busy || (kind === "machine" && !vms.length)} data-testid="inf-target-create">
            {busy ? "Creating…" : "Create"}
          </button>
          <ErrorLine error={error} testId="inf-target-sheet-error" />
        </div>
      </form>
    </Sheet>
  );
}
