/**
 * ONE AGENT CONVERSATION, AS A PERSON READS IT (KG-V2): the brief a person sent, the agent's
 * answer, and what it did — for a demo and for an audit.
 *
 * Every word here is DATA: the messages (GET /v1/agent/threads/{id}/messages), their stored tool
 * calls, and what the conversation wrote to and took from the graph
 * (GET /v1/knowledge/view/conversation). No model writes any of it. An id is never text a person
 * reads: a graph item is shown by its title (a chip that opens it in the graph), a conversation
 * by its question, the console's own actor as the signed-in person.
 */

// ── links ───────────────────────────────────────────────────────────────────────────────────

/** The project the page is on (`?project_id=`, another project of the org), or null. */
function hereProject(): string | null {
  try { return new URL(window.location.href).searchParams.get("project_id"); } catch { return null; }
}

/** A console link that stays in the project the page is on: `?project_id=` rides along when the
 *  page has one and the link does not (a graph node or a conversation of THIS project). */
export function inThisProject(href: string, projectId: string | null = hereProject()): string {
  if (!projectId || !href.startsWith("/?")) return href;
  const p = new URLSearchParams(href.slice(2));
  if (!p.has("project_id")) p.set("project_id", projectId);
  return `/?${p.toString()}`;
}

/** The conversation in Agent Studio › Activity, over whatever else is open there. */
export function conversationHref(threadId: string, orgId?: string | null, projectId?: string | null): string {
  const p = new URLSearchParams();
  if (orgId) p.set("org", orgId);
  p.set("area", "workforce");
  p.set("view", "activity");
  p.set("conversation", threadId);
  return inThisProject(`/?${p.toString()}`, projectId === undefined ? hereProject() : projectId);
}

export function isThreadId(v: unknown): v is string {
  return typeof v === "string" && /^athr_[A-Za-z0-9]+$/.test(v);
}

// ── who asked ───────────────────────────────────────────────────────────────────────────────

/** The console's and Metatron's own actors: the signed-in operator, never named by this id. */
export const CONSOLE_ACTORS = new Set(["executive_console", "metatron", "coo"]);

export function isConsoleActor(id: string | null | undefined): boolean {
  return !!id && CONSOLE_ACTORS.has(id);
}

export type AskedBy = { kind: "console" | "person" | "service" | "unknown"; name: string | null };

/** The person who asked, in words: a console or Metatron question is the signed-in person's
 *  (by display name), else "the operator"; never the raw actor. */
export function askerName(asked: AskedBy | null | undefined, signedIn?: string | null): string {
  const me = (signedIn ?? "").trim();
  if (!asked || asked.kind === "console") return me || "the operator";
  return (asked.name ?? "").trim() || "a person";
}

// ── the wire ────────────────────────────────────────────────────────────────────────────────

export type ToolCall = { tool: string; ok: boolean; args: Record<string, unknown>; refs: string[]; error: string | null };
export type Message = {
  id: string; role: "user" | "assistant" | string; content: string; agentRole: string | null;
  createdAt: string; toolCalls: ToolCall[];
};
export type GraphItem = { id: string; kind: string; title: string };
export type UsedItem = GraphItem & { hows: string[]; learnedIn: string | null };
export type ConversationKnowledge = {
  threadId: string; title: string; startedAt: string | null;
  agent: { role: string; name: string; agentId: string | null };
  askedBy: AskedBy;
  produced: GraphItem[];
  used: UsedItem[];
  names: Record<string, { title: string; kind: string }>;
  learned: number; usedEarlier: number;
};

