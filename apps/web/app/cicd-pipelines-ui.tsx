"use client";

import React, { useCallback, useMemo, useState, type CSSProperties } from "react";
import { formatDateTime } from "../lib/datetime";
import {
  type CICDCatalog,
  type CICDGenerateResult,
  type CICDRun,
  type CICDStatus,
  type CICDValidateResult,
  generateCICDConfig,
  validateCICDYaml,
} from "../lib/cicd-pipelines";

const card: CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 12,
  padding: 14,
  background: "var(--surface)",
};

const inputStyle: CSSProperties = {
  background: "var(--surface-3)",
  border: "1px solid var(--border)",
  color: "var(--ink)",
  borderRadius: 8,
  padding: "6px 10px",
  fontSize: 12.5,
  minWidth: 140,
};

const primaryBtn: CSSProperties = {
  background: "var(--accent)",
  color: "var(--accent-ink)",
  border: "none",
  borderRadius: 8,
  padding: "7px 14px",
  fontSize: 12.5,
  fontWeight: 600,
  cursor: "pointer",
};

const mutedBtn: CSSProperties = {
  ...primaryBtn,
  background: "var(--surface-3)",
  color: "var(--ink)",
  border: "1px solid var(--border)",
};

function tone(status: string): string {
  const s = status.toLowerCase();
  if (s === "passed") return "#3fb950";
  if (s === "failed") return "#f85149";
  if (s === "running" || s === "queued") return "#d29922";
  if (s === "cancelled" || s === "skipped") return "#8b949e";
  return "var(--muted)";
}

export type CICDPipelinesPanelProps = {
  projectId: string;
  catalog?: CICDCatalog | null;
  status?: CICDStatus | null;
  initialRuns?: CICDRun[];
};

