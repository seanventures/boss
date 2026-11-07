"use client";

/**
 * INFRA › SERVER (BOSSAPP-914 screen 14): the shared server at a glance, at the top of its Infra
 * drawer — the address and what it is ("Docker host · Ubuntu 22.04 · shared by 3 projects") with
 * Reachable / Unreachable, four tiles (CPU, Memory, Disk rings; Ports used), the projects on it
 * (mark, name, env and port, health, Beacon's face, `Project › Deployments`), the host plugins it
 * runs and the recent changes. Shared stays in Infra; each project row links to that project's
 * Deployments, which links back here ("runs on 203.0.113.20 ↗").
 *
 * Every number is the server's own answer (engine project_experience/host_overview.py): the deploy
 * preflight records it, and `Read now` asks again (one read-only ssh session).
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  ArrowsClockwise, ChartLine, CheckCircle, ClockCounterClockwise, Cube, Database, Folders, Globe, HardDrives, Lock, Plugs,
  RocketLaunch, Stack, WarningCircle, XCircle, type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { consoleHref } from "../console-href";
import { AgentAvatar } from "../agent-avatar";
import { hostOverview, type HostOverview } from "../../lib/host-folder";
import { ago, useNow } from "./infra-util";

const PLUGIN_ICON: Record<string, Icon> = { cube: Cube, stack: Stack, lock: Lock, globe: Globe, chart: ChartLine, database: Database };

/** A capacity ring: the colour turns amber at 70% and red at 90%. */
export function CapRing({ pct, tone, label }: { pct: number | null; tone: "teal" | "blue" | "amber"; label: string }) {
  const p = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  const color = pct === null ? "var(--border-strong)" : p >= 90 ? "var(--red)" : p >= 70 ? "var(--amber)" : `var(--${tone})`;
  const r = 18, c = 2 * Math.PI * r;
  return (
    <svg className="inf-ring" viewBox="0 0 44 44" role="img" aria-label={pct === null ? `${label}: not read yet` : `${label} ${p}%`}>
      <circle cx="22" cy="22" r={r} fill="none" stroke="var(--border)" strokeWidth="4" />
      <circle cx="22" cy="22" r={r} fill="none" stroke={color} strokeWidth="4" strokeLinecap="round"
              strokeDasharray={`${(c * p) / 100} ${c}`} transform="rotate(-90 22 22)" />
      <text x="22" y="25.5" textAnchor="middle">{pct === null ? "—" : `${p}%`}</text>
    </svg>
  );
}

/** Each project's mark keeps one of four token gradients (infra.css `.inf-so-mark[data-mark]`). */
function mark(name: string): string {
  let h = 7;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return String(h % 4);
}

