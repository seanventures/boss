"use client";

/**
 * Deploying, watched.
 *
 * The old console could not show progress because the deployment reported nothing until it
 * ended — every spinner was an animation over an empty data source, which is why it read as
 * broken. Deployments are now asynchronous and record each step as it happens, so this file is
 * a renderer of real state rather than a decoration.
 *
 * Three parts:
 *   LiveDeployment    — polls a running deployment and draws its steps, timings and logs
 *   DeployWizard      — what to deploy, where, confirm; then hands over to LiveDeployment
 *   RecentDeployments — the last N DEPLOYMENTS (not every code task that opened a PR)
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  deployTargetAction, getDeliveryDefaultsAction, getDeploymentAction, listRefsAction,
  redeployAction,
} from "./actions";

type Step = {
  step: string; label: string; status: "pending" | "running" | "ok" | "failed" | "skipped";
  duration_ms?: number | null; log?: string; error?: string;
};
type Run = {
  deployment_run_id: string; status: string; app_name?: string; environment?: string;
  commit_ref?: string | null; trigger?: string | null; failure_reason?: string | null;
  duration_ms?: number | null; url?: string | null; steps: Step[]; running: boolean;
};

const box: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,.09)", borderRadius: 8, padding: 12,
};
const btn: React.CSSProperties = {
  fontSize: 12, padding: "5px 12px", borderRadius: 6, cursor: "pointer",
  border: "1px solid rgba(255,255,255,.18)", background: "transparent", color: "inherit",
};
const input: React.CSSProperties = {
  fontSize: 12, padding: "5px 8px", borderRadius: 6, background: "rgba(255,255,255,.04)",
  border: "1px solid rgba(255,255,255,.14)", color: "inherit", minWidth: 170,
};

const ms = (v?: number | null) =>
  v == null ? "" : v < 1000 ? `${v}ms` : v < 60000 ? `${(v / 1000).toFixed(1)}s`
    : `${Math.floor(v / 60000)}m ${Math.round((v % 60000) / 1000)}s`;

/** Spinner drawn with SVG SMIL — this console applies a per-area CSS allow-list, so a new
 *  keyframe would need registering in three places and silently do nothing until it was. */
function Spin({ size = 13 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" aria-hidden="true">
      <circle cx="20" cy="20" r="16" fill="none" stroke="currentColor" strokeWidth="5"
              opacity="0.2" />
      <path d="M20 4 a16 16 0 0 1 16 16" fill="none" stroke="currentColor" strokeWidth="5"
            strokeLinecap="round">
        <animateTransform attributeName="transform" type="rotate" from="0 20 20" to="360 20 20"
                          dur="0.9s" repeatCount="indefinite" />
      </path>
    </svg>
  );
}

const MARK: Record<Step["status"], { icon: string; color: string }> = {
  pending: { icon: "○", color: "#6e7681" },
  running: { icon: "", color: "#58a6ff" },
  ok: { icon: "✓", color: "#3fb950" },
  failed: { icon: "✕", color: "#f85149" },
  skipped: { icon: "–", color: "#8b949e" },
};

