"use client";

/**
 * The Secrets sheet (spec §4.3 `+ Add › Secret`, §4.6 Deploys `⋯ › Secrets`; parity P-I-53):
 * the old SecretsPanel, name, kind, backend, write-only value, `Upload`, `Generate keypair`,
 * and the list with `Verify`. BOSS keeps a reference, never the value: the value field is
 * cleared the moment it is written through, and the only key material shown is a public half.
 */
import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useState } from "react";

import { showToast } from "../client-forms";
import {
  afterWrite, generateKeypair, listSecrets, putSecret, secretAccessLog, verifySecret,
  type SecretAccessLog, type SecretRow,
} from "./infra-actions";
import { CopyButton, ErrorLine } from "./infra-util";
import { Sheet } from "./kit";

const KINDS = ["api_token", "ssh_private_key", "kubeconfig", "access_key"];
const BACKENDS: { key: string; label: string }[] = [
  { key: "platform_secret_store", label: "Secret store" }, { key: "database", label: "Database" },
  { key: "file", label: "File" }, { key: "env", label: "Env (read only)" },
];

export function useSecrets(projectId: string): { secrets: SecretRow[]; error: string | null; reload: () => void } {
  const [secrets, setSecrets] = useState<SecretRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    if (!projectId) return;
    void listSecrets(projectId).then((r) => {
      if (r.ok) { setSecrets(r.data.secrets ?? []); setError(null); } else setError(r.error);
    });
  }, [projectId]);
  useEffect(() => { reload(); }, [reload]);
  return { secrets, error, reload };
}

