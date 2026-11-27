/**
 * "WAKE THIS AGENT WHEN…" (E4): the sentence builder over the route filter language
 * (boss-eventing-lld.md §5.2; engine/boss_engine/services/intake_filters.py). Pure: a draft the
 * person edits in words ⇄ the route's stored `config.match` + `config.addressing`.
 *
 *   When [a message mentions @BOSS] in [#support] → [Support agent], acting for [the person]
 *     and [text] [doesn't start with] [“ignore”]
 *
 * A chat source (Slack) picks WHAT HAPPENED from four words (mention / DM / any message /
 * follow-up) and WHERE from channel ids; a record source (GitHub, a webhook) picks from its
 * catalog's event types. Anything the builder cannot say in words (an `any`, a nested `not` of a
 * group) is kept as it is and shown as a custom filter; it is never rewritten silently.
 */

export type Op = "contains" | "prefix" | "suffix" | "eq" | "matches" | "exists";
export type Cond = { field: string; op: Op; value: string; not: boolean };
export type ChatWhen = "mention" | "dm" | "message" | "followup";
export type Draft = {
  when: string;            // ChatWhen for chat sources; an event type (or "") for the others
  channels: string[];      // chat only: Slack channel ids
  followups: boolean;      // chat: a reply in a thread the agent owns continues it
  conds: Cond[];
  custom: unknown | null;  // a filter the builder cannot say in words, kept as is
};

export const CHAT_KINDS = new Set(["slack", "teams"]);

const SLACK = {
  mention: "com.slack.message.app_mention", dm: "com.slack.message.im",
  channel: "com.slack.message.channel", thread: "com.slack.message.thread_reply",
};
const TEAMS = {
  mention: "com.microsoft.teams.message.mention", dm: "com.microsoft.teams.message.personal",
  channel: "com.microsoft.teams.message.channel", thread: "com.microsoft.teams.message.thread_reply",
};

export const CHAT_WHEN: { key: ChatWhen; word: string; tip: string }[] = [
  { key: "mention", word: "a message mentions @BOSS", tip: "Someone @mentions the BOSS app" },
  { key: "dm", word: "someone DMs BOSS", tip: "A direct message to the BOSS app" },
  { key: "message", word: "any message is posted", tip: "Every human message in the channels (a support desk)" },
  { key: "followup", word: "a thread follow-up arrives", tip: "Only replies in threads the agent already owns" },
];

export const OPS: { op: Op; word: string; not: string }[] = [
  { op: "contains", word: "contains", not: "doesn't contain" },
  { op: "prefix", word: "starts with", not: "doesn't start with" },
  { op: "suffix", word: "ends with", not: "doesn't end with" },
  { op: "eq", word: "is", not: "is not" },
  { op: "matches", word: "matches", not: "doesn't match" },
  { op: "exists", word: "is set", not: "is not set" },
];

/** The fields a condition can test, in words. A path not listed here is shown as itself. */
export const FIELDS: { field: string; word: string; chat: boolean }[] = [
  { field: "data.text", word: "text", chat: true },
  { field: "bossactor.display", word: "sender", chat: true },
  { field: "bossactor.email_domain", word: "sender's email domain", chat: true },
  { field: "subject", word: "subject", chat: false },
  { field: "type", word: "event type", chat: false },
  { field: "data.action", word: "action", chat: false },
  { field: "data.repository", word: "repository", chat: false },
];
export const fieldWord = (f: string) => FIELDS.find((x) => x.field === f)?.word || f;
export const opWord = (c: Pick<Cond, "op" | "not">) => {
  const o = OPS.find((x) => x.op === c.op) ?? OPS[0];
  return c.not ? o.not : o.word;
};

export function emptyDraft(chat: boolean): Draft {
  return { when: chat ? "mention" : "", channels: [], followups: true, conds: [], custom: null };
}

type Leaf = { field: string; op: string; value?: unknown };
type Node = Leaf | { all: Node[] } | { any: Node[] } | { not: Node };
const isLeaf = (x: unknown): x is Leaf => !!x && typeof x === "object" && typeof (x as Leaf).field === "string" && typeof (x as Leaf).op === "string";

function leafOf(c: Cond): Node {
  const leaf: Leaf = c.op === "exists" ? { field: c.field, op: "exists" } : { field: c.field, op: c.op, value: c.value };
  return c.not ? { not: leaf } : leaf;
}

