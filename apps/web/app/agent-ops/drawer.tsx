"use client";

/**
 * THE ONLY PLACE TEXT LIVES (§11.0 rule 3). A node's evidence, rationale, thread and receipts,
 * one click away from the canvas: a signal's facts, an agent's messages, a proposal and its
 * votes, a decision's question → chosen vs rejected → why → consensus (each vote a face, the
 * dissent's reason in full) → evidence → undo → cost, and the run it started. Override lives on
 * a decision (decision:approve); the answer to Needs you opens Approvals, the one queue.
 */
import { changeHref, changeViewPath, isLocalRepo, parseLocalChange, repoHref } from "../../lib/repo-links";
import React, { useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { decisionHref, runHref } from "../../lib/command-center";
import { kgNodeHref } from "../../lib/kg-view";
import {
  aaRead, decisionPath, money, microsUsd, overrideDecision, parseDecision, parseSignals, partyOfNode, roleOfParty, signalsPath,
  LEVEL_WORDS, type Decision, type Evidence, type FlowEdge, type FlowNode, type Graph, type Message, type Signal,
} from "../../lib/ambient";
import { AgentAvatar } from "../agent-avatar";
import { showToast } from "../client-forms";
import { NodeFace, TYPE_WORDS } from "./flow-canvas";

export type Target = { node?: FlowNode; edge?: FlowEdge };

function partyName(who: string, graph: Graph): string {
  return graph.nodes.find((n) => partyOfNode(n) === who)?.label ?? who.replace(/^[a-z]+:/, "").replace(/_/g, " ");
}

function Face({ who, size = 22 }: { who: string; size?: number }) {
  const role = roleOfParty(who);
  if (role) return <AgentAvatar agent={role} size={size} aiBadge={false} />;
  if (who.startsWith("person:")) return <AgentAvatar variant="person" size={size} />;
  return <AgentAvatar variant="boss" size={size} />;
}

export function evidenceHref(e: Evidence, orgId?: string | null): string | null {
  if (e.kind === "url" && /^https?:\/\//.test(e.ref)) return e.ref;
  if (e.kind === "pull_request") {
    if (/^https?:\/\//.test(e.ref)) return changeHref(e.ref);
    const m = e.ref.match(/^([\w.-]+\/[\w.-]+)#(\d+)$/);
    if (!m) return null;
    // A local repository's change set opens in BOSS; a GitHub one on GitHub.
    const local = isLocalRepo(m[1]) ? parseLocalChange(`http://boss-local/${m[1]}/pull/${m[2]}`) : null;
    return local ? changeViewPath(local.projectId, local.number) : `${repoHref(m[1])}/pull/${m[2]}`;
  }
  if (e.kind === "workflow_run") return runHref(e.ref, orgId);
  if (e.kind === "kg_node") return kgNodeHref(e.ref, orgId);
  if (e.kind === "decision") return decisionHref(e.ref, orgId);
  return null;
}

function EvidenceList({ items, orgId }: { items: Evidence[]; orgId?: string | null }) {
  if (!items.length) return null;
  return (
    <ul className="aa-evidence" data-testid="aa-evidence">
      {items.map((e, i) => {
        const href = evidenceHref(e, orgId);
        const body = <><span className="aa-ev-kind">{e.kind.replace(/_/g, " ")}</span><code>{e.ref}</code>{e.note ? <span> · {e.note}</span> : null}</>;
        return <li key={`${e.kind}-${e.ref}-${i}`}>{href ? <a href={href} target={href.startsWith("http") ? "_blank" : undefined} rel="noreferrer">{body}</a> : body}</li>;
      })}
    </ul>
  );
}

const bodyText = (m: Message) => String(m.body.summary ?? m.body.reason ?? m.body.vote ?? m.body.position ?? m.body.action ?? "");

function Thread({ messages, graph }: { messages: Message[]; graph: Graph }) {
  if (!messages.length) return null;
  return (
    <ol className="aa-thread" data-testid="aa-thread">
      {messages.map((m) => (
        <li key={m.id} data-kind={m.kind}>
          <Face who={m.sender} />
          <span className="aa-thread-who">{partyName(m.sender, graph)}</span>
          <span className="aa-chip-xs" data-chip={m.kind}>{m.kind}</span>
          <span className="aa-thread-text" data-user-text>{bodyText(m)}</span>
        </li>
      ))}
    </ol>
  );
}

function DecisionView({ d, projectId, orgId, onChanged }: { d: Decision; projectId: string; orgId?: string | null; onChanged: () => void }) {
  const [overriding, setOverriding] = useState(false);
  const [chosen, setChosen] = useState("");
  const [why, setWhy] = useState("");
  const consensus = d.consensus;
  const dissent = consensus?.dissent ?? [];
  const votes = consensus?.votes ?? [];
  const submit = async () => {
    const r = await overrideDecision(projectId, d.act_id, chosen.trim(), why.trim());
    showToast(r.ok ? "success" : "error", r.ok ? "Overridden — agents follow your choice" : `Not overridden: ${r.error}`);
    if (r.ok) { setOverriding(false); onChanged(); }
  };
  return (
    <div className="aa-decision" data-testid="aa-drawer-decision">
      <p className="aa-q" data-user-text>{d.question}</p>
      <div className="aa-options">
        <span className="aa-opt" data-chosen="true" title="Chosen">✓ {d.options.find((o) => o.key === d.chosen)?.label || d.chosen}</span>
        {d.rejected.map((r) => (
          <span key={r.option} className="aa-opt" title={r.why || "Not chosen"}>✕ {d.options.find((o) => o.key === r.option)?.label || r.option}</span>
        ))}
      </div>
      <dl className="aa-facts">
        <dt>Decided by</dt><dd><Face who={d.decided_by} size={18} /> {d.decider_kind}</dd>
        {d.autonomy_level ? <><dt>Allowed at</dt><dd>{d.autonomy_level} · {LEVEL_WORDS[d.autonomy_level] ?? ""}</dd></> : null}
        {d.confidence !== null ? <><dt>Confidence</dt><dd><span className="aa-meter" style={{ ["--v" as string]: d.confidence }} aria-hidden />{Math.round(d.confidence * 100)}%</dd></> : null}
        {d.reversibility ? <><dt>Undo</dt><dd>{d.reversibility}{d.undo ? ` · ${d.undo}` : ""}</dd></> : null}
        <dt>Cost</dt><dd>{money(microsUsd(d.cost_usd_micros))}</dd>
        {d.outcome ? <><dt>Outcome</dt><dd>{d.outcome}</dd></> : null}
      </dl>
      {d.rationale ? <><h4>Why</h4><p data-user-text data-testid="aa-rationale">{d.rationale}</p></> : null}
      {votes.length ? (
        <>
          <h4>Votes{consensus?.mode ? <span className="aa-sub"> · {consensus.mode.replace(/_/g, " ")}</span> : null}</h4>
          <ul className="aa-votes" data-testid="aa-votes">
            {votes.map((v, i) => (
              <li key={`${v.party}-${i}`} data-vote={v.vote}><Face who={String(v.party)} size={20} />{v.vote === "approve" ? "✓" : v.vote === "reject" ? "✕" : "–"}</li>
            ))}
          </ul>
        </>
      ) : null}
      {dissent.length ? (
        <div className="aa-dissent" data-testid="aa-dissent">
          <h4>⑂ Dissent</h4>
          {dissent.map((x, i) => <p key={i} data-user-text><Face who={x.party} size={18} /> {x.reason || x.position}</p>)}
        </div>
      ) : null}
      {d.evidence.length ? <><h4>Evidence</h4><EvidenceList items={d.evidence} orgId={orgId} /></> : null}
      {d.decider_kind !== "person" && !d.overrides_act_id ? (
        overriding ? (
          <form className="aa-form" onSubmit={(e) => { e.preventDefault(); void submit(); }} data-testid="aa-override-form">
            <input aria-label="Your choice" placeholder="Your choice" value={chosen} onChange={(e) => setChosen(e.target.value)} required maxLength={120} />
            <input aria-label="Why" placeholder="Why (one line)" value={why} onChange={(e) => setWhy(e.target.value)} required maxLength={800} />
            <GatedButton permission="decision:approve" projectId={projectId} type="submit" className="ag-btn primary"
                         disabled={!chosen.trim() || !why.trim()}>Override</GatedButton>
            <button type="button" className="ag-btn" onClick={() => setOverriding(false)}>Cancel</button>
          </form>
        ) : (
          <GatedButton permission="decision:approve" projectId={projectId} className="ag-btn" data-testid="aa-override"
                       title="Choose differently; the agents follow you" onClick={() => setOverriding(true)}>Override…</GatedButton>
        )
      ) : null}
    </div>
  );
}

export function NodeDrawer({ target, graph, messages, projectId, orgId, onClose, onChanged }: {
  target: Target; graph: Graph; messages: Message[]; projectId: string; orgId?: string | null;
  onClose: () => void; onChanged: () => void;
}) {
  const ref = target.node?.ref ?? target.edge?.ref;
  const [decision, setDecision] = useState<Decision | null>(null);
  const [signal, setSignal] = useState<Signal | null>(null);
  const refKey = `${ref?.kind}:${ref?.id}`;
  useEffect(() => {
    setDecision(null); setSignal(null);
    if (!ref?.id) return undefined;
    let live = true;
    if (ref.kind === "act") void aaRead(decisionPath(projectId, ref.id), parseDecision).then((r) => { if (live && r.ok && r.data) setDecision(r.data); });
    if (ref.kind === "signal") void aaRead(signalsPath(projectId), parseSignals).then((r) => { if (live && r.ok && r.data) setSignal(r.data.find((x) => x.id === ref.id) ?? null); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refKey, projectId]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", esc);
    return () => document.removeEventListener("keydown", esc);
  }, [onClose]);

  const node = target.node;
  const title = node ? node.label : target.edge ? `${target.edge.dissent ? "Dissent: " : ""}${target.edge.label}` : "";
  const kind = node ? TYPE_WORDS[node.type] ?? node.type : target.edge?.dissent ? "Dissenting vote" : "Message";
  const message = ref?.kind === "message" ? messages.find((m) => m.id === ref.id) : undefined;
  const party = node ? partyOfNode(node) : null;
  const replies = message && message.kind === "proposal" ? messages.filter((m) => m.reply_to === message.id) : [];

  let body: React.ReactNode = null;
  if (ref?.kind === "act") body = decision ? <DecisionView d={decision} projectId={projectId} orgId={orgId} onChanged={onChanged} /> : <p className="aa-sub">Loading…</p>;
  else if (ref?.kind === "signal") body = signal ? (
    <dl className="aa-facts" data-testid="aa-drawer-signal">
      <dt>Signal</dt><dd data-user-text>{signal.title}</dd><dt>Source</dt><dd>{signal.source} · {signal.kind}</dd>
      <dt>Severity</dt><dd>{signal.severity}</dd><dt>Seen</dt><dd>{signal.occurrences}×</dd>
    </dl>
  ) : <p className="aa-sub">{node?.badges.join(" · ")}</p>;
  else if (message) body = (
    <div data-testid="aa-drawer-message">
      <p className="aa-who"><Face who={message.sender} /> {partyName(message.sender, graph)} · <span className="aa-chip-xs">{message.kind}</span></p>
      <p data-user-text>{bodyText(message)}</p>
      {Array.isArray(message.body.evidence) ? <EvidenceList items={message.body.evidence as Evidence[]} orgId={orgId} /> : null}
      {replies.length ? <><h4>Replies</h4><Thread messages={replies} graph={graph} /></> : null}
    </div>
  );
  else if (party) body = (
    <div data-testid="aa-drawer-party">
      <h4>What {node!.label} said</h4>
      <Thread messages={messages.filter((m) => m.sender === party)} graph={graph} />
      {!messages.some((m) => m.sender === party) ? <p className="aa-sub">No messages on the board.</p> : null}
    </div>
  );
  else if (ref?.kind === "decision") body = (
    <div data-testid="aa-drawer-needs-you">
      <p>The agents want one answer from a person.</p>
      {ref.id ? <a className="ag-btn primary" href={decisionHref(ref.id, orgId)}>Answer in Approvals</a> : null}
    </div>
  );
  else if (ref && ["workflow_run", "code_task", "pull_request", "meeting", "incident"].includes(ref.kind)) {
    const href = ref.kind === "workflow_run" ? runHref(String(ref.id), orgId)
      : ref.kind === "pull_request" ? evidenceHref({ kind: "pull_request", ref: String(ref.id) }, orgId)
      : ref.kind === "code_task" ? `/?area=code_tasks&task=${encodeURIComponent(String(ref.id))}`
      : ref.kind === "meeting" ? `/?area=meetings&meeting=${encodeURIComponent(String(ref.id))}`
      : `/?area=observability&view=alerts&incident=${encodeURIComponent(String(ref.id))}`;
    body = <p data-testid="aa-drawer-run">{href ? <a className="ag-btn" href={href}>Open {node?.label ?? ref.kind}</a> : <code>{ref.id}</code>}</p>;
  } else if (ref?.kind === "work_item") body = (
    <dl className="aa-facts" data-testid="aa-drawer-outcome">
      <dt>Ended</dt><dd>{graph.item.lifecycle}</dd>
      {graph.item.resolved_by ? <><dt>By</dt><dd><Face who={graph.item.resolved_by} size={18} /> {partyName(graph.item.resolved_by, graph)}</dd></> : null}
      <dt>Cost</dt><dd>{money(graph.badges.cost_usd)}</dd>
      {graph.links.length ? <><dt>Linked</dt><dd>{graph.links.map((l) => `${l.kind.replace(/_/g, " ")} ${l.ref}`).join(" · ")}</dd></> : null}
    </dl>
  );

  return (
    <aside className="aa-drawer" role="dialog" aria-label={`${kind}: ${title}`} data-testid="aa-drawer">
      <header className="aa-drawer-h">
        {node ? <NodeFace node={node} size={32} /> : null}
        <div>
          <span className="aa-eyebrow">{kind}</span>
          <h3 data-user-text>{title}</h3>
        </div>
        <button type="button" className="aa-x" aria-label="Close" data-testid="aa-drawer-close" onClick={onClose}>✕</button>
      </header>
      <div className="aa-drawer-b">{body}</div>
    </aside>
  );
}
