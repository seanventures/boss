"use client";

/**
 * Deploys `⋯ › Rules` (spec §4.6; parity P-S-37): environment policies — which runtime mode
 * each environment allows, which target kinds, and which gates a provider-backed deploy must
 * pass — and the list of policies saved. The old Deployment Registry's `Save environment
 * policy`, with the gates said in plain words.
 */
import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useState } from "react";

import { showToast } from "../client-forms";
import { listPolicies, type Policy } from "./deploy-actions";
import { afterWrite, saveEnvironmentPolicy } from "./infra-actions";
import { ErrorLine } from "./infra-util";
import { Sheet } from "./kit";

const MODES = [
  { key: "metadata_only", label: "Metadata only" }, { key: "preview_allowed", label: "Preview allowed" },
  { key: "staging_allowed", label: "Staging allowed" }, { key: "production_allowed", label: "Production allowed" },
  { key: "disabled", label: "Disabled" },
];
const GATES: { key: keyof Policy & string; label: string; tip: string }[] = [
  { key: "requires_change_set", label: "Needs a change", tip: "A reviewed change in Cloud before it applies" },
  { key: "requires_budget_reservation", label: "Needs a budget hold", tip: "Spend is reserved before it runs" },
  { key: "requires_approval", label: "Needs approval", tip: "On: a person approves each deploy. Off: no asking, unless its step promised you" },
  { key: "requires_health_check", label: "Needs health check", tip: "It is live only after its check passes" },
  { key: "requires_rollback_plan", label: "Needs rollback plan", tip: "A way back is recorded first" },
];

export function RulesSheet({ projectId, actor, onClose, canWrite = true }: { projectId: string; actor: string; onClose: () => void; canWrite?: boolean }) {
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [env, setEnv] = useState("staging");
  const [mode, setMode] = useState("metadata_only");
  const [kinds, setKinds] = useState("");
  const [gates, setGates] = useState<Record<string, boolean>>(Object.fromEntries(GATES.map((g) => [g.key, true])));
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => {
    void listPolicies(projectId).then((p) => { if (typeof p === "string") setLoadError(p); else { setPolicies(p); setLoadError(null); } });
  }, [projectId]);
  useEffect(() => { reload(); }, [reload]);

  const save = async () => {
    setBusy(true); setError(null);
    const r = await saveEnvironmentPolicy(projectId, actor, {
      environment: env, runtime_mode: mode,
      allowed_target_kinds: kinds.split(/\n|,/).map((s) => s.trim()).filter(Boolean),
      ...gates, rollout_notes: notes.trim() || undefined,
    });
    setBusy(false);
    if (!r.ok) { setError(r.error); return; }
    showToast("success", "Rule saved");
    afterWrite(reload);
  };

  return (
    <Sheet title="Rules" testId="inf-rules-sheet" onClose={onClose}>
      {canWrite ? (
        <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <div className="inf-form-row">
            <label className="inf-field"><span>Environment</span>
              <select value={env} onChange={(e) => setEnv(e.target.value)} data-testid="inf-rules-env">
                <option value="staging">Staging</option><option value="production">Production</option><option value="preview">Preview</option>
              </select></label>
            <label className="inf-field"><span>Mode</span>
              <select value={mode} onChange={(e) => setMode(e.target.value)} data-testid="inf-rules-mode">
                {MODES.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
              </select></label>
          </div>
          <label className="inf-field" title="One per line. Empty allows every kind."><span>Allowed kinds</span>
            <textarea rows={2} value={kinds} onChange={(e) => setKinds(e.target.value)} placeholder="digitalocean_app" data-testid="inf-rules-kinds" /></label>
          <fieldset className="inf-dchecks-row">
            <legend className="ag-sr">Gates</legend>
            {GATES.map((g) => (
              <label key={g.key} className="inf-check" title={g.tip}>
                <input type="checkbox" checked={!!gates[g.key]} onChange={(e) => setGates((x) => ({ ...x, [g.key]: e.target.checked }))} />
                {g.label}
              </label>
            ))}
          </fieldset>
          <label className="inf-field"><span>Notes</span>
            <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} data-testid="inf-rules-notes" /></label>
          <div className="inf-actions">
            <GatedButton permission="deployment_target:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-rules-save">{busy ? "Saving…" : "Save"}</GatedButton>
            <ErrorLine error={error} />
          </div>
        </form>
      ) : null}
      <ErrorLine error={loadError} />
      <div className="inf-table-wrap">
        <table className="inf-table" data-testid="inf-rules-list">
          <thead><tr><th scope="col">Environment</th><th scope="col">Mode</th><th scope="col">Kinds</th><th scope="col">Gates</th><th scope="col">State</th></tr></thead>
          <tbody>
            {policies.length === 0 ? <tr><td colSpan={5} className="inf-muted">No rules yet</td></tr> : null}
            {policies.map((p) => (
              <tr key={p.id}>
                <td>{p.environment}</td>
                <td>{MODES.find((m) => m.key === p.runtime_mode)?.label ?? p.runtime_mode}</td>
                <td className="inf-muted" data-copy="data">{p.allowed_target_kinds?.length ? p.allowed_target_kinds.join(", ") : "All"}</td>
                <td className="inf-muted" title={GATES.filter((g) => p[g.key]).map((g) => g.label).join(", ") || "None"}>
                  {GATES.filter((g) => p[g.key]).length} of {GATES.length}
                </td>
                <td>{p.status ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Sheet>
  );
}
