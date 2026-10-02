"use client";

import { useCallback, useEffect, useState } from "react";
import type { SandboxSummary, SandboxExecResult, SandboxStats, SandboxHost } from "@boss/api-client";
import { apiWrite } from "../lib/api-write";

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
  const r = await apiWrite(`/api/sandboxes/${encodeURIComponent(id)}/exec`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ command, break_glass: breakGlass, reason }),
  });
  return r.json();
}
async function apiStats(id: string): Promise<SandboxStats> {
  const r = await fetch(`/api/sandboxes/${encodeURIComponent(id)}/stats`);
  return r.json();
}
async function apiLogs(id: string): Promise<{ serial_tail?: string; agent_log?: string; unavailable?: string }> {
  const r = await fetch(`/api/sandboxes/${encodeURIComponent(id)}/logs`);
  return r.json();
}
type HelperCatalog = { categories: Record<string, string[]>; commands: string[]; unavailable?: string };
async function apiHelpers(): Promise<HelperCatalog> {
  const r = await fetch(`/api/sandboxes/helpers`);
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

function LogsPanel({ sandboxId }: { sandboxId: string }) {
  const [logs, setLogs] = useState<{ serial_tail?: string; agent_log?: string; unavailable?: string } | null>(null);
  const [tab, setTab] = useState<"agent" | "serial">("agent");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    setBusy(true);
    try { setLogs(await apiLogs(sandboxId)); } catch { /* ignore */ } finally { setBusy(false); }
  }, [sandboxId]);
  useEffect(() => { load(); }, [load]);
  const text = tab === "agent" ? (logs?.agent_log ?? "") : (logs?.serial_tail ?? "");
  return (
    <div className="sbx-console">
      <div className="sbx-console-bar" style={{ gap: 8 }}>
        <button className={`cap-btn${tab === "agent" ? " primary" : ""}`} onClick={() => setTab("agent")}>Agent log</button>
        <button className={`cap-btn${tab === "serial" ? " primary" : ""}`} onClick={() => setTab("serial")}>Guest console</button>
        <button className="cap-btn" onClick={load} disabled={busy} style={{ marginLeft: "auto" }}>{busy ? "…" : "↻"}</button>
      </div>
      {logs?.unavailable ? <p className="detail" style={{ color: "#f85149" }}>⚠ {logs.unavailable}</p> : null}
      <pre className="sbx-out-text" style={{ maxHeight: 340, overflow: "auto", whiteSpace: "pre-wrap" }}>
        {text || (busy ? "loading…" : "(empty)")}
      </pre>
    </div>
  );
}

function SandboxDetail({ sb, onBack }: {
  sb: SandboxSummary; onBack: () => void;
}) {
  const [tree, setTree] = useState<string[]>([]);
  const [file, setFile] = useState<string | null>(null);
  const ds = sb.meta?.diff_summary;

  useEffect(() => {
    let live = true;
    apiTree(sb.sandbox_id).then((r) => live && setTree(r.tree ?? [])).catch(() => {});
    return () => { live = false; };
  }, [sb.sandbox_id]);

  return (
    <div className="sbx-detail">
      <button className="mr-back" onClick={onBack}>← All sandboxes</button>
      <div className="sbx-detail-head">
        <div>
          <h3>{sb.title || sb.repo}</h3>
          <p className="detail"><code>{sb.sandbox_id}</code> · {sb.tenant_id} · {sb.repo}</p>
        </div>
        <div className="sbx-detail-actions">
          <span className={`cap-badge tone-${statusTone(sb.status)}`}>{sb.status}</span>
        </div>
      </div>

      <p className="detail" style={{ marginTop: -4 }}>
        Retained micro‑VM rootfs of a failed/incomplete task (read‑only, auto‑reaped ~2h). Browse the
        working tree, read files, and check the logs to find the root cause. Manual hold and release
        are unavailable while their approval and audit controls are completed.
      </p>

      <div className="sbx-meta">
        <span><b>Engine</b> {sb.meta?.engine ?? "—"}</span>
        <span><b>Diff</b> {ds ? `${ds.files_changed} files · +${ds.additions}/−${ds.deletions}` : "—"}</span>
        <span><b>Tests</b> <span className={`cap-badge tone-${sb.meta?.tests_passed ? "green" : "muted"}`}>{sb.meta?.tests_passed ? "passed" : "failed / —"}</span></span>
        <span><b>Age</b> {Math.round(sb.age_seconds / 60)}m</span>
      </div>

      <div className="sbx-browser">
        <div className="sbx-tree">
          <div className="sbx-tree-head">Working tree (/work/repo)</div>
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

      <div className="sbx-block-title">Logs <span className="detail">— code‑agent log + guest boot console from the retained VM</span></div>
      <LogsPanel sandboxId={sb.sandbox_id} />
    </div>
  );
}

