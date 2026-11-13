"use client";

/**
 * Import › From a source (BOSSAPP-841): a SKILL.md from GitHub, skills.sh or a URL, pinned to the
 * commit you previewed. Preview fetches and parses without writing; Import creates the skill as
 * version 1 and keeps the source, so a later upstream change arrives as a new version (applied by
 * itself only with Auto-update on and the skill reviewed; otherwise it waits for your approval).
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { apiWrite } from "../../lib/api-write";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";

const KINDS = [
  { id: "github", label: "GitHub", placeholder: "owner/repo/path/to/skill" },
  { id: "skillsh", label: "skills.sh", placeholder: "owner/repo/skill" },
  { id: "url", label: "URL", placeholder: "https://example.com/SKILL.md" },
];

type Preview = { sha: string; files: string[]; skill: { skill_key: string; label: string; summary: string; knowledge: string } };

export function SourceImport({ projectId, actor, onImported }: {
  projectId: string; actor: string; onImported: () => void;
}) {
  const [kind, setKind] = useState("github");
  const [locator, setLocator] = useState("");
  const [ref, setRef] = useState("");
  const [pin, setPin] = useState("");
  const [auto, setAuto] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async (action: "preview_source" | "import_source") => {
    setBusy(true); setError(null);
    const r = await apiWrite("/api/skill-supply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, project_id: projectId, actor, kind, locator: locator.trim(), ref: ref.trim(),
                             pin: action === "import_source" ? (preview?.sha ?? pin.trim()) : pin.trim(),
                             skill_key: preview?.skill.skill_key, auto_update: auto }),
    });
    const data = await r.json().catch(() => ({}));
    setBusy(false);
    if (!r.ok) { setError(String(data?.error ?? `HTTP ${r.status}`)); return; }
    if (action === "preview_source") { setPreview(data as Preview); return; }
    showToast("success", `Imported ${(data as { skill?: { label?: string } }).skill?.label ?? "skill"}`);
    onImported();
  };

  const shown = KINDS.find((k) => k.id === kind) ?? KINDS[0];
  return (
    <div data-testid="kit-import-source">
      <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Source">
        {KINDS.map((k) => (
          <button key={k.id} type="button" role="radio" aria-checked={kind === k.id} data-testid={`kit-source-kind-${k.id}`}
                  onClick={() => { setKind(k.id); setPreview(null); }}>{k.label}</button>
        ))}
      </span>
      <label className="kit-field">
        <span className="kit-field-label">Where</span>
        <input type="text" value={locator} spellCheck={false} placeholder={shown.placeholder} data-testid="kit-source-locator"
               onChange={(e) => { setLocator(e.target.value); setPreview(null); }} />
      </label>
      {kind !== "url" ? (
        <div className="kit-row">
          <label className="kit-field">
            <span className="kit-field-label" data-tip="Branch or tag to follow">Ref</span>
            <input type="text" value={ref} spellCheck={false} placeholder="main" data-testid="kit-source-ref"
                   onChange={(e) => { setRef(e.target.value); setPreview(null); }} />
          </label>
          <label className="kit-field">
            <span className="kit-field-label" data-tip="A commit sha; empty pins what Preview fetched">Pin</span>
            <input type="text" value={pin} spellCheck={false} placeholder="commit sha" data-testid="kit-source-pin"
                   onChange={(e) => { setPin(e.target.value); setPreview(null); }} />
          </label>
        </div>
      ) : null}
      <div className="kit-field">
        <span className="kit-field-label" data-tip="Apply upstream changes by itself — only once the skill is reviewed">Auto-update</span>
        <button type="button" role="switch" aria-checked={auto} className="kit-switch" aria-label="Auto-update"
                data-testid="kit-source-auto" onClick={() => setAuto((a) => !a)} />
      </div>
      {preview ? (
        <div data-testid="kit-source-preview">
          <div className="kit-row">
            <strong data-user-text>{preview.skill.label}</strong>
            <span className="kit-chip" data-tip={preview.sha}>@{preview.sha.slice(0, 12)}</span>
            {preview.files.length ? <span className="kit-faint">+{preview.files.length} files</span> : null}
          </div>
          <div className="kit-faint" data-user-text>{preview.skill.summary}</div>
          <pre className="kit-diff" data-user-text>{preview.skill.knowledge.slice(0, 2000)}</pre>
        </div>
      ) : null}
      {error ? <div className="kit-err" role="alert" data-tip={error}>{shortError(error)}</div> : null}
      <footer className="kit-modal-foot">
        <GatedButton permission="skill:manage" type="button" className="kit-btn" disabled={busy || !locator.trim()}
                     data-testid="kit-source-preview-btn" onClick={() => void send("preview_source")}>Preview</GatedButton>
        <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-primary" disabled={busy || !preview}
                     data-testid="kit-source-import-btn" onClick={() => void send("import_source")}>Import</GatedButton>
      </footer>
    </div>
  );
}
