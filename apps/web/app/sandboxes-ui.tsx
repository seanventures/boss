"use client";

import { useCallback, useEffect, useState } from "react";
import type { SandboxSummary, SandboxExecResult, SandboxStats } from "@boss/api-client";

// Same-origin proxies (Next route handlers) — the browser never calls the
// control-plane API directly (avoids CORS + keeps the API off the browser).
async function apiTree(id: string): Promise<{ tree: string[] }> {
  const r = await fetch(`/api/sandboxes/${encodeURIComponent(id)}/tree`);
  return r.json();
}
async function apiFile(id: string, path: string): Promise<{ content: string; error?: string }> {
  const r = await fetch(`/api/sandboxes/${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`);
  return r.json();
}
async function apiExec(id: string, command: string, breakGlass: boolean, reason?: string): Promise<SandboxExecResult> {
  const r = await fetch(`/api/sandboxes/${encodeURIComponent(id)}/exec`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ command, break_glass: breakGlass, reason }),
  });
  return r.json();
}
async function apiDestroy(id: string): Promise<void> {
  await fetch(`/api/sandboxes/${encodeURIComponent(id)}`, { method: "DELETE" });
}
async function apiStats(id: string): Promise<SandboxStats> {
  const r = await fetch(`/api/sandboxes/${encodeURIComponent(id)}/stats`);
  return r.json();
}
type HelperCatalog = { categories: Record<string, string[]>; commands: string[]; unavailable?: string };
async function apiHelpers(): Promise<HelperCatalog> {
  const r = await fetch(`/api/sandboxes/helpers`);
  return r.json();
}
async function apiPreserve(id: string, reason: string): Promise<SandboxSummary> {
  const r = await fetch(`/api/sandboxes/${encodeURIComponent(id)}/preserve`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  return r.json();
}

function statusTone(s: string): string {
  if (s === "idle") return "green";
  if (s === "failed") return "pink";
  if (s === "running") return "blue";
  return "muted";
}

function FileViewer({ sandboxId, path }: { sandboxId: string; path: string }) {
  const [content, setContent] = useState<string>("loading…");
  useEffect(() => {
    let live = true;
    apiFile(sandboxId, path)
      .then((r) => live && setContent(r.content))
      .catch((e) => live && setContent(`(error: ${e?.message ?? e})`));
    return () => { live = false; };
  }, [sandboxId, path]);
  return <pre className="sbx-file"><code>{content}</code></pre>;
}

function Console({ sandboxId }: { sandboxId: string }) {
  const [cmd, setCmd] = useState("git log --oneline -5");
  const [breakGlass, setBreakGlass] = useState(false);
  const [reason, setReason] = useState("");
  const [log, setLog] = useState<{ cmd: string; out: string; bg: boolean }[]>([]);
  const [busy, setBusy] = useState(false);
  const [helpers, setHelpers] = useState<HelperCatalog | null>(null);
  const [showHelpers, setShowHelpers] = useState(false);

  useEffect(() => {
    let live = true;
    apiHelpers().then((h) => live && setHelpers(h)).catch(() => {});
    return () => { live = false; };
  }, []);

  const run = useCallback(async () => {
    if (!cmd.trim() || busy) return;
    if (breakGlass && !reason.trim()) {
      setLog((l) => [{ cmd, out: "⚠ break-glass requires a reason.", bg: true }, ...l]);
      return;
    }
    setBusy(true);
    try {
      const r: SandboxExecResult = await apiExec(sandboxId, cmd, breakGlass, reason || undefined);
      const out = r.blocked
        ? `🔒 ${r.detail}`
        : `${r.stdout ?? ""}${r.stderr ? "\n" + r.stderr : ""}`.trim() || `(exit ${r.exit_code})`;
      setLog((l) => [{ cmd, out, bg: breakGlass }, ...l]);
    } catch (e: unknown) {
      setLog((l) => [{ cmd, out: `error: ${(e as Error)?.message ?? e}`, bg: breakGlass }, ...l]);
    } finally {
      setBusy(false);
    }
  }, [cmd, breakGlass, reason, sandboxId, busy]);

  return (
    <div className="sbx-console">
      <div className="sbx-console-bar">
        <input className="sbx-cmd" value={cmd} onChange={(e) => setCmd(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="git status · ls · mvn -q test · cat <file>" spellCheck={false} />
        <button className="cap-btn primary" onClick={run} disabled={busy}>{busy ? "…" : "Run"}</button>
      </div>
      <label className="sbx-bg-toggle">
        <input type="checkbox" checked={breakGlass} onChange={(e) => setBreakGlass(e.target.checked)} />
        Break-glass (raw shell — audited)
      </label>
      {breakGlass ? (
        <input className="sbx-reason" value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="Reason (required — recorded in the audit log)" />
      ) : null}
      <p className="detail">
        Helper commands run freely. A raw shell needs break-glass + a reason and emits an elevated audit event.{" "}
        <button type="button" className="sbx-helpers-toggle" onClick={() => setShowHelpers((v) => !v)}>
          {showHelpers ? "Hide" : "Show"} allowed commands{helpers ? ` (${helpers.commands.length})` : ""}
        </button>
      </p>
      {showHelpers ? (
        helpers && !helpers.unavailable ? (
          <div className="sbx-helpers">
            {Object.entries(helpers.categories).map(([group, cmds]) => (
              <div key={group} className="sbx-helper-group">
                <span className="sbx-helper-label">{group}</span>
                <span className="sbx-helper-chips">
                  {cmds.map((c) => (
                    <button key={c} type="button" className="sbx-helper-chip"
                      title={`Insert "${c}" into the command box`}
                      onClick={() => setCmd((cur) => (cur.trim() ? cur : c + " "))}>
                      {c}
                    </button>
                  ))}
                </span>
              </div>
            ))}
            <p className="detail sbx-helper-note">
              Diagnostics like <code>top</code> run in batch mode automatically. Anything that mutates outside the
              working tree, reaches the network, or could expose secrets (e.g. <code>env</code>) needs break-glass.
            </p>
          </div>
        ) : (
          <p className="detail">{helpers?.unavailable ? `Helper list unavailable: ${helpers.unavailable}` : "Loading allowed commands…"}</p>
        )
      ) : null}
      <div className="sbx-console-out">
        {log.map((e, i) => (
          <div key={i} className="sbx-out-entry">
            <div className={`sbx-out-cmd${e.bg ? " bg" : ""}`}>{e.bg ? "⚠ " : "$ "}{e.cmd}</div>
            <pre className="sbx-out-text">{e.out}</pre>
          </div>
        ))}
      </div>
    </div>
  );
}

function Telemetry({ sandboxId }: { sandboxId: string }) {
  const [s, setS] = useState<SandboxStats | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    try { setS(await apiStats(sandboxId)); } catch { /* ignore */ } finally { setBusy(false); }
  }, [sandboxId]);
  useEffect(() => { load(); }, [load]);
  return (
    <div className="sbx-telemetry">
      <div className="sbx-tel-head">
        <span className="sbx-block-title">Resources <span className="detail">— is it CPU/memory-bound?</span></span>
        <button className="cap-btn" onClick={load} disabled={busy}>{busy ? "…" : "↻ Refresh"}</button>
      </div>
      {s ? (
        <div className="sbx-tel-grid">
          <span><b>CPU</b> {s.cpu_pct ?? "—"}</span>
          <span><b>Memory</b> {s.mem_usage ?? "—"} {s.mem_pct ? `(${s.mem_pct})` : ""}</span>
          <span><b>PIDs</b> {s.pids ?? "—"}</span>
          <span><b>Block I/O</b> {s.block_io ?? "—"}</span>
          <span className={s.oom_killed ? "sbx-tel-alert" : ""}><b>OOM-killed</b> {s.oom_killed ? "YES ⚠" : "no"}</span>
          <span><b>Container</b> {s.container_status ?? "—"}{s.exit_code != null ? ` · exit ${s.exit_code}` : ""}</span>
        </div>
      ) : <p className="detail">loading telemetry…</p>}
    </div>
  );
}

function SandboxDetail({ sb, onBack, onDestroyed }: {
  sb: SandboxSummary; onBack: () => void; onDestroyed: () => void;
}) {
  const [tree, setTree] = useState<string[]>([]);
  const [file, setFile] = useState<string | null>(null);
  const [status, setStatus] = useState(sb.status);
  const [preserved, setPreserved] = useState(!!sb.preserved);
  const [showPreserve, setShowPreserve] = useState(false);
  const [reason, setReason] = useState("");
  const ds = sb.meta?.diff_summary;

  useEffect(() => {
    let live = true;
    apiTree(sb.sandbox_id).then((r) => live && setTree(r.tree ?? [])).catch(() => {});
    return () => { live = false; };
  }, [sb.sandbox_id]);

  const doPreserve = async () => {
    if (!reason.trim()) return;
    const r = await apiPreserve(sb.sandbox_id, reason);
    setStatus(r.status ?? "quarantined");
    setPreserved(true);
    setShowPreserve(false);
  };

  return (
    <div className="sbx-detail">
      <button className="mr-back" onClick={onBack}>← All sandboxes</button>
      <div className="sbx-detail-head">
        <div>
          <h3>{sb.title || sb.repo}</h3>
          <p className="detail"><code>{sb.sandbox_id}</code> · {sb.tenant_id} · {sb.repo}</p>
        </div>
        <div className="sbx-detail-actions">
          <span className={`cap-badge tone-${statusTone(status)}`}>{status}</span>
          {!preserved ? (
            <button className="cap-btn" onClick={() => setShowPreserve((v) => !v)}>Preserve for investigation</button>
          ) : <span className="detail">🔒 preserved &amp; snapshotted</span>}
          <button className="cap-btn" onClick={async () => { await apiDestroy(sb.sandbox_id); onDestroyed(); }}>
            Destroy
          </button>
        </div>
      </div>

      {showPreserve ? (
        <div className="sbx-preserve">
          <input className="sbx-reason" value={reason} onChange={(e) => setReason(e.target.value)}
            placeholder="Reason / escalation ref (required — quarantines + snapshots the VM, audited)" />
          <button className="cap-btn primary" onClick={doPreserve}>Quarantine + snapshot</button>
        </div>
      ) : null}

      <div className="sbx-meta">
        <span><b>Engine</b> {sb.meta?.engine ?? "—"}</span>
        <span><b>Diff</b> {ds ? `${ds.files_changed} files · +${ds.additions}/−${ds.deletions}` : "—"}</span>
        <span><b>Tests</b> <span className={`cap-badge tone-${sb.meta?.tests_passed ? "green" : "muted"}`}>{sb.meta?.tests_passed ? "passed" : "—"}</span></span>
        <span><b>Age</b> {Math.round(sb.age_seconds / 60)}m</span>
      </div>

      <Telemetry sandboxId={sb.sandbox_id} />

      <div className="sbx-browser">
        <div className="sbx-tree">
          <div className="sbx-tree-head">Working tree</div>
          {tree.length === 0 ? <p className="detail">empty / loading…</p> : (
            <ul className="sbx-tree-list">
              {tree.filter((t) => !t.endsWith("/")).map((t) => (
                <li key={t}>
                  <button className={`sbx-tree-item${file === t ? " active" : ""}`} onClick={() => setFile(t)}>
                    {t}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="sbx-fileview">
          {file ? <FileViewer sandboxId={sb.sandbox_id} path={file} /> : <p className="detail">Select a file to view it.</p>}
        </div>
      </div>

      <div className="sbx-block-title">Console <span className="detail">— governed exec into this sandbox</span></div>
      <Console sandboxId={sb.sandbox_id} />
    </div>
  );
}

export function SandboxesHome({ sandboxes, apiReachable, unavailable }: {
  sandboxes: SandboxSummary[];
  apiReachable: boolean;
  unavailable?: string;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  if (!apiReachable) return <p className="detail">Execution plane unavailable — API unreachable.</p>;
  if (unavailable) return <p className="detail">Sandbox host unreachable: {unavailable}</p>;

  const active = selected ? sandboxes.find((s) => s.sandbox_id === selected) : null;
  if (active) {
    return <SandboxDetail sb={active} onBack={() => setSelected(null)} onDestroyed={() => setSelected(null)} />;
  }

  return (
    <div className="sbx">
      <div className="sbx-home-bar">
        <span className="sbx-home-count">{sandboxes.length} live sandbox{sandboxes.length === 1 ? "" : "es"}</span>
      </div>
      {sandboxes.length === 0 ? (
        <p className="detail">No live sandboxes. Sandboxes appear here while (and shortly after) a code task runs on the execution plane.</p>
      ) : (
        <div className="sbx-grid">
          {sandboxes.map((s) => (
            <button key={s.sandbox_id} className="sbx-card" onClick={() => setSelected(s.sandbox_id)}>
              <div className="sbx-card-head">
                <span className={`cap-badge tone-${statusTone(s.status)}`}>{s.status}</span>
                <span className="detail">{Math.round(s.age_seconds / 60)}m</span>
              </div>
              <div className="sbx-card-title">{s.title || s.repo}</div>
              <div className="sbx-card-repo">{s.tenant_id} · {s.repo}</div>
              <div className="detail">{s.meta?.engine ?? ""} {s.meta?.diff_summary ? `· ${s.meta.diff_summary.files_changed} files` : ""}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
