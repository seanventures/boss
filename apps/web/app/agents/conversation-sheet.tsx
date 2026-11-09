"use client";

/**
 * AN AGENT CONVERSATION, READ BACK (KG-V2): the brief a person sent, the agent's answer, and what
 * it did, for a demo, and for an audit. Opened from Agent Studio › Activity › Talks, from a
 * conversation's node in the Knowledge Graph, or by link (`?area=workforce&view=activity&
 * conversation=<thread>`).
 *
 * Read-only. Every word is the record's (lib/agent-conversation.ts): no model writes any of it, and
 * no id is ever text a person reads, a graph item is its title, a chip that opens it in the graph.
 */
import React, { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "@phosphor-icons/react";

import { loadConversationMessagesAction } from "../actions";
import { callAction } from "../../lib/call-action";
import { Markdown, type InlineText } from "../markdown";
import { useIdentity } from "../../components/identity/identity-provider";
import { identityOf, openAgent } from "../../lib/agent-world";
import {
  askerName, conversationHref, didLines, inThisProject, foldText, footerWords, isPress, kindWords, parseKnowledge, parseMessages,
  splitIds, turnsOf, type ConversationKnowledge, type GraphItem, type Message,
} from "../../lib/agent-conversation";
import { getConversationKnowledge, kgNodeHref } from "../../lib/kg-view";
import { clock, dayLabel, utcTitle } from "../../lib/log-time";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { RunFace } from "./run-face";

type Loaded = { messages: Message[] | null; error: string | null; knowledge: ConversationKnowledge | null };

function orgOfUrl(): string | null {
  try { return new URL(window.location.href).searchParams.get("org"); } catch { return null; }
}

/** The sheet: reads the conversation and its knowledge, then shows ConversationView. */
export function ConversationSheet({ projectId, actor, threadId, onClose }: {
  projectId: string; actor: string; threadId: string; onClose: () => void;
}) {
  const [loaded, setLoaded] = useState<Loaded>({ messages: null, error: null, knowledge: null });
  useEffect(() => {
    let live = true;
    setLoaded({ messages: null, error: null, knowledge: null });
    void callAction(loadConversationMessagesAction, undefined, "assistant:chat")(threadId, projectId, actor).then((r) => {
      if (!live) return;
      setLoaded((l) => r.ok ? { ...l, messages: parseMessages(r.data) }
        : { ...l, messages: [], error: /not found|404/i.test(String(r.error ?? ""))
            ? "This conversation is private to the person who started it, or it no longer exists."
            : "BOSS could not read this conversation. Retry in a moment." });
    });
    void getConversationKnowledge({ projectId, actor }, threadId).then((r) => {
      if (live && r.ok) setLoaded((l) => ({ ...l, knowledge: parseKnowledge(r.data) }));
    });
    return () => { live = false; };
  }, [projectId, actor, threadId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const me = useIdentity().me?.display_name ?? null;
  const sheet = (
    <>
      <div className="cv-scrim" onClick={onClose} aria-hidden />
      <ConversationView messages={loaded.messages} error={loaded.error} knowledge={loaded.knowledge}
                        signedIn={me} orgId={orgOfUrl()} onClose={onClose} />
    </>
  );
  // Over the whole page, never inside the area's box (an ancestor's containment would clip it).
  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}

/** What the sheet shows, from what was read (rendered alone in tests). */
export function ConversationView({ messages, error, knowledge, signedIn, orgId, onClose }: {
  messages: Message[] | null; error?: string | null; knowledge: ConversationKnowledge | null;
  signedIn?: string | null; orgId?: string | null; onClose?: () => void;
}) {
  useIdentityVersion(); // the agent's name and face are resolved at render
  const turns = useMemo(() => turnsOf(messages ?? []), [messages]);
  const roleKey = knowledge?.agent.role || messages?.find((m) => m.agentRole)?.agentRole || "";
  const id = roleKey ? identityOf(roleKey, knowledge?.agent.name) : null;
  const agentName = id ? (id.callsign || id.name) : "The agent";
  const agentRole = id && id.callsign ? id.name : knowledge?.agent.name ?? "";
  const asker = askerName(knowledge?.askedBy, signedIn);
  const firstAsk = turns.find((t) => t.ask)?.ask ?? null;
  const title = knowledge?.title || (firstAsk ? (firstAsk.content.split("\n").find((l) => l.trim()) ?? "").replace(/^[#>\s*_`]+/, "").trim() : "") || "Conversation";
  const started = knowledge?.startedAt || firstAsk?.createdAt || messages?.[0]?.createdAt || "";
  const chips = useChips(knowledge, orgId);

  return (
    <aside className="cv-sheet" role="dialog" aria-modal="false" aria-label={title} data-testid="cv-sheet">
      <header className="cv-head">
        <div className="cv-head-main">
          <span className="cv-area-name">Conversation</span>
          <h2 className="cv-title" data-user-text data-testid="cv-title">{chips(title, "title")}</h2>
          <div className="cv-meta">
            {roleKey ? (
              <span className="cv-agent" data-testid="cv-agent">
                <RunFace roleKey={roleKey} size={24} />
                <strong data-user-text>{agentName}</strong>
                {agentRole && agentRole !== agentName ? <span className="cv-muted" data-user-text>{agentRole}</span> : null}
                <button type="button" className="cv-chip" data-testid="cv-passport"
                        title={`${agentName}'s Passport: who it is, what it may do, and its record`}
                        onClick={() => openAgent(roleKey)}>Passport</button>
              </span>
            ) : null}
            <span className="cv-asker" data-testid="cv-asker">Asked by <strong data-user-text>{asker}</strong></span>
            {started ? (
              <time className="cv-muted" dateTime={started} title={utcTitle(started)} data-testid="cv-started">
                {dayLabel(started)}, {clock(started, { seconds: false })}
              </time>
            ) : null}
          </div>
        </div>
        {onClose ? (
          <button type="button" className="ag-icon-btn" aria-label="Close" data-testid="cv-close" onClick={onClose}>
            <X size={14} weight="bold" aria-hidden />
          </button>
        ) : null}
      </header>
      <div className="cv-body" data-testid="cv-body">
        {messages === null ? <p className="cv-muted" aria-busy="true">Opening…</p> : null}
        {error ? <p className="cv-error" role="alert" data-testid="cv-error">{error}</p> : null}
        {turns.map((t, i) => (
          <section key={t.ask?.id ?? `t${i}`} className="cv-turn" data-testid="cv-turn">
            {t.ask ? <Brief message={t.ask} asker={asker} inline={chips} /> : null}
            {t.answers.map((m) => isPress(m) ? (
              <div key={m.id} className="cv-press" data-testid="cv-press">
                <Markdown text={m.content.replace(/^(✓|\u26a0) The operator/, `$1 ${asker === "the operator" ? "The operator" : asker}`)} inline={chips} />
              </div>
            ) : (
              <Answer key={m.id} message={m} name={agentName} roleKey={m.agentRole || roleKey}
                      produced={knowledge?.produced ?? []} orgId={orgId} inline={chips} />
            ))}
          </section>
        ))}
      </div>
      {knowledge ? <Footer knowledge={knowledge} orgId={orgId} /> : null}
    </aside>
  );
}

function Brief({ message, asker, inline }: { message: Message; asker: string; inline: InlineText }) {
  const [all, setAll] = useState(false);
  const fold = foldText(message.content);
  return (
    <div className="cv-ask" data-testid="cv-ask">
      <div className="cv-who"><strong data-user-text>{asker}</strong>
        <time className="cv-muted" dateTime={message.createdAt} title={utcTitle(message.createdAt)}>{clock(message.createdAt, { seconds: false })}</time>
      </div>
      <div className="cv-bubble" data-user-text>
        <Markdown text={all ? message.content : fold.shown} inline={inline} />
      </div>
      {fold.folded ? (
        <button type="button" className="inf-link cv-more" data-testid="cv-show-all" aria-expanded={all}
                onClick={() => setAll((v) => !v)}>{all ? "Show less" : "Show all"}</button>
      ) : null}
    </div>
  );
}

function Answer({ message, name, roleKey, produced, orgId, inline }: {
  message: Message; name: string; roleKey: string; produced: GraphItem[]; orgId?: string | null; inline: InlineText;
}) {
  const lines = didLines(message.toolCalls, produced);
  return (
    <div className="cv-answer" data-testid="cv-answer">
      <div className="cv-who">
        <RunFace roleKey={roleKey || null} size={20} />
        <strong data-user-text>{name}</strong>
        <time className="cv-muted" dateTime={message.createdAt} title={utcTitle(message.createdAt)}>{clock(message.createdAt, { seconds: false })}</time>
      </div>
      <div className="cv-text" data-user-text><Markdown text={message.content} inline={inline} /></div>
      {lines.length ? (
        <details className="cv-did" open data-testid="cv-did">
          <summary>What it did <span className="cv-muted">· {message.toolCalls.length} step{message.toolCalls.length === 1 ? "" : "s"}</span></summary>
          <ol className="cv-did-list">
            {lines.map((l, i) => (
              <li key={i} data-ok={l.ok ? "true" : "false"} title={l.tip} data-testid="cv-did-line">
                {l.opens ? (
                  <a className="inf-link" href={inThisProject(kgNodeHref(l.opens.id, orgId))} data-testid="cv-did-learning"
                     title={`Open “${l.opens.title}” in the Knowledge Graph`}>{l.text}</a>
                ) : <span>{l.text}</span>}
                {l.count ? <span className="cv-muted"> ×{l.count}</span> : null}
                {!l.ok ? <span className="cv-failed">, failed</span> : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </div>
  );
}

function Footer({ knowledge, orgId }: { knowledge: ConversationKnowledge; orgId?: string | null }) {
  return (
    <footer className="cv-foot" data-testid="cv-foot">
      <p className="cv-foot-line" data-testid="cv-foot-line">{footerWords(knowledge)}</p>
      {knowledge.produced.length ? (
        <div className="cv-foot-row"><span className="cv-muted">Wrote</span>
          {knowledge.produced.map((p) => <NodeChip key={p.id} id={p.id} title={p.title} kind={p.kind} orgId={orgId} />)}
        </div>
      ) : null}
      {knowledge.used.length ? (
        <div className="cv-foot-row"><span className="cv-muted">Used</span>
          {knowledge.used.map((u) => <NodeChip key={u.id} id={u.id} title={u.title} kind={u.kind} orgId={orgId}
                                               how={u.hows.join(", ")} />)}
        </div>
      ) : null}
    </footer>
  );
}

function NodeChip({ id, title, kind, orgId, how }: { id: string; title: string; kind?: string; orgId?: string | null; how?: string }) {
  const word = kind ? kindWords(kind) : "";
  return (
    <a className="cv-chip cv-node" href={inThisProject(kgNodeHref(id, orgId))} data-testid="cv-node-chip" data-kind={kind || undefined}
       title={`${word ? `${word[0].toUpperCase()}${word.slice(1)}` : "Item"}${how ? `, ${how}` : ""}, open in the Knowledge Graph`}>
      <span data-user-text>{title}</span>
    </a>
  );
}

/** Ids in the words become chips: a graph item by its title, a conversation by a link to it. */
function useChips(knowledge: ConversationKnowledge | null, orgId?: string | null): InlineText {
  return useMemo(() => (text: string, key: string, code?: boolean): React.ReactNode => {
    const parts = splitIds(text);
    if (parts.length === 1 && parts[0].kind === "text") return code ? <code>{text}</code> : text;
    return <>{parts.map((part, i) => {
      const k = `${key}-${i}`;
      if (part.kind === "text") return code ? <code key={k}>{part.text}</code> : <React.Fragment key={k}>{part.text}</React.Fragment>;
      if (part.kind === "thread") {
        return <a key={k} className="cv-chip" href={conversationHref(part.id, orgId)} data-testid="cv-thread-chip">a conversation</a>;
      }
      const named = knowledge?.names[part.id];
      return <NodeChip key={k} id={part.id} title={named?.title || "a graph item"} kind={named?.kind} orgId={orgId} />;
    })}</>;
  }, [knowledge, orgId]);
}
