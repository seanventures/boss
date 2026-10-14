// The outbox and the one-use tokens behind reset and invite links.
//
// This reference keeps them in memory so it runs anywhere; ADAPT it to the app's database with
// the tables in schema.sql. What must survive the adaptation:
//   * email_messages.dedupe_key is UNIQUE: a message with a key that was already sent (or caught)
//     is never sent again, so a retried request or a double click sends one email;
//   * a message's status only moves along TRANSITIONS below, and a failure keeps its error code
//     and attempt count where a person can see them (never dropped);
//   * a token is stored ONLY as its SHA-256 hash; the raw token exists in the email alone. A token
//     is used at most once (mark it used in the same statement that checks it is unused).
import { createHash, randomBytes, randomUUID } from "node:crypto";

export const TRANSITIONS = {
  queued: ["sending"],
  sending: ["sent", "caught", "retrying", "failed"],
  retrying: ["sending"],
  sent: [],
  caught: [],
  failed: ["sending"], // a person (or the app) may send a failed message again
};

export class TransitionError extends Error {}

export function hashToken(token) {
  return createHash("sha256").update(String(token)).digest("hex");
}

export function newToken() {
  return randomBytes(32).toString("base64url");
}

export class MemoryEmailStore {
  constructor({ now = () => Date.now() } = {}) {
    this.now = now;
    this.messages = new Map(); // id -> message
    this.byDedupe = new Map(); // dedupe_key -> id
    this.tokens = new Map(); // token_hash -> token row
  }

  // ---- outbox ---------------------------------------------------------------------------------
  findByDedupe(key) {
    const id = key ? this.byDedupe.get(key) : null;
    return id ? this.messages.get(id) : null;
  }

  createMessage({ dedupeKey, template, to, subject, provider }) {
    if (dedupeKey && this.byDedupe.has(dedupeKey)) return this.messages.get(this.byDedupe.get(dedupeKey));
    const row = {
      id: randomUUID(), dedupe_key: dedupeKey || null, template, to_address: to, subject, provider,
      status: "queued", attempts: 0, last_error: null, provider_message_id: null,
      created_at: new Date(this.now()).toISOString(), sent_at: null,
    };
    this.messages.set(row.id, row);
    if (dedupeKey) this.byDedupe.set(dedupeKey, row.id);
    return row;
  }

  transition(id, to, fields = {}) {
    const row = this.messages.get(id);
    if (!row) throw new TransitionError(`no message ${id}`);
    if (!(TRANSITIONS[row.status] || []).includes(to)) throw new TransitionError(`${row.status} -> ${to} is not allowed`);
    Object.assign(row, fields, { status: to });
    return row;
  }

  counts() {
    const out = { queued: 0, sending: 0, retrying: 0, sent: 0, caught: 0, failed: 0 };
    for (const m of this.messages.values()) out[m.status] += 1;
    return out;
  }

  failed() {
    return [...this.messages.values()].filter((m) => m.status === "failed");
  }

  // ---- one-use tokens -------------------------------------------------------------------------
  // kind: "password_reset" | "invite". Returns the RAW token once; only its hash is kept.
  issueToken({ kind, email, userId = null, role = null, invitedBy = null, ttlMs }) {
    if (kind === "password_reset" && userId) {
      // A new reset link replaces every earlier unused one for that person.
      for (const t of this.tokens.values()) {
        if (t.kind === kind && t.user_id === userId && !t.used_at) t.used_at = new Date(this.now()).toISOString();
      }
    }
    const token = newToken();
    const row = {
      id: randomUUID(), kind, token_hash: hashToken(token), email, user_id: userId, role, invited_by: invitedBy,
      expires_at: this.now() + ttlMs, used_at: null, created_at: new Date(this.now()).toISOString(),
    };
    this.tokens.set(row.token_hash, row);
    return { token, row };
  }

  // Check and use in one step: the row when the token is of this kind, unexpired and unused.
  useToken(kind, token) {
    if (typeof token !== "string" || token.length < 20 || token.length > 200) return null;
    const row = this.tokens.get(hashToken(token));
    if (!row || row.kind !== kind || row.used_at || row.expires_at <= this.now()) return null;
    row.used_at = new Date(this.now()).toISOString();
    return row;
  }

  recentTokenCount(kind, email, windowMs) {
    const since = this.now() - windowMs;
    let n = 0;
    for (const t of this.tokens.values()) {
      if (t.kind === kind && t.email === email && Date.parse(t.created_at) > since) n += 1;
    }
    return n;
  }
}
