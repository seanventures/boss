"use client";

import React, { useCallback, useEffect, useState } from "react";
import { GatedButton } from "../../components/gated";
import { ReadFailure } from "../../components/locked-panel";
import { SectionLabel } from "../section-label";
import { formatDate } from "../../lib/datetime";
import {
  cancelProtocolRun,
  createProtocolThread,
  forkProtocolRun,
  readEndpoints,
  readThreadHistory,
  setA2AExposure,
  startProtocolRun,
  streamProtocolRun,
  waitProtocolRun,
  type EndpointsData,
  type RunOut,
  type StreamFrame,
  type ThreadHistoryData,
} from "../../lib/agent-platform";
import "./agent-platform.css";

const ENVS = ["dev", "staging", "prod"] as const;

export function PassportEndpoints({
  projectId,
  roleKey,
  actor,
}: {
  projectId: string;
  roleKey: string;
  actor: string;
}) {
  const [env, setEnv] = useState<(typeof ENVS)[number]>("prod");
  const [data, setData] = useState<EndpointsData | null>(null);
  const [readError, setReadError] = useState<Awaited<ReturnType<typeof readEndpoints>> | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  // Try-It + Thread runner
  const [promptText, setPromptText] = useState('{"task":"Summarize open alerts"}');
  const [mode, setMode] = useState<"stream" | "wait">("stream");
  const [frames, setFrames] = useState<StreamFrame[]>([]);
  const [lastRun, setLastRun] = useState<RunOut | null>(null);
  const [threadId, setThreadId] = useState("");
  const [history, setHistory] = useState<ThreadHistoryData | null>(null);

  const load = useCallback(async () => {
    const r = await readEndpoints(projectId, roleKey, env);
    if (!r.ok) {
      setReadError(r);
      return;
    }
    setReadError(null);
    setData(r.data);
  }, [projectId, roleKey, env]);

  useEffect(() => {
    void load();
  }, [load]);

  const loadHistory = useCallback(async (tid: string) => {
    if (!tid) return;
    const r = await readThreadHistory(projectId, tid);
    if (r.ok) setHistory(r.data);
  }, [projectId]);

  const onToggleA2A = async () => {
    if (!data) return;
    setBusy(true);
    setErr("");
    setMsg("");
    const next = !data.a2a.enabled;
    const r = await setA2AExposure(projectId, actor, roleKey, env, next);
    setBusy(false);
    if (!r.ok) { setErr(r.error); return; }
    setMsg(next ? `Enabled A2A on ${env}.` : `Disabled A2A on ${env}.`);
    void load();
  };

  const parseInput = (): Record<string, unknown> => {
    try {
      const parsed = JSON.parse(promptText);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
      return { input: parsed };
    } catch {
      return { prompt: promptText };
    }
  };

  const onTryIt = async () => {
    setBusy(true);
    setErr("");
    setMsg("");
    setFrames([]);
    const input = parseInput();
    if (mode === "stream") {
      const path = `/v1/agents/${encodeURIComponent(roleKey)}/runs/stream`;
      const r = await streamProtocolRun(
        projectId,
        actor,
        path,
        { environment: env, input },
        (frame) => {
          setFrames((prev) => [...prev, frame]);
          const d = frame.data as Record<string, unknown> | null;
          if (d && typeof d.run_id === "string" && typeof d.thread_id === "string") {
            setThreadId(d.thread_id);
            setLastRun((prev) => ({
              ...(prev ?? { status: "running", agent_key: roleKey }),
              run_id: d.run_id as string,
              thread_id: d.thread_id as string,
            }));
          }
        },
      );
      setBusy(false);
      if (!r.ok) { setErr(r.error); return; }
      setMsg(`Streamed ${r.data.frames} event frames.`);
      return;
    }
    const r = await waitProtocolRun(projectId, actor, `/v1/agents/${encodeURIComponent(roleKey)}/runs/wait`, {
      environment: env,
      input,
    });
    setBusy(false);
    if (!r.ok) { setErr(r.error); return; }
    setLastRun(r.data);
    setThreadId(r.data.thread_id);
    setMsg(`Run ${r.data.run_id} finished (${r.data.status}).`);
    void loadHistory(r.data.thread_id);
  };

  const onNewThread = async () => {
    setBusy(true);
    setErr("");
    const r = await createProtocolThread(projectId, actor, roleKey, { environment: env });
    setBusy(false);
    if (!r.ok) { setErr(r.error); return; }
    setThreadId(r.data.thread_id);
    setMsg(`Created thread ${r.data.thread_id}.`);
    void loadHistory(r.data.thread_id);
  };

  const onRunOnThread = async () => {
    if (!threadId) return;
    setBusy(true);
    setErr("");
    const r = await startProtocolRun(projectId, actor, threadId, { input: parseInput() });
    setBusy(false);
    if (!r.ok) { setErr(r.error); return; }
    setLastRun(r.data);
    setMsg(`Started run ${r.data.run_id} on thread ${threadId}.`);
    void loadHistory(threadId);
  };

  const onCancelRun = async (runId: string) => {
    if (!threadId) return;
    setBusy(true);
    setErr("");
    const r = await cancelProtocolRun(projectId, actor, threadId, runId);
    setBusy(false);
    if (!r.ok) { setErr(r.error); return; }
    setMsg(`Cancelled run ${runId}.`);
    void loadHistory(threadId);
  };

  const onForkRun = async (runId: string, checkpointId?: string) => {
    if (!threadId) return;
    setBusy(true);
    setErr("");
    const r = await forkProtocolRun(projectId, actor, threadId, runId, checkpointId ? { checkpoint_id: checkpointId } : {});
    setBusy(false);
    if (!r.ok) { setErr(r.error); return; }
    setLastRun(r.data);
    setMsg(`Forked run ${runId} -> ${r.data.run_id}.`);
    void loadHistory(threadId);
  };

  if (readError && !readError.ok) {
    return (
      <section className="ct-block ap-wrap" id="ag-sec-endpoints" data-testid="ap-endpoints-sec">
        <SectionLabel level={1} as="h2">Endpoints</SectionLabel>
        <ReadFailure problem={{ status: readError.status, text: readError.error }} onRetry={() => void load()} testId="ap-endpoints-err" />
      </section>
    );
  }

  return (
    <section className="ct-block ap-wrap" id="ag-sec-endpoints" data-testid="ap-endpoints-sec">
      <div className="ap-head">
        <div>
          <SectionLabel level={1} as="h2">Endpoints</SectionLabel>
          <p className="ap-sub">REST, MCP tool, A2A exposure, authorized callers, and live Try-It console.</p>
        </div>
        <div className="ap-grow" />
        <div className="ap-row" role="group" aria-label="Endpoint environment">
          {ENVS.map((e) => (
            <button
              key={e}
              type="button"
              className="ap-chip-btn"
              aria-pressed={env === e}
              data-testid={`ap-ep-env-${e}`}
              onClick={() => setEnv(e)}
            >
              {e}
            </button>
          ))}
        </div>
      </div>

      {msg ? <p className="ap-ok" data-testid="ap-ep-msg">{msg}</p> : null}
      {err ? <p className="ap-err" role="alert" data-testid="ap-ep-err">{err}</p> : null}

      {data && data.rest && data.mcp && data.a2a ? (
        <div className="ap-grid3">
          <div className="ap-card" data-testid="ap-ep-rest">
            <div className="ap-k">REST</div>
            <div className="ap-row">
              <span className="ap-pill" data-tone={data.rest.enabled ? "green" : "amber"}>
                {data.rest.enabled ? `v${data.version ?? 1} live` : "not deployed"}
              </span>
              {data.rest.permission ? <span className="ap-mono ap-sub">{data.rest.permission}</span> : null}
            </div>
            <code className="ap-code" style={{ marginTop: "0.5rem" }}>
              {data.rest.stream || `/v1/agents/${roleKey}/runs/stream`}
            </code>
          </div>

          <div className="ap-card" data-testid="ap-ep-mcp">
            <div className="ap-k">MCP tool</div>
            <div className="ap-row">
              <span className="ap-pill" data-tone={data.mcp.enabled ? "green" : "amber"}>
                {data.mcp.enabled ? "exposed" : "off"}
              </span>
            </div>
            <code className="ap-code" style={{ marginTop: "0.5rem" }}>{data.mcp.tool}</code>
            {data.mcp.note ? <p className="ap-sub" style={{ marginTop: "0.35rem" }}>{data.mcp.note}</p> : null}
          </div>

          <div className="ap-card" data-testid="ap-ep-a2a">
            <div className="ap-k">A2A protocol</div>
            <div className="ap-row">
              <span className="ap-pill" data-tone={data.a2a.enabled ? "green" : "amber"}>
                {data.a2a.enabled ? "enabled" : "disabled"}
              </span>
              <div className="ap-grow" />
              <GatedButton
                permission="agent:deploy"
                type="button"
                className="ap-chip-btn"
                disabled={busy || (data.a2a.may_enable === false && !data.a2a.enabled)}
                data-testid="ap-ep-a2a-toggle"
                onClick={() => void onToggleA2A()}
              >
                {data.a2a.enabled ? "Disable A2A" : "Enable A2A"}
              </GatedButton>
            </div>
            <code className="ap-code" style={{ marginTop: "0.5rem" }}>{data.a2a.card_path}</code>
            {data.a2a.note ? <p className="ap-sub" data-testid="ap-ep-a2a-note" style={{ marginTop: "0.35rem" }}>{data.a2a.note}</p> : null}
          </div>
        </div>
      ) : null}

      <div className="ap-split">
        <div className="ap-card ap-wrap" data-testid="ap-ep-tryit">
          <div className="ap-row">
            <strong>Try it</strong>
            <div className="ap-grow" />
            <button type="button" className="ap-chip-btn" aria-pressed={mode === "stream"} onClick={() => setMode("stream")}>
              SSE stream
            </button>
            <button type="button" className="ap-chip-btn" aria-pressed={mode === "wait"} onClick={() => setMode("wait")}>
              Wait
            </button>
          </div>
          <div className="ap-field">
            <label htmlFor="ap-ep-input">Input JSON</label>
            <textarea
              id="ap-ep-input"
              className="ap-textarea"
              data-testid="ap-ep-input"
              value={promptText}
              onChange={(e) => setPromptText(e.target.value)}
            />
          </div>
          <div className="ap-row">
            <GatedButton
              permission="agent:invoke"
              type="button"
              className="ap-btn"
              data-kind="primary"
              disabled={busy || !data?.rest?.enabled}
              data-testid="ap-ep-run"
              onClick={() => void onTryIt()}
            >
              {busy ? "Running…" : mode === "stream" ? "Stream run" : "Run & wait"}
            </GatedButton>
            <GatedButton
              permission="agent:invoke"
              type="button"
              className="ap-btn"
              disabled={busy || !data?.rest?.enabled}
              data-testid="ap-ep-new-thread"
              onClick={() => void onNewThread()}
            >
              New thread
            </GatedButton>
            {threadId ? (
              <GatedButton
                permission="agent:invoke"
                type="button"
                className="ap-btn"
                disabled={busy}
                data-testid="ap-ep-run-thread"
                onClick={() => void onRunOnThread()}
              >
                Run on {threadId}
              </GatedButton>
            ) : null}
          </div>

          {frames.length > 0 ? (
            <div className="ap-subcard" data-testid="ap-ep-stream-frames">
              <div className="ap-k">Stream frames ({frames.length})</div>
              {frames.map((f, idx) => (
                <div key={idx} className="ap-mono">
                  <strong>{f.event}</strong>: {typeof f.data === "string" ? f.data : JSON.stringify(f.data)}
                </div>
              ))}
            </div>
          ) : null}

          {lastRun ? (
            <div className="ap-subcard" data-testid="ap-ep-run-result">
              <div className="ap-row">
                <span className="ap-mono">{lastRun.run_id}</span>
                <span className="ap-pill" data-tone={lastRun.status === "completed" ? "green" : lastRun.status === "interrupted" ? "amber" : "accent"}>
                  {lastRun.status}
                </span>
              </div>
              {lastRun.output !== undefined ? <pre className="ap-code">{JSON.stringify(lastRun.output, null, 2)}</pre> : null}
              {lastRun.error ? <p className="ap-err">{lastRun.error}</p> : null}
            </div>
          ) : null}
        </div>

        <div className="ap-card ap-wrap">
          <div className="ap-k">Callers ({(data?.callers ?? []).length})</div>
          {data && (data.callers ?? []).length > 0 ? (
            <div className="ap-table-wrap">
              <table className="ap-table" data-testid="ap-ep-callers">
                <thead>
                  <tr><th>Caller</th><th>Via</th><th>Last used</th></tr>
                </thead>
                <tbody>
                  {data.callers.map((c) => (
                    <tr key={c.principal_id + c.via}>
                      <td><strong>{c.name}</strong><div className="ap-mono ap-sub">{c.may}</div></td>
                      <td><span className="ap-pill">{c.via}</span></td>
                      <td className="ap-mono">{formatDate(c.last_used_at, "—")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="ap-sub">No recent callers recorded on this environment.</p>
          )}

          {history ? (
            <div className="ap-wrap" data-testid="ap-ep-thread-history">
              <div className="ap-k">Thread {history.thread_id} history</div>
              {history.runs.map((r) => (
                <div key={r.run_id} className="ap-subcard ap-row">
                  <span className="ap-mono">{r.run_id}</span>
                  <span className="ap-pill">{r.status}</span>
                  <div className="ap-grow" />
                  {r.status === "running" || r.status === "queued" ? (
                    <GatedButton permission="agent:invoke" type="button" className="ap-chip-btn" disabled={busy} onClick={() => void onCancelRun(r.run_id)}>
                      Cancel
                    </GatedButton>
                  ) : null}
                  <GatedButton permission="agent:invoke" type="button" className="ap-chip-btn" disabled={busy} onClick={() => void onForkRun(r.run_id, history.checkpoints[0]?.checkpoint_id)}>
                    Fork
                  </GatedButton>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}