export function CICDPipelinesPanel({
  projectId,
  catalog,
  status,
  initialRuns = [],
}: CICDPipelinesPanelProps) {
  const vendors = catalog?.vendors?.length
    ? catalog.vendors
    : status?.vendors ?? [];
  const recentRuns = status?.recent_runs?.length
    ? status.recent_runs
    : initialRuns;

  const [vendor, setVendor] = useState(vendors[0]?.id ?? "github_actions");
  const [language, setLanguage] = useState("python");
  const [framework, setFramework] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generated, setGenerated] = useState<CICDGenerateResult | null>(null);
  const [validation, setValidation] = useState<CICDValidateResult | null>(null);
  const [yamlText, setYamlText] = useState("");

  const languages = useMemo(() => {
    const v = vendors.find((item) => item.id === vendor);
    return v?.languages?.length ? v.languages : ["python", "typescript", "go"];
  }, [vendors, vendor]);

  const onGenerate = useCallback(async () => {
    setBusy(true);
    setError(null);
    setValidation(null);
    const result = await generateCICDConfig({
      vendor,
      language,
      framework: framework.trim() || undefined,
    });
    setBusy(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setGenerated(result.result);
    setYamlText(result.result.content);
  }, [vendor, language, framework]);

  const onValidate = useCallback(async () => {
    setBusy(true);
    setError(null);
    const result = await validateCICDYaml({
      yaml: yamlText || generated?.content || "",
      vendor,
    });
    setBusy(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setValidation(result.result);
  }, [yamlText, generated, vendor]);

  const apiOk = catalog?.apiReachable || status?.apiReachable;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          Multi-vendor CI config (dry-run) + BOSS-side pipeline provenance.
          Live cloud writes stay adapter-gated.
        </span>
        <span
          style={{
            fontSize: 11,
            padding: "2px 8px",
            borderRadius: 999,
            border: `1px solid ${apiOk ? "#3fb950" : "#d29922"}`,
            color: apiOk ? "#3fb950" : "#d29922",
          }}
        >
          {apiOk ? "API reachable" : "API offline / empty"}
        </span>
      </div>

      {/* Vendor catalog */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
          gap: 10,
        }}
      >
        {vendors.length === 0 ? (
          <div style={{ ...card, color: "var(--muted)", fontSize: 12.5 }}>
            The pipeline vendor catalog did not load — the BOSS API did not answer for it.
            Reload the page; if it stays empty, check that the API is running in Repo and models.
          </div>
        ) : (
          vendors.map((v) => (
            <button
              key={v.id}
              type="button"
              onClick={() => setVendor(v.id)}
              style={{
                ...card,
                textAlign: "left",
                cursor: "pointer",
                borderColor: vendor === v.id ? "var(--accent)" : "var(--border)",
                boxShadow: vendor === v.id ? "0 0 0 1px var(--accent)" : undefined,
              }}
            >
              <div style={{ fontWeight: 600, fontSize: 13, color: "var(--ink)" }}>
                {v.name}
              </div>
              <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 4 }}>
                {v.config_path}
              </div>
              <div style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                <span style={{ fontSize: 10, color: "#3fb950" }}>template</span>
                <span
                  style={{
                    fontSize: 10,
                    color: v.live_adapter ? "#818cf8" : "var(--muted)",
                  }}
                >
                  {v.live_adapter ? "live adapter" : "template only"}
                </span>
              </div>
            </button>
          ))
        )}
      </div>

      {/* Generate + validate */}
      <div style={{ ...card, display: "grid", gap: 12 }}>
        <div style={{ fontWeight: 600, fontSize: 13.5, color: "var(--ink)" }}>
          Generate config (dry-run)
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "end" }}>
          <label style={{ display: "grid", gap: 4, fontSize: 11, color: "var(--muted)" }}>
            Vendor
            <select
              value={vendor}
              onChange={(e) => setVendor(e.target.value)}
              style={inputStyle}
            >
              {(vendors.length ? vendors : [{ id: "github_actions", name: "GitHub Actions" }]).map(
                (v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                  </option>
                ),
              )}
            </select>
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 11, color: "var(--muted)" }}>
            Language
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
              style={inputStyle}
            >
              {languages.map((lang) => (
                <option key={lang} value={lang}>
                  {lang}
                </option>
              ))}
            </select>
          </label>
          <label style={{ display: "grid", gap: 4, fontSize: 11, color: "var(--muted)" }}>
            Framework (optional)
            <input
              value={framework}
              onChange={(e) => setFramework(e.target.value)}
              placeholder="nextjs, react…"
              style={inputStyle}
            />
          </label>
          <button type="button" style={primaryBtn} disabled={busy} onClick={onGenerate}>
            {busy ? "Working…" : "Generate"}
          </button>
          <button type="button" style={mutedBtn} disabled={busy || !yamlText} onClick={onValidate}>
            Validate YAML
          </button>
        </div>

        {error ? (
          <div style={{ fontSize: 12, color: "#f85149", whiteSpace: "pre-wrap" }}>{error}</div>
        ) : null}

        {generated ? (
          <div style={{ display: "grid", gap: 8 }}>
            <div style={{ fontSize: 12, color: "var(--muted)" }}>
              Path <code style={{ color: "var(--accent-text)" }}>{generated.path}</code>
              {" · "}checksum <code>{generated.checksum.slice(0, 12)}…</code>
            </div>
            <textarea
              value={yamlText}
              onChange={(e) => setYamlText(e.target.value)}
              rows={14}
              spellCheck={false}
              style={{
                ...inputStyle,
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                fontSize: 11.5,
                width: "100%",
                minWidth: 0,
                resize: "vertical",
              }}
            />
          </div>
        ) : null}

        {validation ? (
          <div
            style={{
              fontSize: 12.5,
              color: validation.valid ? "#3fb950" : "#f85149",
              borderTop: "1px solid var(--border)",
              paddingTop: 10,
            }}
          >
            {validation.valid ? "Valid structure" : "Invalid structure"}
            {validation.errors?.length ? (
              <ul style={{ margin: "6px 0 0", paddingLeft: 18, color: "#f85149" }}>
                {validation.errors.map((e) => (
                  <li key={e}>{e}</li>
                ))}
              </ul>
            ) : null}
            {validation.warnings?.length ? (
              <ul style={{ margin: "6px 0 0", paddingLeft: 18, color: "#d29922" }}>
                {validation.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Provenance ledger */}
      <div style={{ ...card, display: "grid", gap: 10 }}>
        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
          <div style={{ fontWeight: 600, fontSize: 13.5, color: "var(--ink)" }}>
            Pipeline run provenance
          </div>
          <div style={{ fontSize: 11.5, color: "var(--muted)" }}>
            Project {projectId}
            {status?.run_counts?.total_listed != null
              ? ` · ${status.run_counts.total_listed} recorded`
              : ""}
          </div>
        </div>
        <p style={{ margin: 0, fontSize: 12, color: "var(--muted)", lineHeight: 1.5 }}>
          BOSS&apos;s own record of pipeline runs it has been told about. Not live cloud polling —
          that requires pipeline adapters + tokens. Links to delivery and deployments when
          recorded with delivery_run_id / deployment_run_id.
        </p>
        {recentRuns.length === 0 ? (
          <div style={{ fontSize: 12.5, color: "var(--muted)" }}>
            No runs recorded yet. Operators or webhooks can POST provenance without cloud writes.
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ color: "var(--muted)", textAlign: "left" }}>
                  <th style={{ padding: "6px 8px" }}>Vendor</th>
                  <th style={{ padding: "6px 8px" }}>Status</th>
                  <th style={{ padding: "6px 8px" }}>Repo / ref</th>
                  <th style={{ padding: "6px 8px" }}>External</th>
                  <th style={{ padding: "6px 8px" }}>Recorded</th>
                </tr>
              </thead>
              <tbody>
                {recentRuns.map((run) => (
                  <tr key={run.id} style={{ borderTop: "1px solid var(--border)" }}>
                    <td style={{ padding: "6px 8px", color: "var(--ink)" }}>{run.vendor}</td>
                    <td style={{ padding: "6px 8px", color: tone(run.status) }}>{run.status}</td>
                    <td style={{ padding: "6px 8px", color: "var(--ink-soft)" }}>
                      {run.repository || "—"}
                      {run.ref ? ` @ ${run.ref}` : ""}
                    </td>
                    <td style={{ padding: "6px 8px", color: "var(--muted)" }}>
                      {run.external_id || "—"}
                    </td>
                    <td style={{ padding: "6px 8px", color: "var(--muted)" }}>
                      {formatDateTime(run.recorded_at, "—")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
