"use client";

/**
 * APP MAP CONFIRM / EDIT CARD (BOSSAPP-556).
 * Wireframe: docs/design/APP_MAP_CONSOLE_CARD.md.
 *
 * Displays the stored App Map for an application, lets an operator scan the repository for
 * proposals, resolve ambiguity by picking from the candidate list, edit paths/commands inline,
 * confirm the map, and trigger baseline install+test verification on the base commit.
 */
import React, { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, CheckCircle, Compass, PencilSimple, Play, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import {
  appMapApi,
  provenanceLabel,
  statusBadge,
  type AppMapDrift,
  type AppMapEntry,
  type AppMapStatus,
} from "../../lib/app-map";
import { refusalMessage } from "../../lib/multirepo";
import { showToast } from "../client-forms";
import "./app-map-card.tsx.css";

type EditDraft = {
  root_path: string;
  workspace_root: string;
  install: string;
  build: string;
  test: string;
  run_command: string;
  run_health: string;
  shared_paths: string;
};

function draftFrom(entry: AppMapEntry | null): EditDraft {
  return {
    root_path: entry?.root_path ?? ".",
    workspace_root: entry?.workspace_root ?? ".",
    install: entry?.commands?.install ?? "",
    build: entry?.commands?.build ?? "",
    test: entry?.commands?.test ?? "",
    run_command: entry?.run?.command ?? "",
    run_health: entry?.run?.health ?? "",
    shared_paths: (entry?.shared_paths ?? []).join(", "),
  };
}

export function AppMapCard({
  projectId,
  appKey,
  appName,
  onConfirmed,
}: {
  projectId: string;
  appKey: string;
  appName?: string;
  onConfirmed?: (entry: AppMapEntry) => void;
}) {
  const [entry, setEntry] = useState<AppMapEntry | null>(null);
  const [status, setStatus] = useState<AppMapStatus>("unmapped");
  const [drift, setDrift] = useState<AppMapDrift | null>(null);
  const [candidates, setCandidates] = useState<AppMapEntry[]>([]);
  const [pickedRoot, setPickedRoot] = useState<string>("");
  const [editing, setEditing] = useState<EditDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();

  const load = useCallback(async () => {
    try {
      const res = await appMapApi.get(projectId, appKey);
      if (res && typeof res === "object" && "status" in res) {
        setEntry(res.app_map ?? null);
        setStatus(res.status ?? "unmapped");
        setDrift(res.drift ?? null);
      } else {
        setEntry(null);
        setStatus("unmapped");
        setDrift(null);
      }
      setError(null);
    } catch (e) {
      const st = (e as { status?: number })?.status;
      if (st === 404) {
        setEntry(null);
        setStatus("unmapped");
        setError(null);
      } else {
        setError(refusalMessage(e));
      }
    }
  }, [projectId, appKey]);

  useEffect(() => {
    void load();
  }, [load]);

  const runScan = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await guarded(`Scan repository for ${appName || appKey} App Map.`, () =>
        appMapApi.scan(projectId, { persist: false }),
      );
      if (res.status === "proposed" && res.selected) {
        setEntry(res.selected);
        setStatus("proposed");
        setCandidates([]);
        setPickedRoot(res.selected.root_path);
        showToast("success", `Proposed App Map at ${res.selected.root_path}`);
      } else if (res.status === "ambiguous") {
        setCandidates(res.candidates);
        setStatus("ambiguous");
        setPickedRoot(res.candidates[0]?.root_path ?? "");
        showToast("success", `Found ${res.candidates.length} candidate apps — choose one to confirm`);
      } else {
        setCandidates([]);
        setStatus("unmapped");
        setError(res.refusal || "No runnable app was detected in this repository.");
      }
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setError(refusalMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const confirmCandidateOrEntry = async (target: AppMapEntry) => {
    setBusy(true);
    setError(null);
    try {
      const res = await guarded(`Confirm App Map for ${appName || appKey}.`, () =>
        appMapApi.save(appKey, {
          project_id: projectId,
          app_name: appName || target.app_name || appKey,
          root_path: target.root_path,
          workspace_root: target.workspace_root,
          stack: target.stack,
          commands: target.commands,
          run: target.run,
          shared_paths: target.shared_paths,
          manifests: target.manifests,
          provenance: target.provenance,
          fingerprint: target.fingerprint,
          binding_id: target.binding_id,
          confirm: true,
        }),
      );
      setEntry(res.app_map);
      setStatus(res.status);
      setCandidates([]);
      setDrift(null);
      setEditing(null);
      if (res.app_map) onConfirmed?.(res.app_map);
      showToast("success", "App Map confirmed");
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setError(refusalMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const saveEdit = async () => {
    if (!editing) return;
    setBusy(true);
    setError(null);
    try {
      const shared = editing.shared_paths
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await guarded(`Save App Map for ${appName || appKey}.`, () =>
        appMapApi.save(appKey, {
          project_id: projectId,
          app_name: appName || entry?.app_name || appKey,
          root_path: editing.root_path || ".",
          workspace_root: editing.workspace_root || ".",
          stack: entry?.stack,
          commands: {
            ...(entry?.commands ?? {}),
            install: editing.install || null,
            build: editing.build || null,
            test: editing.test || null,
          },
          run: {
            ...(entry?.run ?? {}),
            command: editing.run_command || null,
            health: editing.run_health || null,
          },
          shared_paths: shared,
          binding_id: entry?.binding_id,
          confirm: true,
        }),
      );
      setEntry(res.app_map);
      setStatus(res.status);
      setDrift(null);
      setEditing(null);
      if (res.app_map) onConfirmed?.(res.app_map);
      showToast("success", "App Map saved and confirmed");
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setError(refusalMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const runBaseline = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await guarded(`Run baseline verification for ${appName || appKey}.`, () =>
        appMapApi.verifyBaseline(appKey, projectId),
      );
      setEntry(res.app_map);
      showToast(
        res.baseline.status === "passed" ? "success" : "error",
        res.baseline.status === "passed" ? "Baseline verification passed" : "Baseline verification failed",
      );
    } catch (e) {
      if ((e as { code?: string }).code !== "step_up_cancelled") setError(refusalMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const badge = statusBadge(status);
  const pickedCandidate = candidates.find((c) => c.root_path === pickedRoot) ?? candidates[0] ?? null;
  const baseline = entry?.baseline;

  return (
    <section className="mr-ui-card am-card" data-testid="app-map-card" data-status={status}>
      <div className="am-head">
        <div>
          <div className="am-title-row">
            <Compass size={16} aria-hidden />
            <h3>App Map</h3>
            <span className="mr-ui-pill" data-tone={badge.tone} data-testid="app-map-status">
              {badge.label}
            </span>
          </div>
          <p className="mr-ui-sub" data-testid="app-map-provenance">
            {entry ? (
              <>
                Source: <b>{provenanceLabel(entry.provenance?.source)}</b>
                {entry.fingerprint ? <> · Fingerprint <code className="mr-ui-mono">{entry.fingerprint.slice(0, 10)}</code></> : null}
              </>
            ) : (
              "BOSS reads one stored App Map for every build, gate, preview and deploy — never guessing the repo root."
            )}
          </p>
        </div>
        <div className="am-actions">
          <GatedButton
            permission="repository:manage"
            className="ag-btn"
            data-testid="app-map-scan"
            disabled={busy}
            onClick={() => void runScan()}
          >
            <ArrowsClockwise size={13} aria-hidden /> Scan repo
          </GatedButton>
          {!editing ? (
            <GatedButton
              permission="repository:manage"
              className="ag-btn"
              data-testid="app-map-edit"
              disabled={busy}
              onClick={() => setEditing(draftFrom(entry))}
            >
              <PencilSimple size={13} aria-hidden /> Edit
            </GatedButton>
          ) : null}
          {entry && !editing && status !== "confirmed" && status !== "ambiguous" ? (
            <GatedButton
              permission="repository:manage"
              className="ag-btn ag-btn-primary"
              data-testid="app-map-confirm"
              disabled={busy}
              onClick={() => void confirmCandidateOrEntry(entry)}
            >
              <CheckCircle size={13} aria-hidden /> Confirm App Map
            </GatedButton>
          ) : null}
        </div>
      </div>

      {error ? (
        <div className="mr-ui-note" data-tone="bad" role="alert" data-testid="app-map-error">
          <Warning size={15} weight="bold" aria-hidden />
          <div>{error}</div>
        </div>
      ) : null}

      {(status === "stale" || drift?.drifted) && !editing ? (
        <div className="mr-ui-note" data-tone="warn" role="status" data-testid="app-map-drift">
          <Warning size={15} weight="bold" aria-hidden />
          <div>
            Manifests changed since confirmation
            {drift?.added_manifests?.length ? ` (added: ${drift.added_manifests.join(", ")})` : ""}.
            Re-scan and re-confirm the App Map so every stage uses current commands.
          </div>
        </div>
      ) : null}

      {status === "ambiguous" && candidates.length > 0 && !editing ? (
        <div className="am-candidates" data-testid="app-map-candidates">
          <div className="mr-ui-sub">Multiple apps found. Choose which folder belongs to this app:</div>
          {candidates.map((cand) => {
            const selected = cand.root_path === (pickedCandidate?.root_path ?? "");
            return (
              <label
                key={cand.root_path}
                className="am-cand-row"
                data-selected={selected}
                data-testid={`app-map-candidate-${cand.root_path}`}
              >
                <input
                  type="radio"
                  name="app-map-candidate"
                  checked={selected}
                  onChange={() => setPickedRoot(cand.root_path)}
                />
                <span className="mr-ui-mono"><b>{cand.root_path}</b></span>
                <span className="mr-ui-dim">
                  {[cand.stack?.language, cand.stack?.framework].filter(Boolean).join(" / ") || "detected"}
                </span>
                <span className="mr-ui-spacer" />
                <span className="mr-ui-sub">{provenanceLabel(cand.provenance?.source)}</span>
              </label>
            );
          })}
          <div className="am-actions">
            <GatedButton
              permission="repository:manage"
              className="ag-btn ag-btn-primary"
              data-testid="app-map-confirm-candidate"
              disabled={busy || !pickedCandidate}
              onClick={() => pickedCandidate && void confirmCandidateOrEntry(pickedCandidate)}
            >
              <CheckCircle size={13} aria-hidden /> Confirm selected app
            </GatedButton>
          </div>
        </div>
      ) : null}

      {editing ? (
        <div data-testid="app-map-editor">
          <div className="am-form">
            <label className="am-field">
              <span>Root path (<code>.</code> for repo root)</span>
              <input
                aria-label="Root path"
                data-testid="app-map-input-root"
                value={editing.root_path}
                onChange={(e) => setEditing({ ...editing, root_path: e.target.value })}
              />
            </label>
            <label className="am-field">
              <span>Workspace root</span>
              <input
                aria-label="Workspace root"
                data-testid="app-map-input-workspace"
                value={editing.workspace_root}
                onChange={(e) => setEditing({ ...editing, workspace_root: e.target.value })}
              />
            </label>
            <label className="am-field">
              <span>Install command</span>
              <input
                aria-label="Install command"
                data-testid="app-map-input-install"
                value={editing.install}
                onChange={(e) => setEditing({ ...editing, install: e.target.value })}
              />
            </label>
            <label className="am-field">
              <span>Build command</span>
              <input
                aria-label="Build command"
                data-testid="app-map-input-build"
                value={editing.build}
                onChange={(e) => setEditing({ ...editing, build: e.target.value })}
              />
            </label>
            <label className="am-field">
              <span>Test command</span>
              <input
                aria-label="Test command"
                data-testid="app-map-input-test"
                value={editing.test}
                onChange={(e) => setEditing({ ...editing, test: e.target.value })}
              />
            </label>
            <label className="am-field">
              <span>Run command</span>
              <input
                aria-label="Run command"
                data-testid="app-map-input-run"
                value={editing.run_command}
                onChange={(e) => setEditing({ ...editing, run_command: e.target.value })}
              />
            </label>
            <label className="am-field">
              <span>Shared write-boundary paths (comma-separated)</span>
              <input
                aria-label="Shared paths"
                data-testid="app-map-input-shared"
                value={editing.shared_paths}
                onChange={(e) => setEditing({ ...editing, shared_paths: e.target.value })}
              />
            </label>
          </div>
          <div className="mr-ui-foot" style={{ marginTop: 12 }}>
            <button
              type="button"
              className="ag-btn"
              disabled={busy}
              onClick={() => {
                setEditing(null);
                setError(null);
              }}
            >
              Cancel
            </button>
            <GatedButton
              permission="repository:manage"
              className="ag-btn ag-btn-primary"
              data-testid="app-map-save"
              disabled={busy}
              onClick={() => void saveEdit()}
            >
              {busy ? "Saving…" : "Save & confirm App Map"}
            </GatedButton>
          </div>
        </div>
      ) : entry ? (
        <>
          <dl className="am-grid" data-testid="app-map-details">
            <div>
              <dt>Root path</dt>
              <dd className="mr-ui-mono" data-testid="app-map-root-path">{entry.root_path}</dd>
            </div>
            <div>
              <dt>Workspace root</dt>
              <dd className="mr-ui-mono">{entry.workspace_root}</dd>
            </div>
            <div>
              <dt>Stack</dt>
              <dd>
                {[entry.stack?.language, entry.stack?.framework].filter(Boolean).join(" / ") || "—"}
                {entry.stack?.package_manager ? ` (${entry.stack.package_manager})` : ""}
              </dd>
            </div>
            <div>
              <dt>Install / Test</dt>
              <dd className="mr-ui-mono">
                {[entry.commands?.install, entry.commands?.test].filter(Boolean).join(" · ") || "—"}
              </dd>
            </div>
            <div>
              <dt>Run command</dt>
              <dd className="mr-ui-mono">{entry.run?.command || "—"}</dd>
            </div>
            <div>
              <dt>Shared paths</dt>
              <dd className="mr-ui-mono">{entry.shared_paths?.length ? entry.shared_paths.join(", ") : "—"}</dd>
            </div>
          </dl>

          <div className="am-baseline" data-testid="app-map-baseline">
            <div>
              <span className="mr-ui-sub">Baseline on base commit: </span>
              {baseline?.status === "passed" ? (
                <span className="mr-ui-pill" data-tone="ok" data-testid="app-map-baseline-status">
                  Passed{baseline.commit_sha ? ` · ${baseline.commit_sha.slice(0, 7)}` : ""}
                </span>
              ) : baseline?.status === "failed" ? (
                <span className="mr-ui-pill" data-tone="bad" data-testid="app-map-baseline-status">
                  Failed{baseline.error ? ` — ${baseline.error.slice(0, 80)}` : ""}
                </span>
              ) : (
                <span className="mr-ui-pill" data-tone="muted" data-testid="app-map-baseline-status">
                  Not verified
                </span>
              )}
            </div>
            <GatedButton
              permission="repository:manage"
              className="ag-btn"
              data-testid="app-map-verify-baseline"
              disabled={busy}
              onClick={() => void runBaseline()}
            >
              <Play size={12} aria-hidden /> Run baseline verify
            </GatedButton>
          </div>
        </>
      ) : null}
    </section>
  );
}