export function SandboxesHome({ sandboxes, apiReachable, unavailable, host }: {
  sandboxes: SandboxSummary[];
  apiReachable: boolean;
  unavailable?: string;
  host?: SandboxHost;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  if (!apiReachable) return <p className="detail">Execution plane unavailable — API unreachable.</p>;

  if (!host?.connected) {
    return (
      <div className="panel" style={{ padding: 20, display: "grid", gap: 8 }}>
        <div style={{ fontWeight: 700, fontSize: 15, color: "var(--ink)" }}>No micro‑VM sandbox host connected</div>
        <p className="detail" style={{ margin: 0 }}>
          The control plane isn&apos;t wired to a micro‑VM execution host right now, so coding tasks run via
          the in‑process engine. Connect the micro‑VM provider to run tasks in isolated Firecracker VMs.
        </p>
        <p className="detail" style={{ margin: 0, opacity: 0.6, fontSize: 12 }}>Host status: {unavailable ?? "not connected"}</p>
      </div>
    );
  }

  // Connected micro-VM host — show it as proof + live capacity/running.
  const hostBanner = (
    <div className="panel" style={{ padding: 16, display: "grid", gap: 8, marginBottom: 12 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span className="cap-badge tone-green">● connected</span>
        <span style={{ fontWeight: 700, fontSize: 15, color: "var(--ink)" }}>{host?.kind ?? "micro‑VM"} execution host</span>
        {host?.url ? <code className="detail" style={{ fontSize: 12 }}>{host.url}</code> : null}
      </div>
      <div className="detail" style={{ display: "flex", gap: 18, flexWrap: "wrap", fontSize: 14 }}>
        <span><b style={{ color: "var(--ink)" }}>{host?.running ?? 0}</b> running</span>
        <span><b style={{ color: "var(--ink)" }}>{host?.available ?? "—"}</b> available</span>
        <span><b style={{ color: "var(--ink)" }}>{host?.capacity ?? "—"}</b> capacity</span>
      </div>
      <p className="detail" style={{ margin: 0 }}>
        Projects run <b>inside single‑tenant Firecracker micro‑VMs</b> here — jailed, cgroup‑capped,
        internet‑only egress, destroyed after each task. VMs are ephemeral &amp; isolated by design (not
        browsable); the <b>running</b> count rises while a project executes.
      </p>
    </div>
  );

  const active = selected ? sandboxes.find((s) => s.sandbox_id === selected) : null;
  if (active) {
    return <SandboxDetail sb={active} onBack={() => setSelected(null)} />;
  }

  return (
    <div className="sbx">
      <div className="sbx-home-bar">
        <span className="sbx-home-count">{sandboxes.length} live sandbox{sandboxes.length === 1 ? "" : "es"}</span>
      </div>
      {sandboxes.length === 0 ? (
        <p className="detail">No live sandboxes. Sandboxes appear here while (and shortly after) a project runs on the execution plane.</p>
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
