"use client";

/**
 * Talk to the Deployment Manager.
 *
 * The role existed on paper — `packages/specpack/roles/deployment_manager.yaml`, with a
 * mission, boundaries and escalation rules — and there was nowhere to say anything to it. The
 * hosts were registered, preflighted and invisible: an operator could see that
 * `hetzner-ci-01` was verified and had no way to ask it anything about that machine.
 *
 * WHAT IT CAN DO, stated on the panel rather than discovered by asking.
 * It reads hosts, preflight results, what is deployed where, and deployment history. It can
 * also run commands on a host — and never on its own initiative: `run_on_host` refuses a
 * model's direct call and tells it to propose, so the command arrives as a button showing the
 * exact host and the exact command, and nothing happens until the operator clicks.
 *
 * That is the distinction the operator drew after an autonomous agent destroyed the database:
 * an agent working WITH a person can be trusted with real power, because the person is the
 * gate. An agent acting alone is a different thing entirely.
 */

import { useCallback, useEffect, useState } from "react";

import { openDockAbout } from "../lib/dock-subject";


type Host = {
  id: string;
  name: string;
  address: string;
  arch?: string | null;
  status?: string | null;
  last_preflight_at?: string | null;
  last_preflight?: { ready?: boolean; problems?: string[]; findings?: string[] } | null;
};

/** Questions worth asking on arrival, so the box is not a blank prompt.
 *
 *  No "@Deployment Manager —" prefix any more: the dock says whose conversation this is, and
 *  addressing the role inside the message as well made the operator's first sentence read like
 *  it was written by the console. */
const STARTERS = (host?: Host) => [
  host
    ? `Is ${host.name} (${host.address}) healthy right now, and what is deployed on it?`
    : "What hosts can we deploy onto, and are they healthy?",
  "What shipped most recently, and did it pass its health check?",
  host
    ? `What would it take to deploy this project onto ${host.name}?`
    : "What would it take to deploy this project?",
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

  // OPEN THE DOCK, DO NOT BECOME ONE — and hand the question over as `dock_ask`.
  //
  // `?ask=` looked like the right handoff and silently lost: it is consumed by whichever
  // AgentChat reads it first, and the BOSS Agent area stays mounted, so the starter was
  // swallowed by an instance off-screen and the dock opened with an empty composer.
  //
  // This panel first embedded the whole chat inline — its own thread sidebar, its own hero —
  // which swallowed the Infra area to ask one question about a host. The console already has
  // a side dock for exactly this, driven by `?agent=`; `?ask=` prefills the composer, the same
  // handoff the Agents page uses. Two params, no new chat surface.
  const ask = useCallback((question: string) => {
    // ADDRESS IT, DO NOT INHERIT IT.
    //
    // `dock_role` is a URL parameter, so it SURVIVES until something overwrites it. This
    // panel set the question and not the role, so pressing "Open the Deployment Manager"
    // opened whichever role the operator had last addressed — reported as a Deployment
    // Manager card that opened the Security Reviewer chat, ready to answer about hosts and
    // rollbacks as the wrong agent. Every door into the dock names its own role.
    //
    // No subject: these starters are about the fleet as a whole, not one record.
    openDockAbout({ subject: null, question, role: "Deployment Manager" });
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
        Reads hosts, health and deployment history. Deployment target bootstrap and SSH
        workload delivery are paused in Community while their safeguards are completed.
      </p>

      <div className="dm-starters">
        {STARTERS(verified[0] ?? hosts[0]).map((q) => (
          <button type="button" className="cap-btn" key={q} onClick={() => ask(q)}
                  data-testid="dm-starter">
            {q}
          </button>
        ))}
      </div>

      <div className="dm-starters">
        <button type="button" className="cap-btn" data-testid="dm-open-dock"
                onClick={() => ask("")}>
          Open the Deployment Manager
        </button>
      </div>
    </div>
  );
}
