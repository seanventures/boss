"use client";

/**
 * Work: what this agent has actually done, newest first (spec §3.4 › Work).
 *
 * Every row is one recorded act from `/v1/agents/{role}/acts` — a step it ran, a build stage, a
 * panel seat, a chat turn — with who handed it the work and how, in the API's own words
 * (`edge_label`, never composed here). A live act gets the `Now` card on top. `What I remember`
 * is the role's own memories, each with a way to make it forget. Nothing on this tab is
 * generated: an agent with no signed work says so and points at where to give it some.
 */
import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowSquareOut, ChatCircle, CheckSquare, Gavel, Hammer, ListChecks, Play, UsersThree,
} from "@phosphor-icons/react";

import type { Act, ActKind, AvatarState, Presence } from "../../lib/agent-world-types";
import {
  forgetMemory, identityOf, listActs, listRoleMemories, money, toast, type Memory,
} from "../../lib/agent-world";
import { consoleHref } from "../url-intent";
import { AgFace, AgState } from "./agent-face";
import { useIdentityVersion } from "../../lib/use-agent-identities";

type Row = Act & { from?: { kind: string; id?: string; roleKey?: string } };

export const WORK_FILTERS: { key: "" | ActKind; label: string }[] = [
  { key: "", label: "All" },
  { key: "step", label: "Steps" },
  { key: "stage", label: "Builds" },
  { key: "seat", label: "Panels" },
  { key: "chat", label: "Chats" },
];

const KIND_ICON: Record<ActKind, React.ComponentType<{ size?: number; "aria-hidden"?: boolean }>> = {
  run: Play, step: ListChecks, stage: Hammer, seat: UsersThree, chat: ChatCircle, approval: CheckSquare,
  handoff: Gavel, decision: Gavel,
};
const KIND_WORD: Record<ActKind, string> = {
  run: "Run", step: "Step", stage: "Build", seat: "Panel", chat: "Chat", approval: "Approval", handoff: "Handoff",
  decision: "Decision",
};

/** The act's status as a face state, so a row's dot and the face say the same thing. */
export function actState(status: Act["status"]): AvatarState {
  if (status === "complete") return "done";
  if (status === "awaiting_input") return "needs_you";
  if (status === "error") return "failed";
  if (status === "stale") return "stopped";
  if (status === "active" || status === "pending") return "working";
  return "idle";
}

/** The query string the console is on now; empty on a server render. */
const here = () => (typeof window === "undefined" ? "" : window.location.search);

/** Where the act's subject lives in the console. Built from the current URL, so `?org=` stays
 *  (url-intent.ts): a link without it lands on the organisation picker. */
export function actHref(a: Act, search: string = here()): string | null {
  const l = a.links;
  if (l.workflow_run_id) return consoleHref(search, { area: "delivery_workflows", wfr: l.workflow_run_id });
  if (l.code_task_run_id) return consoleHref(search, { area: "code_tasks", run: l.code_task_run_id });
  if (l.meeting_run_id) return consoleHref(search, { area: "meetings" });
  return null;
}

