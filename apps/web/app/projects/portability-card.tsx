"use client";

/**
 * PROJECT › SETTINGS › MOVE AND RECOVER (BOSSAPP-836).
 *
 * Export this project (secrets shown only as empty placeholders), check an import, then apply
 * it. Backup health is the install's own backups. Roll back an upgrade with `boss rollback` on
 * the host — the API has no Docker socket, so the console does not pretend to run it.
 */
import React, { useEffect, useState } from "react";
import { ArrowSquareOut, ClockCounterClockwise, DownloadSimple, Package } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError, refusalSentence } from "../../lib/identity/api";
import {
  backupHealth, importSummary, rollbackCommand, secretLeaks, secretPlaceholders,
  type BackupHealth, type SecretPlaceholder,
} from "../../lib/portability-console";
import { applyImport, exportProject, planImport, readBackups } from "./portability-actions";
import { ConfirmSheet } from "./confirm-sheet";
import { Ico } from "./kit";

const ROW: React.CSSProperties = { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" };

function downloadBundle(bundle: unknown, slug: string) {
  const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${slug || "project"}.boss.json`;
  link.click();
  URL.revokeObjectURL(url);
}

export function PortabilityCard({ projectId, actor }: { projectId: string; actor: string }) {
  const guarded = useGuardedCall();
  const [health, setHealth] = useState<BackupHealth | null>(null);
  const [bundle, setBundle] = useState<unknown>(null);
  const [placeholders, setPlaceholders] = useState<SecretPlaceholder[] | null>(null);
  const [leaks, setLeaks] = useState<string[]>([]);
  const [exportSaid, setExportSaid] = useState("");
  const [raw, setRaw] = useState("");
  const [plan, setPlan] = useState("");
  const [importId, setImportId] = useState<string | null>(null);
  const [canApply, setCanApply] = useState(false);
  const [imported, setImported] = useState("");
  const [restore, setRestore] = useState("");
  const [askRollback, setAskRollback] = useState(false);
  const [command, setCommand] = useState("");

  useEffect(() => {
    let live = true;
    readBackups()
      .then((body) => { if (live) setHealth(backupHealth(body?.backups)); })
      .catch(() => { if (live) setHealth({ state: "pending", sentence: "Backups could not be read." }); });
    return () => { live = false; };
  }, []);

  const onExport = async () => {
    setExportSaid("");
    setBundle(null);
    setPlaceholders(null);
    setLeaks([]);
    try {
      const body = await guarded("Export this project", () => exportProject(projectId, actor));
      const found = secretLeaks(body);
      setLeaks(found);
      if (found.length) {
        setExportSaid("This export still has a secret. It was not saved.");
        return;
      }
      setBundle(body);
      setPlaceholders(secretPlaceholders(body));
    } catch (error) {
      setExportSaid(error instanceof IdentityError ? refusalSentence(error) : "Export did not answer.");
    }
  };

  const onCheck = async () => {
    setPlan("");
    setImportId(null);
    setCanApply(false);
    setImported("");
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { setPlan("Not valid JSON."); return; }
    if (secretLeaks(parsed).length) {
      setPlan("This bundle still has a secret. Nothing was sent.");
      return;
    }
    try {
      const body = await guarded("Check this import", () => planImport(projectId, actor, parsed));
      const errors = Array.isArray(body.errors) ? body.errors.length : 0;
      setPlan(errors ? `${importSummary(body.summary)}. ${errors} errors — not applied.` : importSummary(body.summary));
      setImportId(body.import_id ?? null);
      setCanApply(!errors && !!body.import_id);
    } catch (error) {
      setPlan(error instanceof IdentityError ? refusalSentence(error) : "The check did not answer.");
    }
  };

  const onApply = async () => {
    if (!importId) return;
    setImported("");
    try {
      const body = await guarded("Import this project", () => applyImport(projectId, actor, importId));
      setImported(body.status === "applied" ? "Imported." : `Import ${body.status ?? "stopped"}.`);
      setCanApply(body.status !== "applied");
    } catch (error) {
      setImported(error instanceof IdentityError ? refusalSentence(error) : "Import did not answer.");
    }
  };

  const commandNow = rollbackCommand(restore);

  return (
    <section className="px-card" aria-labelledby="pc-title" data-testid="pc-portability" style={{ scrollMarginTop: 80 }}>
      <div className="px-card-h">
        <Ico icon={Package} c="teal" />
        <h2 id="pc-title">Move this project</h2>
      </div>
      <div style={{ display: "grid", gap: 10 }}>
        <div style={ROW}>
          <GatedButton permission="project:update" projectId={projectId} className="px-more px-linkbtn" data-testid="pc-export"
                       title="Download this project. Secrets stay empty." onClick={() => void onExport()}>
            <ArrowSquareOut size={12} aria-hidden /> Export
          </GatedButton>
          {bundle && !leaks.length ? (
            <button type="button" className="px-more px-linkbtn" data-testid="pc-download" title="Download the bundle"
                    onClick={() => downloadBundle(bundle, projectId)}>
              <DownloadSimple size={12} aria-hidden /> Download
            </button>
          ) : null}
          {exportSaid ? <span className="px-note" role="alert" data-testid="pc-export-refused">{exportSaid}</span> : null}
        </div>
        {placeholders ? (
          <ul data-testid="pc-secrets" style={{ margin: 0, paddingLeft: 18 }}>
            {placeholders.length ? placeholders.map((item, i) => (
              <li key={`${item.name}-${i}`}>{item.name} · {item.kind || "secret"} · empty</li>
            )) : <li>No secrets in this project.</li>}
          </ul>
        ) : null}
        <label className="px-faint" htmlFor="pc-import-bundle">Import a bundle</label>
        <textarea id="pc-import-bundle" data-testid="pc-import-bundle" value={raw} rows={4} spellCheck={false}
                  onChange={(e) => setRaw(e.target.value)} style={{ width: "100%", fontFamily: "ui-monospace, monospace", fontSize: 12 }} />
        <div style={ROW}>
          <GatedButton permission="project:update" projectId={projectId} className="px-more px-linkbtn" data-testid="pc-import-check"
                       title="Check what would change. Nothing is written." onClick={() => void onCheck()}>Check</GatedButton>
          {canApply ? (
            <GatedButton permission="project:update" projectId={projectId} className="px-more px-linkbtn" data-testid="pc-import-apply"
                         title="Apply the checked import." onClick={() => void onApply()}>Import</GatedButton>
          ) : null}
        </div>
        {plan ? <p data-testid="pc-import-plan" style={{ margin: 0 }}>{plan}</p> : null}
        {imported ? <p data-testid="pc-import-done" style={{ margin: 0 }}>{imported}</p> : null}

        <div className="px-card-h" style={{ marginTop: 4 }}>
          <Ico icon={ClockCounterClockwise} c="amber" />
          <h2 id="pc-backups">Backups and rollback</h2>
        </div>
        <p data-testid="pc-backup-health" style={{ margin: 0 }}>{health?.sentence ?? "Reading backups…"}</p>
        <label className="px-faint" htmlFor="pc-restore-path">Backup directory, if the upgrade changed the database</label>
        <input id="pc-restore-path" data-testid="pc-restore-path" value={restore} autoComplete="off" spellCheck={false}
               onChange={(e) => setRestore(e.target.value)} placeholder="/var/lib/boss/backups/…" />
        <div style={ROW}>
          <GatedButton permission="instance:manage" className="px-more px-linkbtn" data-testid="pc-rollback"
                       title="Put the previous release back on the host" onClick={() => setAskRollback(true)}>
            Roll back the last upgrade
          </GatedButton>
        </div>
        <p className="px-faint" style={{ margin: 0 }}>Runs on the host. The API cannot reach Docker.</p>
        {command ? <code data-testid="pc-rollback-command">{command}</code> : null}
      </div>
      {askRollback ? (
        <ConfirmSheet title="Roll back the last upgrade" testid="px-confirm"
                      lede="The host puts the previous release back. Nothing is built."
                      rows={[{ label: "Command", value: commandNow ?? "Not a backup directory", mono: true }]}
                      press="Roll back" pressKind="danger"
                      onClose={() => setAskRollback(false)}
                      onConfirm={async () => {
                        if (!commandNow) return "Use a backup directory, not a command.";
                        setCommand(commandNow);
                        return null;
                      }} />
      ) : null}
    </section>
  );
}
