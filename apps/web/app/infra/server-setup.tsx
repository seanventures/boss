"use client";

/**
 * THE SERVER PAGE, SETTING ITSELF UP (agentic-factory 3-add-a-server.html §2): where Add and set
 * up lands. The diagram (key landed → read → install → verify → ready → watching), the Now line
 * with the live log, the gate while the key is awaited (the one line, Copy; the agent sees it
 * land, so there is no "I ran it"), then the server's rings with free ports and the disk alert line. A server never
 * moves an app: each app picks it on its own Project › Deployments page.
 *
 * The old stepper stays one press away ("Do it by hand") for anyone who wants it.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  ArrowSquareOut, CheckCircle, Eye, Info, Key, MagnifyingGlass, Package, ShieldCheck, Terminal, Warning,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { useServerSetup, type SetupStep } from "../../lib/add-server";
import { notifyChanged } from "../changed";
import { CopyButton, localTime } from "./infra-util";
import { DrawerSection } from "./kit";
import { ServerOverview } from "./server-overview";
import "./add-server.css";

const ICON: Record<string, React.ReactNode> = {
  key: <Key size={16} aria-hidden />, read: <MagnifyingGlass size={16} aria-hidden />, install: <Package size={16} aria-hidden />,
  verify: <ShieldCheck size={16} aria-hidden />, ready: <CheckCircle size={16} aria-hidden />, watching: <Eye size={16} aria-hidden />,
};

function Node({ s }: { s: SetupStep }) {
  return (
    <li className="ss-node" data-state={s.state} data-testid={`ss-step-${s.key}`} title={s.detail ?? undefined}>
      <span className="ss-dot">{s.state === "blocked" ? <Warning size={16} aria-hidden /> : ICON[s.key]}</span>
      <b>{s.label}</b>
      <span>{s.detail ?? ""}</span>
    </li>
  );
}

export function ServerSetup({ hostId, projectId, canExec, onAskBoss }: {
  hostId: string; projectId: string; canExec: boolean; onAskBoss?: () => void;
}) {
  const setup = useServerSetup(hostId);
  const [logOpen, setLogOpen] = useState(false);
  const v = setup.data;
  // A STEP THE AGENT TOOK SHOWS EVERYWHERE AT ONCE: the drawer's state word, the capacity cards
  // and the fleet re-read when the phase moves, instead of each on its own clock.
  const phase = useRef<string | null>(null);
  useEffect(() => {
    if (!v) return;
    if (phase.current !== null && phase.current !== v.phase) notifyChanged("infra");
    phase.current = v.phase;
  }, [v]);
  // ONE SET OF NUMBERS: the server's rings (CPU, memory, disk, ports) carry what setup adds,
  // free ports and the disk alert line, instead of a second row of cards.
  const glance = <ServerOverview projectId={projectId} hostId={hostId} canExec={canExec}
                                 setup={v ? { freePorts: v.stats.freePorts, alertPct: v.stats.alertPct } : undefined} />;
  if (!v) return glance;
  return (
    <>
    <DrawerSection label="SETUP" testId="ss">
      <div className="ss" data-phase={v.phase} data-state={v.state}>
        <div className="ss-head">
          <h3 data-testid="ss-title">{v.name} · <span data-copy="data">{v.address}</span></h3>
          <span className="as-tag" data-tone={v.ready ? "green" : v.state === "needs you" ? "amber" : "accent"} data-testid="ss-state">{v.state}</span>
          <span className="ag-spacer" />
          <span className="as-owner"><AgentAvatar agent="infra" size={20} aiBadge={false} tip="Infra agent" /> Infra agent</span>
        </div>
        <ol className="ss-flow" aria-label="Setup" data-testid="ss-flow">{v.steps.map((s) => <Node key={s.key} s={s} />)}</ol>
        {v.gate ? (
          <div className="ss-gate" data-testid="ss-gate">
            <h4><span className="ss-hand" aria-hidden>✋</span> Run this one line on {v.gate.address} as {v.gate.user}</h4>
            <code className="as-code" data-copy="data" data-testid="ss-gate-line">{v.gate.line}</code>
            <div className="as-row">
              <CopyButton text={v.gate.line} label="Copy the line" testId="ss-gate-copy" tip="Adds only BOSS's public key to authorized_keys" />
              <span className="as-faint">The agent tries the key every few seconds and goes on the moment it lands.</span>
            </div>
            {v.gate.fromLaptop ? (
              <details className="as-faint" data-testid="ss-gate-more">
                <summary>From your laptop instead</summary>
                <code className="as-code" data-copy="data" data-testid="ss-gate-laptop">{v.gate.fromLaptop}</code>
                <CopyButton text={v.gate.fromLaptop} label="Copy" testId="ss-gate-laptop-copy" />
              </details>
            ) : null}
          </div>
        ) : null}
        <div className="ss-now" data-tone={v.now.tone} data-testid="ss-now">
          <AgentAvatar agent="infra" size={20} aiBadge={false} tip="Infra agent" />
          <b>Now</b><span className="ss-now-text" data-testid="ss-now-text">{v.now.text}</span>
          <button type="button" className="inf-link" aria-expanded={logOpen} data-testid="ss-log-toggle" onClick={() => setLogOpen((o) => !o)}>
            <Terminal size={12} aria-hidden /> Live log</button>
          {onAskBoss ? <button type="button" className="inf-link" data-testid="ss-ask" onClick={onAskBoss}>Ask BOSS <ArrowSquareOut size={11} aria-hidden /></button> : null}
        </div>
        {logOpen ? (
          <ol className="ss-log" aria-label="Live log" data-testid="ss-log">
            {v.log.map((l, i) => <li key={`${l.at}-${i}`}><time dateTime={l.at}>{localTime(l.at)}</time>{l.line}</li>)}
          </ol>
        ) : null}
        <span className="ss-note" data-testid="ss-note"><Info size={13} aria-hidden />
          Adding a server never moves an app. Each app picks its servers on its own page (Project › Deployments), where this one shows as ready. Prod is never picked for you.</span>
      </div>
    </DrawerSection>
    {glance}
    </>
  );
}
