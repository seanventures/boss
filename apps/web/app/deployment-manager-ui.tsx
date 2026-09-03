"use client";

/**
 * Talk to the Deployment Manager.
 *
 * The role existed on paper — `packages/specpack/roles/deployment_manager.yaml`, with a
 * mission, boundaries and escalation rules — and there was nowhere to say anything to it. The
 * hosts were registered, preflighted and invisible: an operator could see that
 * `hetzner-ci-01` was verified and had no way to ask it anything about that machine.
 *
 * WHAT IT CAN AND CANNOT DO, stated on the panel rather than discovered by asking.
 * It reads: hosts and their preflight results, what is deployed where, deployment history.
 * It does NOT execute on a host. That is the role's own contract — "must not deploy directly",
 * "must escalate staging deploy, production deploy, rollback execution" — and it is the line
 * that keeps a chat message from becoming an unreviewed change on a live machine.
 */

import { useCallback, useEffect, useState } from "react";


type Host = {
  id: string;
  name: string;
  address: string;
  arch?: string | null;
  status?: string | null;
  last_preflight_at?: string | null;
  last_preflight?: { ready?: boolean; problems?: string[]; findings?: string[] } | null;
};

/** Questions worth asking on arrival, so the box is not a blank prompt. */
const STARTERS = (host?: Host) => [
  host
    ? `@Deployment Manager — is ${host.name} (${host.address}) healthy right now, and what is deployed on it?`
    : "@Deployment Manager — what hosts can we deploy onto, and are they healthy?",
  "@Deployment Manager — what shipped most recently, and did it pass its health check?",
  host
    ? `@Deployment Manager — what would it take to deploy this project onto ${host.name}?`
    : "@Deployment Manager — what would it take to deploy this project?",
];

export function DeploymentManagerPanel({ projectId }: { projectId: string }) {
  const [hosts, setHosts] = useState<Host[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/deployment-hosts?project_id=${encodeURIComponent(projectId)}`,
          { cache: "no-store" },
        );
        const d = await r.json().catch(() => ({}));
        if (!live) return;
        if (!r.ok) { setError(d?.error ?? `API returned ${r.status}`); return; }
        setHosts((d.hosts ?? []) as Host[]);
      } catch (e) {
        if (live) setError(String((e as Error)?.message ?? e));
      }
    })();
    return () => { live = false; };
  }, [projectId]);

  // OPEN THE DOCK, DO NOT BECOME ONE.
  //
  // This panel first embedded the whole chat inline — its own thread sidebar, its own hero —
  // which swallowed the Infra area to ask one question about a host. The console already has
  // a side dock for exactly this, driven by `?agent=`; `?ask=` prefills the composer, the same
  // handoff the Agents page uses. Two params, no new chat surface.
  const ask = useCallback((question: string) => {
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("agent", "general");
      url.searchParams.set("ask", question);
      window.history.replaceState(null, "", url.toString());
      window.dispatchEvent(new PopStateEvent("popstate"));
    } catch { /* non-browser */ }
  }, []);

  const verified = hosts.filter((h) => h.status === "verified");

  return (
    <div className="dm-panel">
      <div className="dm-hosts" data-testid="dm-hosts">
        {error ? <p className="detail" style={{ color: "#f85149" }}>{error}</p> : null}
        {!error && hosts.length === 0 ? (
          <p className="detail">
            No hosts registered yet. Register one under CI/CD and the Deployment Manager can
            report on it.
          </p>
        ) : null}
        {hosts.map((h) => {
          const ready = h.last_preflight?.ready;
          return (
            <div className="dm-host" key={h.id} data-testid={`dm-host-${h.id}`}>
              <span className="dm-host-name">{h.name}</span>
              <code className="detail">{h.address}</code>
              <span className="dm-badge" data-tone={
                h.status === "verified" ? "ok" : h.status === "degraded" ? "bad" : "idle"
              }>{h.status ?? "unknown"}</span>
              {h.arch ? <span className="detail">{h.arch}</span> : null}
              <span className="detail">
                {ready === true ? "preflight passed"
                  : ready === false ? "preflight failed"
                  : "not preflighted"}
              </span>
            </div>
          );
        })}
      </div>

      {/* THE LIMIT, SAID UP FRONT. An operator who believes this can restart a service will
          phrase a request as an instruction and read the reply as confirmation. */}
      <p className="dm-scope detail" data-testid="dm-scope">
        Reads hosts, health and deployment history, and recommends. It does not run commands on
        a host: deploys, rollbacks and anything touching production go through the governed
        deploy path with an approval, which is what its role forbids it from bypassing.
      </p>

      <div className="dm-starters">
        {STARTERS(verified[0] ?? hosts[0]).map((q) => (
          <button type="button" className="cap-btn" key={q} onClick={() => ask(q)}
                  data-testid="dm-starter">
            {q.replace("@Deployment Manager — ", "")}
          </button>
        ))}
      </div>

      <div className="dm-starters">
        <button type="button" className="cap-btn" data-testid="dm-open-dock"
                onClick={() => ask("@Deployment Manager — ")}>
          Open the Deployment Manager
        </button>
      </div>
    </div>
  );
}