/** The draft as the route's `config.match` and `config.addressing`. */
export function toMatch(d: Draft, chat: boolean, kind = "slack"): { match: unknown; addressing?: string } {
  if (d.custom !== null) return { match: d.custom };
  const leaves: Node[] = [];
  let addressing: string | undefined;
  const map = kind === "teams" ? TEAMS : SLACK;
  if (chat) {
    const when = d.when as ChatWhen;
    const types = when === "mention" ? [map.mention] : when === "dm" ? [map.dm]
      : when === "message" ? [map.channel, map.thread] : [map.thread];
    if (d.followups && (when === "mention" || when === "dm")) types.push(map.thread);
    leaves.push({ field: "type", op: "in", value: types });
    addressing = when === "message" ? "channel_all" : when === "followup" ? "thread_followups" : "mention_or_dm";
    const channels = d.channels.map((c) => c.replace(/^#/, "").trim()).filter(Boolean);
    if (channels.length && when !== "dm") leaves.push({ field: "data.channel", op: "in", value: channels });
  } else if (d.when) {
    leaves.push(d.when.endsWith(".*") ? { field: "type", op: "prefix", value: d.when.slice(0, -1) } : { field: "type", op: "eq", value: d.when });
  }
  leaves.push(...d.conds.filter((c) => c.field && (c.op === "exists" || c.value.trim())).map(leafOf));
  const match = leaves.length === 0 ? null : leaves.length === 1 ? leaves[0] : { all: leaves };
  return addressing ? { match, addressing } : { match };
}

const OP_SET = new Set(OPS.map((x) => x.op));

/** A stored rule back into a draft; a shape the builder cannot say in words stays custom. */
export function fromMatch(match: unknown, addressing: string, chat: boolean): Draft {
  const d = emptyDraft(chat);
  if (match === null || match === undefined || (typeof match === "object" && !Array.isArray(match) && Object.keys(match as object).length === 0)) {
    if (!chat) d.when = "";
    return d;
  }
  const items: unknown[] = isLeaf(match) || (match as { not?: unknown }).not ? [match] : Array.isArray((match as { all?: unknown[] }).all) ? (match as { all: unknown[] }).all : [];
  if (!items.length) return { ...d, custom: match };
  let types: string[] | null = null;
  for (const it of items) {
    const neg = !!it && typeof it === "object" && "not" in (it as object) && isLeaf((it as { not: unknown }).not);
    const leaf = neg ? (it as { not: Leaf }).not : it;
    if (!isLeaf(leaf)) return { ...d, custom: match };
    if (!neg && leaf.field === "type" && types === null && (leaf.op === "in" || leaf.op === "eq" || (!chat && leaf.op === "prefix"))) {
      types = leaf.op === "in" && Array.isArray(leaf.value) ? leaf.value.map(String) : [String(leaf.value || "")];
      if (!chat) d.when = leaf.op === "prefix" ? `${String(leaf.value)}*` : String(leaf.value || "");
      continue;
    }
    if (chat && !neg && leaf.field === "data.channel" && leaf.op === "in" && Array.isArray(leaf.value) && !d.channels.length) {
      d.channels = leaf.value.map(String);
      continue;
    }
    if (!OP_SET.has(leaf.op as Op)) return { ...d, custom: match };
    d.conds.push({ field: leaf.field, op: leaf.op as Op, value: leaf.op === "exists" ? "" : String(leaf.value || ""), not: neg });
  }
  if (chat) {
    const t = new Set(types ?? []);
    const hasThread = t.has(SLACK.thread) || t.has(TEAMS.thread);
    const hasDm = t.has(SLACK.dm) || t.has(TEAMS.dm);
    const hasMention = t.has(SLACK.mention) || t.has(TEAMS.mention);
    if (addressing === "channel_all") { d.when = "message"; d.followups = true; }
    else if (addressing === "thread_followups") { d.when = "followup"; d.followups = true; }
    else if (hasDm && !hasMention) { d.when = "dm"; d.followups = hasThread; }
    else { d.when = "mention"; d.followups = types === null || hasThread; }
  }
  if (!chat && d.when.endsWith("*") && !d.when.endsWith(".*")) d.when = `${d.when.slice(0, -1)}.*`.replace(/\.\.\*$/, ".*");
  return d;
}

/** The rule in one line, for the list: "Mentions in #support", "DMs to BOSS", "Every message in #help". */
export function ruleWords(d: Draft, chat: boolean): string {
  if (d.custom !== null) return "Custom filter";
  const where = d.channels.length ? ` in ${d.channels.map((c) => `#${c.replace(/^#/, "")}`).join(", ")}` : "";
  let head: string;
  if (chat) {
    head = d.when === "dm" ? "DMs to BOSS" : d.when === "message" ? `Every message${where}` : d.when === "followup" ? `Thread follow-ups${where}` : `Mentions${where || " anywhere"}`;
  } else {
    head = d.when ? typeWords(d.when) : "Every event";
  }
  const extra = d.conds.length ? ` · ${d.conds.map((c) => `${fieldWord(c.field)} ${opWord(c)}${c.op === "exists" ? "" : ` “${c.value}”`}`).join(" · ")}` : "";
  return head + extra;
}

/** "com.github.issues.*" → "GitHub issues", "com.boss.webhook.received" → "Webhook received". */
export function typeWords(t: string): string {
  const parts = t.replace(/^com\./, "").replace(/\.\*$/, "").split(".");
  const vendor = parts.shift() || "";
  const rest = parts.join(" ").replace(/_/g, " ");
  const label = vendor === "github" ? "GitHub" : vendor === "boss" ? "" : vendor.charAt(0).toUpperCase() + vendor.slice(1);
  const words = `${label} ${rest}`.trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The defaults a source class gets for its per-entity limit (intake_guards CHAT/RECORD_PER_ENTITY). */
export const limitDefaults = (chat: boolean) => (chat ? { per_entity: 30, window: 3600 } : { per_entity: 5, window: 86400 });