/** One step: state, how long it took, and its log when it matters. */
function StepRow({ s, elapsed }: { s: Step; elapsed: number }) {
  // The failed step is the only one anyone opens, so it opens itself.
  const [open, setOpen] = useState(s.status === "failed");
  useEffect(() => { if (s.status === "failed") setOpen(true); }, [s.status]);
  const mark = MARK[s.status] ?? MARK.pending;
  const hasLog = !!(s.log || s.error);

  return (
    <div style={{ borderTop: "1px solid rgba(255,255,255,.06)", padding: "7px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13 }}>
        <span style={{ color: mark.color, width: 14, display: "inline-flex" }}>
          {s.status === "running" ? <Spin /> : mark.icon}
        </span>
        <span style={{ opacity: s.status === "pending" ? 0.5 : 1, flex: 1 }}>{s.label}</span>
        <span className="detail" style={{ fontSize: 12 }}>
          {/* A running step shows the CLOCK, not a guess: it is the only honest number while
              the work is still going. */}
          {s.status === "running" ? `${elapsed}s` : ms(s.duration_ms)}
        </span>
        {hasLog && (
          <button onClick={() => setOpen(!open)}
                  style={{ ...btn, padding: "1px 7px", fontSize: 11 }}>
            {open ? "hide" : "log"}
          </button>
        )}
      </div>
      {s.error && (
        <div style={{ color: "#f85149", fontSize: 12, margin: "4px 0 0 24px" }}>{s.error}</div>
      )}
      {open && s.log && (
        <pre style={{ margin: "6px 0 0 24px", fontSize: 11, whiteSpace: "pre-wrap",
                      background: "rgba(255,255,255,.04)", padding: 8, borderRadius: 6,
                      maxHeight: 220, overflow: "auto" }}>{s.log}</pre>
      )}
    </div>
  );
}

export function LiveDeployment({ runId, onFinished }: {
  runId: string; onFinished?: (run: Run) => void;
}) {
  const [run, setRun] = useState<Run | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const done = useRef(false);

  // Poll while running. Stops on a terminal state rather than polling a finished deployment
  // forever — a console that keeps asking after the answer is final is just noise on the API.
  useEffect(() => {
    let live = true;
    const tick = async () => {
      const d = await getDeploymentAction(runId);
      if (!live || !d || d.error) return;
      setRun(d);
      if (!d.running && !done.current) { done.current = true; onFinished?.(d); }
    };
    tick();
    const poll = setInterval(() => { if (!done.current) tick(); }, 2000);
    const clock = setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => { live = false; clearInterval(poll); clearInterval(clock); };
  }, [runId, onFinished]);

  if (!run) {
    return (
      <div style={box}>
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", fontSize: 13 }}>
          <Spin /> starting deployment…
        </span>
      </div>
    );
  }

  const failed = run.status === "failed" || run.status === "blocked";
  const tone = run.running ? "#58a6ff" : failed ? "#f85149" : "#3fb950";
  const runningStep = run.steps.find((s) => s.status === "running");

  return (
    <div style={{ ...box, ...(failed ? { borderColor: "rgba(248,81,73,.35)" } : {}) }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ color: tone, fontWeight: 600, display: "inline-flex",
                       alignItems: "center", gap: 7 }}>
          {run.running && <Spin size={14} />}
          {run.running ? "Deploying" : failed ? "Deployment failed" : "Deployed"}
        </span>
        <strong style={{ fontSize: 13 }}>{run.app_name}</strong>
        {run.environment && <span className="cap-badge tone-blue">{run.environment}</span>}
        {run.commit_ref && (
          <span className="detail" style={{ fontFamily: "monospace", fontSize: 12 }}>
            {run.commit_ref.slice(0, 12)}
          </span>
        )}
        <span style={{ flex: 1 }} />
        <span className="detail" style={{ fontSize: 12 }}>
          {run.running ? `${elapsed}s elapsed` : `took ${ms(run.duration_ms)}`}
        </span>
      </div>

      {run.running && runningStep && (
        <div className="detail" style={{ fontSize: 12, marginTop: 4 }}>
          {runningStep.label}…
        </div>
      )}

      <div style={{ marginTop: 8 }}>
        {run.steps.map((s) => (
          <StepRow key={s.step} s={s}
                   elapsed={s.status === "running" ? elapsed : 0} />
        ))}
      </div>

      {!run.running && !failed && run.url && (
        <div style={{ marginTop: 10 }}>
          <a href={`http://${run.url}/`} target="_blank" rel="noreferrer"
             style={{ fontSize: 13 }}>Open {run.url} ↗</a>
        </div>
      )}
      {/* The run-level reason ONLY when no step owns the failure. When a step failed it has
          already shown the same text twice over (its error line and its log), and printing it
          a third time reads as three different problems. */}
      {failed && run.failure_reason && !run.steps.some((s) => s.status === "failed") && (
        <div className="detail" style={{ color: "#f85149", fontSize: 12, marginTop: 8 }}>
          {readableFailure(run.failure_reason).slice(0, 400)}
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Deploy wizard
// --------------------------------------------------------------------------- //
type WizardTarget = {
  id: string; app_name: string; environment: string; repo: string; status: string;
  repo_owner?: string | null; domains?: string[] | null; app_entry?: string | null;
  auto_deploy?: boolean; paused?: boolean;
};

export function DeployWizard({ targets }: { targets: WizardTarget[] }) {
  const [open, setOpen] = useState(false);
  const [targetId, setTargetId] = useState("");
  const [ref, setRef] = useState("");
  const [refs, setRefs] = useState<{ name: string; sha: string }[]>([]);
  const [refsError, setRefsError] = useState<string | null>(null);
  const [defaultOwner, setDefaultOwner] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const active = targets.filter((t) => t.status === "active");
  const target = active.find((t) => t.id === targetId);

  // Who owns the repo BOSS clones when a target does not say. Fetched once, on open.
  useEffect(() => {
    if (!open || defaultOwner) return;
    getDeliveryDefaultsAction().then((d) => { if (d?.owner) setDefaultOwner(d.owner); });
  }, [open, defaultOwner]);

  /** Branches for a target's repo.
   *
   * The owner comes from the repo string, else the target, else the project default. The first
   * version used only the repo string — which for `boss-sandbox` produced an empty owner, a
   * request to `/repos//boss-sandbox/refs`, a 404, and a branch picker that silently listed
   * nothing. A picker that cannot fail visibly is worse than a plain text box. */
  const loadRefs = useCallback(async (t: WizardTarget, fallbackOwner: string) => {
    const repo = t.repo || "";
    if (!repo) return;
    const [owner, name] = repo.includes("/")
      ? [repo.split("/")[0], repo.split("/").slice(1).join("/")]
      : [t.repo_owner || fallbackOwner, repo];
    if (!owner) { setRefsError("no repo owner configured for this app"); return; }
    setRefsError(null);
    const d = await listRefsAction(owner, name);
    if (d?.error || !d?.branches) {
      setRefsError(`could not list branches for ${owner}/${name}`);
      return;
    }
    setRefs(d.branches);
    if (d.default_branch) setRef((r) => r || d.default_branch);
  }, []);

  const start = async () => {
    if (!targetId) { setErr("choose which app to deploy"); return; }
    setErr(null); setStarting(true);
    const out = await deployTargetAction(targetId, ref);
    setStarting(false);
    if (out?.error) {
      setErr(typeof out.error === "string" ? out.error : JSON.stringify(out.error).slice(0, 220));
      return;
    }
    // Asynchronous: the response carries an id to watch, not a finished deployment.
    setRunId(out.deployment_run_id);
  };

  if (!open) {
    return <button style={{ ...btn, padding: "7px 16px" }} onClick={() => setOpen(true)}>
      Deploy an app
    </button>;
  }

  return (
    <section className="ct-block">
      <div className="ct-block-title">Deploy</div>

      {!runId && (
        <div style={{ ...box, display: "grid", gap: 12 }}>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
            <label style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>app</span>
              <select style={input} value={targetId}
                      onChange={(e) => {
                        setTargetId(e.target.value);
                        const t = active.find((x) => x.id === e.target.value);
                        setRefs([]); setRef(""); setRefsError(null);
                        if (t) loadRefs(t, defaultOwner);
                      }}>
                <option value="">— choose —</option>
                {active.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.app_name} · {t.environment}
                  </option>
                ))}
              </select>
            </label>

            <label style={{ display: "grid", gap: 3 }}>
              <span style={{ opacity: .55, fontSize: 11, fontFamily: "monospace" }}>
                branch or sha
              </span>
              {/* A real select once the branches are known: a datalist looks like an empty
                  text box when the fetch fails, which is precisely how a broken picker hides.
                  Falls back to free text so a raw sha is still deployable. */}
              {refs.length > 0 ? (
                <select style={input} value={ref} onChange={(e) => setRef(e.target.value)}>
                  {/* BOSS leaves a `boss/code/crun_…` branch behind for every code task it has
                      ever run — thirty of them here, all named after an id nobody recognises.
                      Grouped rather than dropped: deploying one is legitimate (that is how you
                      ship a fix before it merges), but it must not bury `main`. */}
                  {refs.filter((r) => !r.name.startsWith("boss/")).map((r) => (
                    <option key={r.name} value={r.name}>{r.name} · {r.sha.slice(0, 7)}</option>
                  ))}
                  {refs.some((r) => r.name.startsWith("boss/")) && (
                    <optgroup label="BOSS work branches">
                      {refs.filter((r) => r.name.startsWith("boss/")).map((r) => (
                        <option key={r.name} value={r.name}>
                          {r.name} · {r.sha.slice(0, 7)}
                        </option>
                      ))}
                    </optgroup>
                  )}
                </select>
              ) : (
                <input style={input} value={ref} placeholder="main"
                       onChange={(e) => setRef(e.target.value)} />
              )}
            </label>
          </div>

          {refsError && (
            <div className="detail" style={{ fontSize: 12, color: "#d29922" }}>
              {refsError} — type a branch or sha instead.
            </div>
          )}

          {/* What WILL happen, before it happens. A deploy is easier to authorise than to undo. */}
          {target && (
            <div className="detail" style={{ fontSize: 12 }}>
              Deploy <strong>{target.app_name}</strong> from{" "}
              <code>{target.repo_owner || defaultOwner || "?"}/{target.repo}@
              {ref || "default branch"}</code>
              {target.app_entry && <> (<code>{target.app_entry}</code>)</>} to{" "}
              <code>{target.environment}</code>
              {target.domains?.[0] && <> at <code>{target.domains[0]}</code></>}. BOSS will
              build, transfer, start the container and health-check it — usually under a minute.
            </div>
          )}

          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <button style={btn} disabled={starting} onClick={start}>
              {starting ? "starting…" : "Deploy"}
            </button>
            <button style={btn} onClick={() => setOpen(false)}>Cancel</button>
            {err && <span className="detail" style={{ color: "#f85149", fontSize: 12 }}>{err}</span>}
          </div>
        </div>
      )}

      {runId && (
        <div style={{ display: "grid", gap: 10 }}>
          <LiveDeployment runId={runId} />
          <div>
            <button style={btn} onClick={() => { setRunId(null); setErr(null); }}>
              Deploy another
            </button>
            <button style={{ ...btn, marginLeft: 8 }} onClick={() => setOpen(false)}>Close</button>
          </div>
        </div>
      )}
    </section>
  );
}


/** A failure reason a person can read.
 *
 * The health verdict is stored as the raw JSON the checker produced, so a failed deployment
 * reported `{"healthy": false, "checks": [{"url": ..., "error": ...}]}` — accurate, and the
 * kind of thing an operator has to decode rather than read. The JSON is still one click away
 * on the failing step; the row gets the sentence.
 */
function readableFailure(reason: string): string {
  const text = reason.trim();
  if (!text.startsWith("{")) return text;
  try {
    const parsed = JSON.parse(text);
    const check = (parsed.checks || [])[0];
    if (!check) return text;
    const what = check.error
      ? check.error
      : `returned ${check.status}, expected ${check.expected}`;
    return `health check failed — ${check.url || "the app"} ${what}`;
  } catch {
    return text;
  }
}

// --------------------------------------------------------------------------- //
// Recent deployments
// --------------------------------------------------------------------------- //
type Deployment = {
  id: string; status: string; app_name: string; environment: string;
  commit_ref: string | null; trigger: string | null; duration_ms: number | null;
  finished_at: string | null; failure_reason: string | null; url: string | null;
};

export function RecentDeployments({ deployments }: { deployments?: Deployment[] | null }) {
  const [openId, setOpenId] = useState<string | null>(null);
  // A redeploy is a NEW run, so the row that started it is not the row to watch.
  const [startedFrom, setStartedFrom] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const rows = deployments ?? [];

  const again = async (id: string) => {
    setBusy(id);
    const out = await redeployAction(id);
    setBusy(null);
    if (out?.deployment_run_id) {
      setStartedFrom((m) => ({ ...m, [id]: out.deployment_run_id }));
      setOpenId(null);
    }
  };

  const tone = (s: string) =>
    s === "succeeded" ? "#3fb950" : ["failed", "blocked"].includes(s) ? "#f85149"
      : s === "running" ? "#58a6ff" : "#8b949e";
  // `created` is what a plan-only run is called in the database. Shown as "planned", because
  // "created" alongside "succeeded" and "failed" reads as a deployment that went nowhere
  // rather than one that was never asked to go anywhere.
  const label = (s: string) => (s === "created" ? "planned" : s);

  return (
    <section className="ct-block">
      <div className="ct-block-title">Recent deployments</div>
      {rows.length === 0 && (
        <div className="detail">Nothing has been deployed yet.</div>
      )}
      {rows.map((d) => (
        <div key={d.id} style={{ borderTop: "1px solid rgba(255,255,255,.07)", padding: "8px 0" }}>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap",
                        fontSize: 13 }}>
            <span style={{ color: tone(d.status), fontWeight: 600, minWidth: 78 }}>
              {label(d.status)}
            </span>
            <strong>{d.app_name}</strong>
            <span className="cap-badge tone-blue">{d.environment}</span>
            <span className="detail" style={{ fontFamily: "monospace", fontSize: 12 }}>
              {d.commit_ref ? d.commit_ref.slice(0, 12) : "—"}
            </span>
            <span className="detail" style={{ fontSize: 12 }}>{d.trigger || "—"}</span>
            <span className="detail" style={{ fontSize: 12 }}>{ms(d.duration_ms) || "—"}</span>
            <span style={{ flex: 1 }} />
            {/* Steps and logs are one click away for ANY past deployment, not only the one
                you happened to be watching when it ran. */}
            <button style={{ ...btn, padding: "2px 9px", fontSize: 11 }}
                    onClick={() => setOpenId(openId === d.id ? null : d.id)}>
              {openId === d.id ? "hide steps" : "steps & logs"}
            </button>
            {/* "Run that again" without retyping what it was. A new run, so this row's history
                survives instead of being overwritten by its own replacement. */}
            <button style={{ ...btn, padding: "2px 9px", fontSize: 11 }}
                    disabled={busy === d.id}
                    title="Ship this target again as a new deployment"
                    onClick={() => again(d.id)}>
              {busy === d.id ? "starting…" : "redeploy"}
            </button>
          </div>
          {startedFrom[d.id] && (
            <div style={{ marginTop: 8 }}>
              <LiveDeployment runId={startedFrom[d.id]} />
            </div>
          )}
          {d.failure_reason && openId !== d.id && (
            <div className="detail" style={{ color: "#f85149", fontSize: 12, marginTop: 3 }}>
              {readableFailure(d.failure_reason).slice(0, 200)}
            </div>
          )}
          {openId === d.id && (
            <div style={{ marginTop: 8 }}><LiveDeployment runId={d.id} /></div>
          )}
        </div>
      ))}
    </section>
  );
}
