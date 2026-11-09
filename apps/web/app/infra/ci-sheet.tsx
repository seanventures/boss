"use client";

/**
 * Deploys `⋯ › CI config` (spec §4.6; parity P-I-54..56): the old CICDPipelinesPanel's whole
 * capability, whether the API answered, the vendor cards, Generate (a dry run: nothing is
 * written to any repository), Validate, the YAML editor, and BOSS's own record of pipeline runs.
 */
import { GatedButton } from "../../components/gated";
import React, { useEffect, useMemo, useState } from "react";

import { generateCi, loadCi, validateCi, type CiState } from "./deploy-actions";
import { ErrorLine, ago, localTime, useNow } from "./infra-util";
import { Sheet } from "./kit";

export function CiSheet({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const [ci, setCi] = useState<CiState | null>(null);
  const [vendor, setVendor] = useState("github_actions");
  const [language, setLanguage] = useState("python");
  const [framework, setFramework] = useState("");
  const [yaml, setYaml] = useState("");
  const [path, setPath] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<{ valid: boolean; errors: string[]; warnings: string[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const now = useNow(60_000);

  useEffect(() => {
    void loadCi(projectId).then((s) => { setCi(s); if (s.vendors[0]) setVendor((v) => (s.vendors.some((x) => x.id === v) ? v : s.vendors[0].id)); });
  }, [projectId]);

  const languages = useMemo(() => {
    const v = ci?.vendors.find((x) => x.id === vendor);
    return v?.languages?.length ? v.languages : ["python", "typescript", "go"];
  }, [ci, vendor]);

  const generate = async () => {
    setBusy("gen"); setError(null); setVerdict(null);
    const r = await generateCi({ vendor, language, ...(framework.trim() ? { framework: framework.trim() } : {}) });
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    setYaml(r.data.content ?? ""); setPath(r.data.path ?? null);
  };
  const validate = async () => {
    setBusy("val"); setError(null);
    const r = await validateCi({ yaml, vendor });
    setBusy(null);
    if (!r.ok) { setError(r.error); return; }
    setVerdict({ valid: r.data.valid === true, errors: r.data.errors ?? [], warnings: r.data.warnings ?? [] });
  };

  return (
    <Sheet title="CI config" testId="inf-ci-sheet" onClose={onClose}>
      <div className="inf-actions">
        <span className="inf-state" data-tone={ci?.reachable ? "green" : "amber"} data-testid="inf-ci-api"
              title="Whether the CI catalog answered">
          <span className="inf-dot" data-tone={ci?.reachable ? "green" : "amber"} aria-hidden />
          <span className="inf-state-word">{ci === null ? "Checking…" : ci.reachable ? "API reachable" : "API offline"}</span>
        </span>
        <span className="inf-faint" title="Nothing is written to a repository or a CI provider">Dry run</span>
      </div>

      <div className="inf-ci-vendors" data-testid="inf-ci-vendor" role="radiogroup" aria-label="Vendor">
        {(ci?.vendors ?? []).length === 0 ? <span className="inf-muted">No vendors listed</span> : null}
        {(ci?.vendors ?? []).map((v) => (
          <button key={v.id} type="button" role="radio" aria-checked={vendor === v.id} className="inf-ci-card"
                  data-testid={`inf-ci-vendor-${v.id}`} onClick={() => setVendor(v.id)}>
            <strong>{v.name}</strong>
            {v.config_path ? <code className="inf-mono inf-trunc" data-copy="data">{v.config_path}</code> : null}
            <span className="inf-faint">{v.live_adapter ? "Live adapter" : "Template only"}</span>
          </button>
        ))}
      </div>

      <form className="inf-form" onSubmit={(e) => { e.preventDefault(); void generate(); }}>
        <div className="inf-form-row">
          <label className="inf-field"><span>Vendor</span>
            <select value={vendor} onChange={(e) => setVendor(e.target.value)}>
              {(ci?.vendors.length ? ci.vendors : [{ id: "github_actions", name: "GitHub Actions" }]).map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select></label>
          <label className="inf-field"><span>Language</span>
            <select value={language} onChange={(e) => setLanguage(e.target.value)}>
              {languages.map((l) => <option key={l} value={l}>{l}</option>)}
            </select></label>
          <label className="inf-field"><span>Framework</span>
            <input value={framework} onChange={(e) => setFramework(e.target.value)} placeholder="Optional" autoComplete="off" /></label>
        </div>
        <div className="inf-actions">
          <GatedButton permission="cicd:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy !== null} data-testid="inf-ci-generate">
            {busy === "gen" ? "Generating…" : "Generate"}</GatedButton>
          <button type="button" className="ag-btn" disabled={busy !== null || !yaml.trim()} data-testid="inf-ci-validate"
                  onClick={() => void validate()}>{busy === "val" ? "Validating…" : "Validate"}</button>
          {path ? <code className="inf-mono inf-faint" data-copy="data">{path}</code> : null}
          <ErrorLine error={error} testId="inf-ci-error" />
        </div>
        <label className="inf-field"><span>YAML</span>
          <textarea rows={10} value={yaml} onChange={(e) => { setYaml(e.target.value); setVerdict(null); }} spellCheck={false}
                    data-testid="inf-ci-yaml" placeholder="Generate, or paste a config" /></label>
        {verdict ? (
          <div data-testid="inf-ci-verdict">
            <span className="inf-state" data-tone={verdict.valid ? "green" : "red"}>
              <span className="inf-dot" data-tone={verdict.valid ? "green" : "red"} aria-hidden />
              <span className="inf-state-word">{verdict.valid ? "Valid" : "Invalid"}</span>
            </span>
            {verdict.errors.length || verdict.warnings.length ? (
              <ul className="inf-dchecks">
                {verdict.errors.map((e) => <li key={`e-${e}`} data-ok="0"><span aria-label="Error">✗</span><span data-copy="data">{e}</span></li>)}
                {verdict.warnings.map((w) => <li key={`w-${w}`} data-ok="warn"><span aria-label="Warning">!</span><span data-copy="data">{w}</span></li>)}
              </ul>
            ) : null}
          </div>
        ) : null}
      </form>

      <div className="inf-table-wrap">
        <table className="inf-table" data-testid="inf-ci-runs">
          <caption className="inf-caption">Pipeline runs{ci?.total !== null && ci?.total !== undefined ? `, ${ci.total}` : ""}</caption>
          <thead><tr><th scope="col">Vendor</th><th scope="col">Status</th><th scope="col">Repo / ref</th><th scope="col">External</th><th scope="col">Recorded</th></tr></thead>
          <tbody>
            {(ci?.runs ?? []).length === 0 ? <tr><td colSpan={5} className="inf-muted">No runs recorded</td></tr> : null}
            {(ci?.runs ?? []).map((r) => (
              <tr key={r.id}>
                <td>{r.vendor}</td>
                <td>{r.status}</td>
                <td className="inf-trunc" data-copy="data">{r.repository || "—"}{r.ref ? ` @ ${r.ref}` : ""}</td>
                <td className="inf-muted" data-copy="data">{r.url ? <a className="inf-link" href={r.url} target="_blank" rel="noreferrer">{r.external_id || "Open ↗"}</a> : (r.external_id || "—")}</td>
                <td className="inf-num inf-faint" title={r.recorded_at ? localTime(r.recorded_at) : undefined}>{ago(r.recorded_at, now) || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Sheet>
  );
}