type Wire = Record<string, unknown>;
const obj = (v: unknown): Wire => (v && typeof v === "object" && !Array.isArray(v) ? v as Wire : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

function jsonish(v: unknown): unknown {
  if (typeof v !== "string") return v;
  try { return JSON.parse(v); } catch { return v; }
}

export function parseMessages(w: unknown): Message[] {
  return arr(obj(w).messages).map((m) => {
    const x = obj(m);
    return {
      id: str(x.id), role: str(x.role), content: str(x.content), agentRole: optStr(x.agent_role),
      createdAt: str(x.created_at),
      toolCalls: arr(jsonish(x.tool_calls)).map((c) => {
        const t = obj(c);
        return { tool: str(t.tool), ok: t.ok !== false, args: obj(jsonish(t.args)),
                 refs: arr(t.refs).map((r) => str(r)).filter(Boolean), error: optStr(t.error) };
      }),
    };
  });
}

export function parseKnowledge(w: unknown): ConversationKnowledge {
  const o = obj(w);
  const agent = obj(o.agent);
  const asked = obj(o.asked_by);
  const kind = str(asked.kind);
  const item = (v: unknown): GraphItem => { const x = obj(v); return { id: str(x.id), kind: str(x.kind), title: str(x.title) }; };
  const names: ConversationKnowledge["names"] = {};
  for (const [id, v] of Object.entries(obj(o.names))) names[id] = { title: str(obj(v).title), kind: str(obj(v).kind) };
  return {
    threadId: str(o.thread_id), title: str(o.title), startedAt: optStr(o.started_at),
    agent: { role: str(agent.role), name: str(agent.name), agentId: optStr(agent.agent_id) },
    askedBy: { kind: (["console", "person", "service"].includes(kind) ? kind : "unknown") as AskedBy["kind"], name: optStr(asked.name) },
    produced: arr(o.produced).map(item),
    used: arr(o.used).map((u) => ({ ...item(u), hows: arr(obj(u).hows).map((h) => str(h)), learnedIn: optStr(obj(u).learned_in) })),
    names,
    learned: Number(o.learned) || 0,
    usedEarlier: Number(o.used_earlier) || 0,
  };
}

// ── turns ───────────────────────────────────────────────────────────────────────────────────

/** A press outcome (agent_service.record_action_outcome): the operator's click, not an answer. */
export function isPress(m: Pick<Message, "role" | "agentRole" | "content">): boolean {
  return m.role === "assistant" && !m.agentRole && /^(✓|⚠) The operator pressed/.test(m.content);
}

export type Turn = { ask: Message | null; answers: Message[] };

/** Each person's message, then everything that answered it, in order. */
export function turnsOf(messages: Message[]): Turn[] {
  const out: Turn[] = [];
  for (const m of messages) {
    if (m.role === "user") { out.push({ ask: m, answers: [] }); continue; }
    if (m.role !== "assistant") continue;
    if (!out.length) out.push({ ask: null, answers: [] });
    out[out.length - 1].answers.push(m);
  }
  return out;
}

/** A long brief folds after this many lines, with "Show all". */
export const BRIEF_LINES = 12;

export function foldText(text: string, lines = BRIEF_LINES): { shown: string; folded: boolean } {
  const all = text.replace(/\r\n/g, "\n").split("\n");
  if (all.length <= lines) return { shown: text, folded: false };
  // Never cut a table in two (its header alone renders as raw pipes): fold before it, or — when
  // the table starts the brief — after it.
  let end = lines;
  if (all[end - 1]?.includes("|") && all[end]?.includes("|")) {
    let start = end - 1;
    while (start > 0 && all[start - 1].includes("|")) start -= 1;
    if (start > 2) end = start;
    else { while (end < all.length && all[end].includes("|")) end += 1; }
  }
  if (end >= all.length) return { shown: text, folded: false };
  let cut = all.slice(0, end);
  // Never cut inside a code fence: an odd number of fences closes the one left open.
  if (cut.filter((l) => l.trim().startsWith("```")).length % 2) cut = [...cut, "```"];
  return { shown: cut.join("\n"), folded: true };
}

// ── what it did, in plain words ─────────────────────────────────────────────────────────────

const KIND_WORDS: Record<string, string> = {
  root_cause: "root cause", investigation: "investigation", precedent: "precedent", fix: "fix",
  workaround: "workaround", test_failure: "test failure", review_finding: "review finding",
  decision: "decision", fact: "fact",
};
export function kindWords(kind: string): string {
  return KIND_WORDS[kind] ?? kind.replace(/_/g, " ");
}

/** A long CamelCase file name, kept recognisable: "FineractStyle…Processor.java". */
export function shortFile(path: string, max = 32): string {
  const base = path.split("/").filter(Boolean).pop() ?? path;
  if (base.length <= max) return base;
  const dot = base.lastIndexOf(".");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  const ext = dot > 0 ? base.slice(dot) : "";
  const words = stem.match(/[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])/g) ?? [];
  if (words.length >= 3) return `${words.slice(0, 2).join("")}…${words[words.length - 1]}${ext}`;
  return `${stem.slice(0, 14)}…${stem.slice(-8)}${ext}`;
}

function quote(v: unknown, max = 48): string {
  const s = str(v).replace(/\s+/g, " ").trim();
  return `“${s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s}”`;
}

