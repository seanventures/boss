"use client";

/**
 * PUBLISH TO MARKETPLACE, from a workflow under Yours (the Library card's ⋯ menu, the editor's).
 *
 *     "When people build the workflow, they can publish to the Marketplace and they can be
 *      installed into the app using Install button."
 *
 * Pack name, one line, version (the next one, prefilled), and who can install: this BOSS install
 * (every project in the org). The community marketplace is shown, greyed, for later. Confirmed
 * here on the page. Publishing again is the next version.
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useState } from "react";

import { useCan } from "../../components/identity/identity-provider";
import { nextVersion, publishPack } from "../../lib/workflow-api";
import type { PublishedPack } from "../../lib/workflow-editor-types";

export function PublishForm({ projectId, definitionId, workflowName, description, published, blocked,
                              onDone, onCancel }: {
  projectId: string;
  definitionId: string;
  workflowName: string;
  description?: string;
  /** What is published from it already: the form publishes the next version. */
  published?: PublishedPack | null;
  /** Why publishing is not possible now (no permission, unsaved changes). */
  blocked?: string | null;
  onDone: (pack: PublishedPack | null) => void;
  onCancel: () => void;
}) {
  const gate = useCan("workflow:publish");
  blocked = blocked || (gate.allowed ? null : gate.reason);
  const again = published?.status === "published" ? published : null;
  const [name, setName] = useState(published?.name || workflowName);
  const [summary, setSummary] = useState(published?.summary || description || "");
  const [version, setVersion] = useState(published ? nextVersion(published.version) : "1.0");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const badVersion = !/^\d{1,4}(\.\d{1,4}){0,2}$/.test(version.trim());

  const publish = async () => {
    if (busy || blocked || badVersion || !name.trim()) return;
    setBusy(true);
    setError("");
    try {
      onDone(await publishPack(projectId, definitionId,
                               { name: name.trim(), summary: summary.trim(), version: version.trim() }));
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="wf-publish" data-testid="wf-publish" aria-label={`Publish ${workflowName}`}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}
          onSubmit={(e) => { e.preventDefault(); void publish(); }}>
      <strong className="wf-publish-title">Publish “<span data-user-text>{workflowName}</span>”</strong>
      <label className="wf-field">
        <span>Pack name</span>
        <input value={name} autoFocus data-testid="wf-publish-name" maxLength={120}
               onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="wf-field">
        <span>One line</span>
        <input value={summary} data-testid="wf-publish-summary" maxLength={300}
               onChange={(e) => setSummary(e.target.value)} />
      </label>
      <label className="wf-field">
        <span title={again ? `Published v${again.version}. A later version publishes again.` : undefined}>Version</span>
        <input value={version} data-testid="wf-publish-version" inputMode="decimal" maxLength={14}
               aria-invalid={badVersion || undefined}
               onChange={(e) => setVersion(e.target.value)} />
      </label>
      <fieldset className="wf-publish-who">
        <legend>Who can install</legend>
        <label className="wf-publish-radio">
          <input type="radio" name="wf-publish-who" checked readOnly data-testid="wf-publish-org" />
          <span>This BOSS install (your org)</span>
        </label>
        <label className="wf-publish-radio" data-off="true">
          <input type="radio" name="wf-publish-who" disabled data-testid="wf-publish-community" />
          <span>Community marketplace — later
            <i>Needs a hosted registry, review and signing. First step: export / import a pack file.</i>
          </span>
        </label>
      </fieldset>
      {error ? <p className="wf-publish-error" role="alert" data-testid="wf-publish-error">{error}</p> : null}
      <div className="wf-publish-actions">
        <button type="submit" className="wf-btn wf-btn-primary" data-testid="wf-publish-submit"
                disabled={busy || Boolean(blocked) || badVersion || !name.trim()}
                title={blocked ?? (badVersion ? "Numbers and dots, like 1.0" : undefined)}>
          {busy ? "Publishing…" : "Publish"}
        </button>
        <button type="button" className="wf-btn" data-testid="wf-publish-cancel" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
