"use client";

/**
 * ASK THE CAPTAIN (screen 09, contract §7): the project's captain in a 480 px drawer —
 * its face, "Captain", the scope ("HelpDesk AI Demo only · knows 412 facts"), past
 * conversations, and the conversation itself.
 *
 * The conversation IS the Ask BOSS dock's (AgentChat, one agent path): same project, same
 * subject `{kind: project, id}`, same role, so the same thread shows in the dock. Suggestion
 * chips come from the project's record (GET …/captain `starters`) and a tap sends; a door that
 * asked a question (`&cap_ask=`, the Overview's "Plan next week") sends it once on open.
 */
import React, { useEffect, useState } from "react";
import { ClockCounterClockwise, Coins, Folders, MapTrifold, Question, Sparkle } from "@phosphor-icons/react";

import { AgentChat } from "../agent-ui";
import { subjectRef } from "../../lib/dock-subject";
import { PX_PATHS, parseCaptainDetail, pxHref, usePxRead, writePxUrl, type CaptainDetail } from "../../lib/project-experience";
import { whenLocal } from "../../lib/local-time";
import { Drawer, Face, Tag } from "./kit";
import type { PxCtx } from "./project-page";

const STARTER_ICON: Record<string, React.ReactNode> = {
  map: <MapTrifold size={12} aria-hidden />, question: <Question size={12} aria-hidden />, coins: <Coins size={12} aria-hidden />,
};

/** The question a door asked (`&cap_ask=`), taken once: left in the URL it would be sent
 *  again on every reopen. */
function useAsked(): string {
  const [asked] = useState(() => {
    try { return new URLSearchParams(window.location.search).get("cap_ask") ?? ""; } catch { return ""; }
  });
  useEffect(() => { if (asked) writePxUrl({ cap_ask: null }, "replace"); }, [asked]);
  return asked;
}

/** A draft a door carried here (`&cap_draft=`, New work's "Open in Ask Helm"): it waits in the
 *  box for the person to send — never sent for them — and is taken once. */
function useDraft(): string {
  const [draft] = useState(() => {
    try { return new URLSearchParams(window.location.search).get("cap_draft") ?? ""; } catch { return ""; }
  });
  useEffect(() => { if (draft) writePxUrl({ cap_draft: null }, "replace"); }, [draft]);
  return draft;
}

export function CaptainDrawer({ ctx, onClose }: { ctx: PxCtx; onClose: () => void }) {
  const read = usePxRead<CaptainDetail | null>(PX_PATHS.captain(ctx.projectId), parseCaptainDetail, { everyMs: 60_000 });
  const asked = useAsked();
  const draft = useDraft();
  const [history, setHistory] = useState(false);
  const base = ctx.overview.captain;
  const c = read.data;
  const name = c?.name || base?.name || "BOSS";
  const roleKey = c?.role_key || base?.role_key || "";
  const scope = c?.scope || `${ctx.overview.project.name} only`;
  return (
    <Drawer testid="px-drawer-captain" onClose={onClose} bodyClass="px-capd-b"
            icon={base || c ? <Face agent={{ name, role_key: roleKey }} size={34} state={c?.status.startsWith("Watching") ? "working" : "idle"} standalone /> : <Sparkle size={20} aria-hidden />}
            title={<span className="px-row" style={{ gap: 8 }}>{name}<Tag tone="blue">Captain</Tag></span>}
            sub={<span className="px-row px-capd-scope" data-testid="px-capd-scope">
              <Folders size={12} aria-hidden /> {scope}
              {c ? <><span aria-hidden>·</span><span className="px-dot" data-s="done" data-round aria-hidden /> knows {c.facts.toLocaleString()} facts</> : null}
            </span>}
            actions={<button type="button" className="px-icon-btn" aria-pressed={history} title="Past conversations" aria-label="Past conversations"
                             data-testid="px-capd-history" onClick={() => setHistory((v) => !v)}><ClockCounterClockwise size={15} /></button>}>
      {history ? (
        <div className="px-capd-hist" data-testid="px-capd-history-panel">
          <span className="px-lbl">Lately</span>
          {c?.recent.length ? c.recent.map((r) => (
            <a key={r.act_id} className="px-li" href={pxHref(r.href)} data-testid={`px-capd-recent-${r.act_id}`}>
              <Face agent={{ name: r.role_key, role_key: r.role_key }} size={20} state={r.state === "now" ? "working" : "idle"} />
              <span style={{ minWidth: 0 }}><b className="px-ell" style={{ display: "block" }}>{r.label}</b><small>{whenLocal(r.at)}</small></span>
              <Tag tone={r.state === "now" ? "blue" : r.state === "failed" ? "red" : "green"}>{r.state === "now" ? "running" : r.state === "failed" ? "failed" : "done"}</Tag>
            </a>
          )) : <p className="px-faint" style={{ margin: 0 }}>Nothing done here yet.</p>}
          <span className="px-lbl" style={{ marginTop: 6 }}>Past conversations</span>
        </div>
      ) : null}
      <div className="px-capd" data-history={history ? "1" : undefined} data-testid="px-capd-chat">
        <AgentChat key={`${ctx.projectId}|${roleKey}|${asked}|${draft}`} projectId={ctx.projectId} actor={ctx.actor} initialMessage={draft || undefined}
                   initialTaskId={subjectRef({ kind: "project", id: ctx.projectId })}
                   roleName={name === "BOSS" ? undefined : name} roleKey={roleKey || undefined}
                   placeholder={`Ask ${name}, or describe new work…`}
                   starters={(c?.starters ?? []).map((st) => ({ label: st.label, message: st.message, icon: STARTER_ICON[st.icon] ?? null }))}
                   sendOnOpen={asked || undefined} />
      </div>
    </Drawer>
  );
}