export type DidLine = {
  text: string;
  /** The full detail on hover (a path, a pattern): never an id. */
  tip?: string;
  ok: boolean;
  /** The graph item this call wrote, to open it (a recorded learning). */
  opens?: GraphItem;
  count?: number;
};

function found(n: number): string {
  return ` → ${n} found`;
}

/** One stored tool call in plain words. `produced` matches a recorded learning to the item it
 *  wrote (by title), so the line opens it in the graph. */
export function toolWords(call: ToolCall, produced: GraphItem[] = []): DidLine {
  const a = call.args;
  const refs = call.refs.length;
  const n = refs ? found(refs) : "";
  const ok = call.ok;
  switch (call.tool) {
    case "kg_precedent": return { text: `Searched past incidents (kg_precedent)${n}`, tip: str(a.query) || undefined, ok };
    case "kg_search": return { text: `Searched the knowledge graph for ${quote(a.query)}${n}`, ok };
    case "kg_rules": return { text: `Read the business rules (kg_rules)${n}`, ok };
    case "kg_why": return { text: `Asked the graph why (kg_why)${n}`, ok };
    case "record_learning": {
      const kind = str(a.learning_kind ?? a.kind);
      const title = str(a.title).trim();
      const opens = produced.find((p) => title && (p.title === title || p.title.startsWith(title) || title.startsWith(p.title)));
      return { text: `Recorded learning: ${kind ? kindWords(kind) : "a learning"}`, tip: title || undefined, ok, opens };
    }
    case "kg_propose_rule": return { text: "Proposed a business rule", tip: str(a.title ?? a.statement) || undefined, ok };
    case "read_code": return { text: `Read ${shortFile(str(a.file ?? a.path))}`, tip: str(a.file ?? a.path) || undefined, ok };
    case "search_code": return { text: `Searched the code for ${quote(a.pattern ?? a.query)}`, tip: str(a.pattern ?? a.query) || undefined, ok };
    case "git_history": {
      const file = str(a.file);
      if (file) return { text: `Read the history of ${shortFile(file)}`, tip: file, ok };
      if (a.search) return { text: `Searched commits for ${quote(a.search)}`, tip: str(a.search), ok };
      return { text: "Read the recent commits", ok };
    }
    case "list_repositories": return { text: "Listed the repositories", ok };
    case "read_pull_request": return { text: `Read pull request ${str(a.pull_request ?? a.number)}`.trim(), ok };
    case "read_logs": return { text: "Read the logs", ok };
    default: {
      const words = call.tool.replace(/_/g, " ").trim();
      return { text: words ? `Used ${words}` : "Used a tool", ok };
    }
  }
}

/** The calls in plain words, a repeat back to back folded into one line (`×2`). */
export function didLines(calls: ToolCall[], produced: GraphItem[] = []): DidLine[] {
  const out: DidLine[] = [];
  for (const c of calls) {
    const line = toolWords(c, produced);
    const last = out[out.length - 1];
    if (last && !line.opens && !last.opens && last.text === line.text && last.ok === line.ok) {
      last.count = (last.count ?? 1) + 1;
      continue;
    }
    out.push(line);
  }
  return out;
}

// ── ids in the words ────────────────────────────────────────────────────────────────────────

/** A citation (`[L:kgn_…]`, `[R:kgn_…]`), a quoted id, or a bare graph or conversation id. */
export const ID_IN_TEXT = /\[(?:L|R):(kgn_[A-Za-z0-9]+)\]|(kgn_[A-Za-z0-9]+)|(athr_[A-Za-z0-9]+)/g;

export type TextPart = { kind: "text"; text: string } | { kind: "node"; id: string } | { kind: "thread"; id: string };

/** Text cut at every id it names, for chips in place of the ids. */
export function splitIds(text: string): TextPart[] {
  const out: TextPart[] = [];
  let last = 0;
  for (const m of text.matchAll(ID_IN_TEXT)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ kind: "text", text: text.slice(last, at) });
    if (m[3]) out.push({ kind: "thread", id: m[3] });
    else out.push({ kind: "node", id: (m[1] ?? m[2]) as string });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", text: text.slice(last) });
  return out;
}

/** "Wrote to the knowledge graph: 3 learnings · used 0 earlier learnings". */
export function footerWords(k: Pick<ConversationKnowledge, "learned" | "usedEarlier">): string {
  const n = (c: number, w: string) => `${c} ${w}${c === 1 ? "" : "s"}`;
  return `Wrote to the knowledge graph: ${n(k.learned, "learning")} · used ${n(k.usedEarlier, "earlier learning")}`;
}
