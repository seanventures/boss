"use client";

/**
 * Library › ⋯ › Import: a skill as a capability-bundle YAML (the same import Access and packs
 * had), or MCP servers as JSON (`{"mcpServers": {"name": {"url": …}}}`, the shape editors use).
 * Imported servers arrive unreviewed and without tokens — a token is typed in its drawer.
 */
import React, { useState } from "react";

import { callAction } from "../../lib/call-action";
import { mcpApi } from "../../lib/kit-api";
import { importSkillAction } from "../actions";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";

export function ImportSheet({ projectId, actor, onClose, onImported }: {
  projectId: string; actor: string; onClose: () => void; onImported: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true); setError(null);
    const body = text.trim();
    try {
      if (body.startsWith("{")) {
        const parsed = JSON.parse(body) as { mcpServers?: Record<string, { url?: string; risk_tier?: number }> };
        const entries = Object.entries(parsed.mcpServers ?? {});
        if (!entries.length) throw new Error("No mcpServers in that JSON");
        for (const [name, spec] of entries) {
          const r = await mcpApi.register(projectId, actor, { name, url: String(spec.url ?? ""),
                                                             risk_tier: Number(spec.risk_tier ?? 1) });
          if (!r.ok) throw new Error(`${name}: ${r.error}`);
        }
        showToast("success", `Saved · ${entries.length} servers`);
      } else {
        const form = new FormData();
        form.set("project_id", projectId); form.set("actor", actor); form.set("yaml", body);
        const result = await callAction(importSkillAction)(form);
        if (!result.ok) throw new Error(result.error ?? "Import failed");
        showToast("success", "Saved");
      }
      onImported();
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="kit-modal" role="dialog" aria-label="Import" data-testid="kit-import">
      <div className="kit-modal-scrim" onClick={onClose} />
      <div className="kit-modal-card">
        <header className="kit-modal-head">
          <h3 data-tip="A skill YAML bundle, or MCP servers as JSON">Import</h3>
          <button type="button" className="kit-icon-btn" aria-label="Close" onClick={onClose}>×</button>
        </header>
        <textarea rows={10} value={text} spellCheck={false} aria-label="YAML or JSON" data-testid="kit-import-text"
                  placeholder={'format: boss.capability-bundle/v1\n…  or  {"mcpServers": {…}}'}
                  onChange={(e) => setText(e.target.value)} />
        {error ? <div className="kit-err" role="alert" data-tip={error}>{shortError(error)}</div> : null}
        <footer className="kit-modal-foot">
          <button type="button" className="kit-btn kit-btn-primary" disabled={busy || !text.trim()}
                  data-testid="kit-import-run" onClick={() => void run()}>Import</button>
        </footer>
      </div>
    </div>
  );
}