export function SecretsSheet({ projectId, actor, onClose, canWrite = true }: {
  projectId: string; actor: string; onClose: () => void; canWrite?: boolean;
}) {
  const { secrets, error: loadError, reload } = useSecrets(projectId);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("api_token");
  const [backend, setBackend] = useState("platform_secret_store");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [publicKey, setPublicKey] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<Record<string, string>>({});
  // BOSSAPP-865: one secret's access log, open beneath its row.
  const [logFor, setLogFor] = useState<string | null>(null);
  const [log, setLog] = useState<SecretAccessLog | null>(null);
  const [logError, setLogError] = useState<string | null>(null);
  const toggleLog = async (s: SecretRow) => {
    if (logFor === s.secret_ref_id) { setLogFor(null); return; }
    setLogFor(s.secret_ref_id); setLog(null); setLogError(null);
    const r = await secretAccessLog(projectId, s.secret_ref_id);
    if (r.ok) setLog(r.data); else setLogError(r.error);
  };

  const upload = async () => {
    if (!name.trim() || !value) { setError("Name and value"); return; }
    setBusy("upload"); setError(null); setPublicKey(null);
    const r = await putSecret(projectId, actor, { name: name.trim(), value, secret_kind: kind, backend });
    setValue("");   // written through: never kept in the browser
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", `Stored ${name.trim()}`);
    afterWrite(reload);
  };
  const keygen = async () => {
    if (!name.trim()) { setError("Name it first"); return; }
    setBusy("keygen"); setError(null);
    const r = await generateKeypair(projectId, actor, { name: name.trim(), backend });
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    setPublicKey(r.data.public_key ?? null);
    afterWrite(reload);
  };
  const verify = async (s: SecretRow) => {
    setBusy(`v-${s.secret_ref_id}`);
    const r = await verifySecret(s.secret_ref_id, actor);
    setBusy(null);
    const word = !r.ok ? "Can't verify" : r.data.resolvable ? (r.data.fingerprint_unchanged === false ? "Changed" : "Resolves") : "Unresolvable";
    setVerdict((v) => ({ ...v, [s.secret_ref_id]: word }));
    if (r.ok && r.data.resolvable) showToast("success", "Verified");
  };

  return (
    <Sheet title="Secrets" testId="inf-secrets-sheet" onClose={onClose}>
      {canWrite ? (
        <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void upload(); }}>
          <div className="inf-form-row">
            <label className="inf-field"><span>Name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="ssh_key" data-testid="inf-secret-name"
                     autoComplete="off" spellCheck={false} /></label>
            <label className="inf-field"><span>Kind</span>
              <select value={kind} onChange={(e) => setKind(e.target.value)} data-testid="inf-secret-kind">
                {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
              </select></label>
            <label className="inf-field"><span>Backend</span>
              <select value={backend} onChange={(e) => setBackend(e.target.value)} data-testid="inf-secret-backend">
                {BACKENDS.map((b) => <option key={b.key} value={b.key}>{b.label}</option>)}
              </select></label>
          </div>
          <label className="inf-field" title="Write only. Never shown again.">
            <span>Value</span>
            <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off"
                   data-testid="inf-secret-value" placeholder="Never shown again" />
          </label>
          <div className="inf-actions">
            <GatedButton permission="secret:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-secret-upload">
              {busy === "upload" ? "Uploading…" : "Upload"}
            </GatedButton>
            <GatedButton permission="secret:manage" type="button" className="ag-btn" disabled={busy !== null} data-testid="inf-secret-keygen"
                    title="BOSS keeps the private half and shows you the public one" onClick={() => void keygen()}>
              {busy === "keygen" ? "Generating…" : "Generate keypair"}
            </GatedButton>
            <ErrorLine error={error} testId="inf-secret-error" />
          </div>
        </form>
      ) : null}
      {publicKey ? (
        <div className="inf-keyblock" data-testid="inf-secret-public">
          <code className="inf-mono inf-code" data-copy="data">{publicKey}</code>
          <CopyButton text={publicKey} label="Copy key" />
        </div>
      ) : null}
      <ErrorLine error={loadError} />
      <div className="inf-table-wrap">
        <table className="inf-table" data-testid="inf-secrets-table">
          <thead><tr><th scope="col">Name</th><th scope="col">Kind</th><th scope="col">Backend</th>
            <th scope="col">Fingerprint</th><th scope="col">Bound to</th><th scope="col">Status</th><th scope="col"><span className="ag-sr">Verify</span></th><th scope="col"><span className="ag-sr">Access log</span></th></tr></thead>
          <tbody>
            {secrets.length === 0 ? <tr><td colSpan={8} className="inf-muted">No secrets yet</td></tr> : null}
            {secrets.map((s) => (
              <React.Fragment key={s.secret_ref_id}>
              <tr>
                <td className="inf-mono" data-copy="data">{s.name.split("/").pop()}</td>
                <td>{s.secret_kind}</td>
                <td className="inf-muted">{s.backend}</td>
                <td className="inf-mono inf-trunc" data-copy="data" title={s.fingerprint ?? undefined}>
                  {s.fingerprint ? `${s.fingerprint.slice(0, 18)}…` : s.last4 ? `…${s.last4}` : "—"}
                </td>
                <td className="inf-muted" data-copy="data">{s.bound_to.length ? s.bound_to.join(", ") : "—"}</td>
                <td>{verdict[s.secret_ref_id] ?? s.status}</td>
                <td>
                  <button type="button" className="ag-btn" disabled={busy !== null} data-testid={`inf-secret-verify-${s.secret_ref_id}`}
                          onClick={() => void verify(s)}>Verify</button>
                </td>
                <td>
                  <button type="button" className="ag-btn" aria-expanded={logFor === s.secret_ref_id}
                          data-testid={`inf-secret-log-${s.secret_ref_id}`} title="Who read it, and when"
                          onClick={() => void toggleLog(s)}>Access log</button>
                </td>
              </tr>
              {logFor === s.secret_ref_id ? (
                <tr data-testid="inf-secret-access-log">
                  <td colSpan={8}>
                    <ErrorLine error={logError} />
                    {!log && !logError ? <span className="inf-muted">Loading…</span> : null}
                    {log ? (
                      <>
                        <div className="inf-muted" data-testid="inf-secret-versions">
                          {log.version ? `Version ${log.version}` : "Version —"}
                          {log.versions.length > 1 ? ` · ${log.versions.length} versions` : ""}
                        </div>
                        {log.events.length === 0 ? <div className="inf-muted">Not read yet</div> : (
                          <table className="inf-table">
                            <thead><tr><th scope="col">When</th><th scope="col">Result</th><th scope="col">Run</th>
                              <th scope="col">Person</th><th scope="col">Agent</th><th scope="col">Version</th></tr></thead>
                            <tbody>
                              {log.events.map((e) => (
                                <tr key={e.id} data-testid="inf-secret-access-row">
                                  <td className="inf-muted">{e.accessed_at ? new Date(e.accessed_at).toLocaleString() : "—"}</td>
                                  <td title={e.reason ?? undefined}>{e.outcome === "resolved" ? "Read" : "Refused"}</td>
                                  <td className="inf-mono" data-copy="data">{e.run_id ?? "—"}</td>
                                  <td className="inf-mono">{e.person_id ?? "—"}</td>
                                  <td>{e.agent_role ?? "—"}</td>
                                  <td>{e.version ?? "—"}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                      </>
                    ) : null}
                  </td>
                </tr>
              ) : null}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </Sheet>
  );
}