export function ServerOverview({ projectId, hostId, canExec }: { projectId: string; hostId: string; canExec: boolean }) {
  const [data, setData] = useState<HostOverview | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [reading, setReading] = useState(false);
  const [said, setSaid] = useState("");
  const now = useNow(60_000);
  const load = useCallback(() => {
    hostOverview.read(projectId, hostId).then((d) => { setData(d); setState("ready"); })
      .catch(() => setState((s) => (s === "ready" ? s : "error")));
  }, [projectId, hostId]);
  useEffect(() => { setState("loading"); load(); }, [load]);
  if (state === "loading" && !data) return <div className="inf-so inf-so-skel" data-testid="inf-server-loading" aria-busy="true"><span /><span /><span /><span /></div>;
  if (!data) return null;
  const readNow = async () => {
    setReading(true); setSaid("");
    try { await hostOverview.readCapacity(projectId, hostId); load(); setSaid("Read just now."); }
    catch (e) { setSaid(e instanceof Error ? e.message : "The server did not answer."); }
    finally { setReading(false); }
  };
  const cap = data.capacity;
  const reach = data.reachable;
  return (
    <div className="inf-so" data-testid="inf-server-overview">
      <header className="inf-so-head">
        <span className="inf-so-ico" aria-hidden><HardDrives size={22} weight="duotone" /></span>
        <div className="inf-so-title">
          <div className="inf-so-row"><h3 data-copy="data">{data.address || data.name}</h3>
            <span className="inf-so-tag" data-tone={reach.state === "reachable" ? "green" : reach.state === "unreachable" ? "red" : "grey"}
                  data-testid="inf-server-reach" title={reach.reason ?? undefined}>
              <span className="inf-so-dot" aria-hidden />{reach.state === "reachable" ? "Reachable" : reach.state === "unreachable" ? "Unreachable" : "Not checked"}</span></div>
          <span className="inf-so-sub" data-testid="inf-server-sub">{data.sub}{reach.state === "unreachable" && reach.reason ? ` · ${reach.reason}` : ""}</span>
        </div>
      </header>
      <div className="inf-so-tiles" data-testid="inf-server-tiles">
        {([["CPU", cap.cpu, "teal"], ["Memory", cap.memory, "blue"], ["Disk", cap.disk, "amber"]] as const).map(([label, v, tone]) => (
          <div key={label} className="inf-so-tile" data-testid={`inf-server-${label.toLowerCase()}`}>
            <span className="inf-so-lbl">{label}</span>
            <div className="inf-so-row"><CapRing pct={v.pct} tone={tone} label={label} /><span className="inf-so-cap">{v.label ?? "not read yet"}</span></div>
          </div>
        ))}
        <div className="inf-so-tile" data-testid="inf-server-ports">
          <span className="inf-so-lbl">Ports used</span>
          <b className="inf-so-num">{cap.ports.used}</b>
          <span className="inf-so-cap">{cap.ports.range ?? "any port"}</span>
        </div>
      </div>
      <div className="inf-so-read">
        {cap.at && now !== null ? <span className="inf-faint">read {ago(cap.at, now)}</span> : <span className="inf-faint">Capacity not read yet</span>}
        {canExec ? (
          <GatedButton permission="host:exec" projectId={projectId} type="button" className="inf-link" data-testid="inf-server-read"
                       disabled={reading} title="Ask the server now (read-only)" onClick={() => void readNow()}>
            <ArrowsClockwise size={11} aria-hidden /> {reading ? "Reading…" : "Read now"}</GatedButton>
        ) : null}
        {said ? <span className="inf-faint" role="status">{said}</span> : null}
      </div>

      <section className="inf-so-card" data-testid="inf-server-projects" aria-label="Projects on this server">
        <h4><span className="inf-so-ico inf-so-ico-s" aria-hidden><Folders size={15} /></span>Projects on this server
          <span className="inf-so-badge">{data.shared_by}</span></h4>
        {data.projects.length ? data.projects.map((p) => (
          <div key={p.target_id} className="inf-so-proj" data-testid={`inf-host-project-${p.id}`}>
            <span className="inf-so-mark" data-mark={mark(p.name)} aria-hidden>{p.initials}</span>
            <span className="inf-so-pname"><b>{p.name}</b><small>{p.env}{p.port ? <span className="inf-mono"> :{p.port}</span> : null}</small></span>
            <span className="ag-spacer" />
            <span className="inf-so-tag" data-tone={p.health.tone === "green" ? "green" : p.health.tone === "red" ? "red" : p.health.tone === "amber" ? "amber" : "grey"}>
              <span className="inf-so-dot" aria-hidden />{p.health.word}</span>
            <AgentAvatar agent={p.agent.role_key} size={22} state={p.agent.live ? "working" : "idle"} aiBadge={false} tip={`${p.agent.name}${p.agent.live ? " · deploying now" : ""}`} />
            <a className="inf-so-chip" href={consoleHref({ project_id: p.id, area: "project", tab: "deployments", host: undefined, view: undefined })}
               data-console-load data-testid={`inf-host-project-link-${p.id}`} title={`${p.name}: its deployments, demo logins and health`}>
              <RocketLaunch size={11} aria-hidden /> Project › Deployments</a>
          </div>
        )) : <span className="inf-faint inf-so-sub">No projects on this server yet</span>}
      </section>

      <div className="inf-so-pair">
        <section className="inf-so-card" data-testid="inf-server-plugins" aria-label="Host plugins">
          <h4><span className="inf-so-ico inf-so-ico-s" data-c="blue" aria-hidden><Plugs size={15} /></span>Host plugins</h4>
          <div className="inf-so-chips">
            {data.plugins.length ? data.plugins.map((p) => {
              const I = PLUGIN_ICON[p.icon] ?? Cube;
              return <span key={p.key} className="inf-so-pchip"><I size={11} aria-hidden /> {p.label}</span>;
            }) : <span className="inf-faint">Read on the next deploy or Read now</span>}
          </div>
        </section>
        <section className="inf-so-card" data-testid="inf-server-changes" aria-label="Recent changes">
          <h4><span className="inf-so-ico inf-so-ico-s" data-c="violet" aria-hidden><ClockCounterClockwise size={15} /></span>Recent changes</h4>
          {data.changes.length ? data.changes.map((c) => (
            <a key={c.id} className="inf-so-change" href={consoleHref({ project_id: c.href.match(/project_id=([^&]+)/)?.[1], area: "project", tab: "deployments", host: undefined, view: undefined })} data-console-load>
              <span className="inf-so-ck" data-s={c.state} aria-hidden>{c.state === "done" ? <CheckCircle size={14} weight="fill" /> : c.state === "failed" ? <XCircle size={14} weight="fill" /> : <WarningCircle size={14} />}</span>
              <span><b>{c.label}</b><small>{c.at && now !== null ? `${ago(c.at, now)} · ` : ""}{c.by}</small></span>
            </a>
          )) : <span className="inf-faint">Nothing deployed here yet</span>}
        </section>
      </div>
    </div>
  );
}