/** `3 Oct` in UTC: one heading per day; `Today` / `Yesterday` once the clock is known. */
export function dayOf(iso: string, now = 0): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Earlier";
  const key = (x: Date) => `${x.getUTCFullYear()}-${x.getUTCMonth()}-${x.getUTCDate()}`;
  if (now) {
    if (key(d) === key(new Date(now))) return "Today";
    if (key(d) === key(new Date(now - 86_400_000))) return "Yesterday";
  }
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${d.getUTCDate()} ${months[d.getUTCMonth()]}`;
}

export function duration(a: Act): string {
  const s = Date.parse(a.startedAt);
  const e = a.endedAt ? Date.parse(a.endedAt) : NaN;
  if (Number.isNaN(s) || Number.isNaN(e)) return "";
  const sec = Math.max(0, Math.round((e - s) / 1000));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function elapsed(since: string | undefined, now: number): string {
  const s = since ? Date.parse(since) : NaN;
  if (Number.isNaN(s) || !now) return "";
  const m = Math.max(0, Math.round((now - s) / 60_000));
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Who handed the work: a person, BOSS, or another agent's face. */
function From({ row, actor }: { row: Row; actor: string }) {
  useIdentityVersion(); // names and faces below are resolved at render: repaint when identities load
  const kind = row.from?.kind ?? row.handedBy.kind;
  const id = row.from?.roleKey ?? row.from?.id ?? row.handedBy.id;
  if (kind === "agent" && id) {
    return <span className="ag-from" title={identityOf(id).name}><AgFace roleKey={id} size={20} decorative /></span>;
  }
  if (kind === "person") {
    const name = row.from ? undefined : row.handedBy.name;
    return <span className="ag-from-word" title={id}>{id === actor || !id ? "You" : name ?? id}</span>;
  }
  return <span className="ag-from-word">BOSS</span>;
}

export function PassportWork({ projectId, actor, roleKey, name, presence }: {
  projectId: string; actor: string; roleKey: string; name: string; presence?: Presence;
}) {
  const [kind, setKind] = useState<"" | ActKind>("");
  const [days, setDays] = useState<7 | 30>(7);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [failed, setFailed] = useState("");
  const [memories, setMemories] = useState<Memory[]>([]);
  const [open, setOpen] = useState("");
  // The clock, read after mount and once a minute: never in render, so server and browser agree.
  const [now, setNow] = useState(0);
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);

  const load = useCallback(async (cursor?: string | null) => {
    const r = await listActs(projectId, roleKey, { kind, days, cursor });
    if (!r.ok) { setFailed(r.status === 404 ? "" : r.error); if (!cursor) setRows([]); return; }
    setFailed("");
    setRows((was) => (cursor ? [...(was ?? []), ...r.data.items] : r.data.items));
    setNext(r.data.next);
  }, [projectId, roleKey, kind, days]);

  useEffect(() => { setRows(null); void load(); }, [load]);
  useEffect(() => {
    let alive = true;
    void listRoleMemories(projectId, actor, roleKey).then((r) => { if (alive && r.ok) setMemories(r.data); });
    return () => { alive = false; };
  }, [projectId, actor, roleKey]);

  const live = (rows ?? []).find((a) => !a.endedAt && a.status !== "complete");
  const groups = useMemo(() => {
    const out: { day: string; items: Row[] }[] = [];
    for (const a of rows ?? []) {
      const day = dayOf(a.startedAt, now);
      const last = out[out.length - 1];
      if (last && last.day === day) last.items.push(a); else out.push({ day, items: [a] });
    }
    return out;
  }, [rows, now]);
  const showNow = presence && presence.state !== "idle" && presence.state !== "done";

  const forget = async (m: Memory) => {
    const r = await forgetMemory(projectId, actor, m.id);
    if (!r.ok) { toast("error", r.error); return; }
    setMemories((xs) => xs.filter((x) => x.id !== m.id));
  };

  return (
    <div className="ag-work" data-testid="ag-work">
      {showNow ? (
        <div className="ag-now" data-testid="ag-now">
          <AgFace roleKey={roleKey} size={32} state={presence!.state} decorative />
          <span className="ag-now-label">Now</span>
          <span className="ag-now-line" data-user-text>{presence!.line || live?.label || "Working"}</span>
          {presence!.since ? <span className="ag-muted">{elapsed(presence!.since, now)}</span> : null}
          {live && actHref(live) ? <a className="ag-link" href={actHref(live)!}>Open run ↗</a> : null}
        </div>
      ) : null}

      <div className="ag-filters">
        <span className="ag-seg" role="group" aria-label="Kind">
          {WORK_FILTERS.map((f) => (
            <button key={f.key || "all"} type="button" aria-pressed={kind === f.key}
                    data-testid={`ag-work-kind-${f.key || "all"}`} onClick={() => setKind(f.key)}>{f.label}</button>
          ))}
        </span>
        <span className="ag-seg" role="group" aria-label="Window">
          {([7, 30] as const).map((d) => (
            <button key={d} type="button" aria-pressed={days === d} onClick={() => setDays(d)}>{d}d</button>
          ))}
        </span>
      </div>

      {failed ? <div className="ag-bad" role="alert">{failed}</div> : null}
      {rows === null ? <div className="ag-muted">Reading…</div> : null}
      {rows && !rows.length && !failed ? (
        <div className="ag-empty" data-testid="ag-work-empty">
          <span>No signed work yet</span>
          <a className="ag-link" href={consoleHref(here(), { area: "delivery_workflows", tab: "library" })}
             title={`Give ${name} a workflow step`}>Name it on a step ↗</a>
        </div>
      ) : null}

      {groups.map((g) => (
        <section key={g.day} className="ag-day">
          <div className="ag-day-head">{g.day}</div>
          <ul className="ag-acts">
            {g.items.map((a) => {
              const Icon = KIND_ICON[a.kind] ?? ListChecks;
              const href = actHref(a);
              const st = actState(a.status);
              return (
                <li key={a.actId} className="ag-act-row" data-testid={`ag-act-${a.actId}`}
                    data-reconstructed={a.reconstructed || undefined}>
                  <button type="button" className="ag-act-main" aria-expanded={open === a.actId}
                          onClick={() => setOpen(open === a.actId ? "" : a.actId)}>
                    <span className="ag-act-kind" title={KIND_WORD[a.kind]}><Icon size={14} aria-hidden /></span>
                    <span className="ag-act-label" data-user-text>{a.label}</span>
                    <From row={a} actor={actor} />
                    <span className="ag-act-edge" data-testid="ag-act-edge"
                          title={a.reconstructed ? "Rebuilt from older records; the handoff order may be approximate." : undefined}>
                      {a.edgeLabel}
                    </span>
                    <AgState state={st} compact />
                    <span className="ag-muted">{duration(a)}</span>
                    {a.costUsd > 0 ? <span className="ag-muted">{money(a.costUsd)}</span> : null}
                  </button>
                  {open === a.actId ? (
                    <dl className="ag-act-more">
                      {a.why ? <div><dt>Why</dt><dd data-user-text>{a.why}</dd></div> : null}
                      {a.scope.skills.length || a.scope.mcp.length ? (
                        <div><dt>Scope</dt><dd className="ag-chips">
                          {[...a.scope.skills, ...a.scope.mcp].map((s) => <code key={s}>{s}</code>)}
                          {a.scopeOk === false ? (
                            <span className="ag-warn" title="Had skills the step that handed it work didn't.">Wider than its parent</span>
                          ) : null}
                        </dd></div>
                      ) : null}
                      <div><dt>Accountable</dt><dd>{a.accountable === actor ? "You" : a.accountable}</dd></div>
                      {a.costUsd > 0 ? (
                        <div><dt>Cost</dt><dd title={`${a.tokensIn + a.tokensOut} model units`}>{money(a.costUsd)}</dd></div>
                      ) : null}
                      {href ? <div><dt>Open</dt><dd><a className="ag-link" href={href}>Open run <ArrowSquareOut size={11} aria-hidden /></a></dd></div> : null}
                    </dl>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
      {next ? (
        <button type="button" className="ag-btn" onClick={() => void load(next)}>More</button>
      ) : null}

      {memories.length ? (
        <section className="ag-memory" data-testid="ag-memory">
          <div className="ag-day-head">What I remember</div>
          <ul className="ag-memory-list">
            {memories.map((m) => (
              <li key={m.id} className="ag-mem" title={m.body}>
                <span data-user-text>{m.body}</span>
                <GatedButton permission="memory:manage" type="button" className="ag-link" onClick={() => void forget(m)}
                        data-testid={`ag-forget-${m.id}`}>Forget</GatedButton>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
