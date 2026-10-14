// The transactional-email connector for an Express app:
//
//   const email = createEmail(loadEmailConfig(), { findUserByEmail, setPassword, canInvite, onInviteAccepted });
//   app.use(email.router);
//   await email.notify("ada@example.com", { title: "Your report is ready", body: "...", url: "/reports/7" });
//
// Routes (the contract the connector's own tests check over HTTP):
//   GET    /api/email/status                    which way mail goes; never a credential
//   POST   /api/email/password-reset            {email} -> 202, the same answer whether or not the address has an account
//   POST   /api/email/password-reset/confirm    {token, password} -> 204 | 400 invalid_token | 400 weak_password
//   POST   /api/email/invites                   {email, role?} -> 201 | 403 forbidden (canInvite) | 502 email_not_sent
//   POST   /api/email/invites/accept            {token} -> 200 {email, role} | 400 invalid_token
//   GET    /dev/mail                            the mail catcher's inbox (HTML); says it is off when it is
//   GET    /dev/mail/messages                   the caught messages (JSON)
//   GET    /dev/mail/messages/:id               one caught message, with its text and HTML
//   DELETE /dev/mail/messages                   empty the catcher
import express from "express";

import { escapeHtml, oneLine, render, DEFAULT_TEMPLATES } from "./templates.mjs";
import { MemoryEmailStore } from "./store.mjs";
import { TransportError, transportFor } from "./transports.mjs";

const EMAIL = /^[^\s@<>()",;:\\[\]]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$/;
const ROLE = /^[a-z][a-z0-9_-]{0,31}$/;
//: At most this many reset emails per address per window; more requests get the same 202 and no mail.
export const RESET_LIMIT = 3;
export const RESET_WINDOW_MS = 15 * 60_000;
const MIN_PASSWORD = 8;
const MAX_PASSWORD = 512;

export class EmailInputError extends Error {}

export function normalizeEmail(value) {
  const email = String(value ?? "").trim().toLowerCase();
  if (email.length > 254 || !EMAIL.test(email)) return null;
  return email;
}

export function createEmail(config, options = {}) {
  const now = options.now || (() => Date.now());
  const sleep = options.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const log = options.logger || console;
  const store = options.store || new MemoryEmailStore({ now });
  const transport = transportFor(config, { ...options, now });
  const templates = { ...DEFAULT_TEMPLATES, ...(options.templates || {}) };
  const findUserByEmail = options.findUserByEmail || (async () => null);
  const setPassword = options.setPassword || (async () => { throw new Error("setPassword is not wired"); });
  const canInvite = options.canInvite || (() => false); // default: nobody may invite
  const onInviteAccepted = options.onInviteAccepted || (async () => {});
  const inviteRoles = options.inviteRoles || ["member"];
  const onFailure = options.onFailure || (() => {});

  function link(path, token) {
    return `${config.baseUrl}${path}?token=${encodeURIComponent(token)}`;
  }

  function absoluteUrl(url) {
    if (url === undefined || url === null || url === "") return "";
    const s = String(url);
    if (/^\/(?!\/)/.test(s)) return config.baseUrl + s;
    if (/^https?:\/\/[^\s]+$/i.test(s)) return s;
    throw new EmailInputError("a notification's url must be an http(s) address or a path on this app");
  }

  // Send one templated message, at most once per dedupeKey. Resolves to the outbox row; a failure
  // is recorded on the row (status failed, last_error, attempts), logged and passed to onFailure,
  // never thrown away.
  async function send(template, { to, vars = {}, dedupeKey = null }) {
    const address = normalizeEmail(to);
    if (!address) throw new EmailInputError("not an email address");
    const msg = render(templates, template, { appName: config.appName, ...vars });
    let row = store.createMessage({ dedupeKey, template, to: address, subject: msg.subject, provider: transport.name });
    if (row.status !== "queued" && row.status !== "failed") return { ...row };
    const message = { ...msg, template, from: config.from, replyTo: config.replyTo, to: address,
      headers: { "X-Entity-Ref-ID": row.id } };
    for (;;) {
      row = store.transition(row.id, "sending", { attempts: row.attempts + 1 });
      try {
        const { providerId } = await transport.send(message, { idempotencyKey: dedupeKey || row.id });
        row = store.transition(row.id, transport.name === "catcher" ? "caught" : "sent",
          { provider_message_id: providerId, sent_at: new Date(now()).toISOString(), last_error: null });
        return { ...row };
      } catch (err) {
        const e = err instanceof TransportError ? err : new TransportError("send_failed", { transient: false });
        if (e.transient && row.attempts < config.maxAttempts) {
          row = store.transition(row.id, "retrying", { last_error: e.code });
          await sleep(250 * 2 ** (row.attempts - 1));
          continue;
        }
        row = store.transition(row.id, "failed", { last_error: e.code });
        // The address stays in the outbox, not in the log.
        log.error(`[email] message ${row.id} (${template}) failed after ${row.attempts} attempt(s): ${e.code}`);
        try { onFailure({ ...row }); } catch { /* a failing hook never hides the failure */ }
        return { ...row };
      }
    }
  }

  async function notify(to, { title, body, url, action, dedupeKey = null } = {}) {
    if (!oneLine(title) || !body) throw new EmailInputError("a notification needs a title and a body");
    return send("notification", { to, dedupeKey, vars: { title: oneLine(title), body: String(body), url: absoluteUrl(url), action } });
  }

  const router = express.Router();
  const json = express.json({ limit: "16kb" });
  const bad = (res, status, error) => res.status(status).json({ error });

  router.get("/api/email/status", (req, res) => {
    res.set("Cache-Control", "no-store").json({
      provider: transport.name,
      from: config.from,
      catcher: transport.name === "catcher",
      catcher_url: transport.name === "catcher" ? "/dev/mail" : null,
      templates: Object.keys(templates),
      outbox: store.counts(),
    });
  });

  router.post("/api/email/password-reset", json, async (req, res, next) => {
    try {
      const email = normalizeEmail(req.body && req.body.email);
      // One answer for every address: whether an account exists is never revealed.
      const answer = () => res.status(202).json({ ok: true });
      if (!email) return bad(res, 400, "bad_email");
      if (store.recentTokenCount("password_reset", email, RESET_WINDOW_MS) >= RESET_LIMIT) return answer();
      const user = await findUserByEmail(email);
      if (!user) return answer();
      const { token, row } = store.issueToken({ kind: "password_reset", email, userId: String(user.id), ttlMs: config.resetTtlMs });
      // The link is built from APP_BASE_URL, NEVER from the request's Host header (reset poisoning).
      await send("password_reset", { to: email, dedupeKey: `password_reset:${row.id}`,
        vars: { url: link(config.resetPath, token), minutes: Math.round(config.resetTtlMs / 60_000) } });
      return answer();
    } catch (err) { return next(err); }
  });

  router.post("/api/email/password-reset/confirm", json, async (req, res, next) => {
    try {
      const { token, password } = req.body || {};
      if (typeof password !== "string" || password.length < MIN_PASSWORD || password.length > MAX_PASSWORD) {
        return bad(res, 400, "weak_password");
      }
      const row = store.useToken("password_reset", token);
      if (!row) return bad(res, 400, "invalid_token");
      await setPassword(row.user_id, password);
      return res.status(204).end();
    } catch (err) { return next(err); }
  });

  router.post("/api/email/invites", json, async (req, res, next) => {
    try {
      const inviter = await canInvite(req);
      if (!inviter) return bad(res, 403, "forbidden");
      const email = normalizeEmail(req.body && req.body.email);
      if (!email) return bad(res, 400, "bad_email");
      const role = req.body && req.body.role !== undefined ? String(req.body.role) : inviteRoles[0];
      if (!ROLE.test(role) || !inviteRoles.includes(role)) return bad(res, 400, "bad_role");
      const invitedBy = typeof inviter === "object" ? String(inviter.id ?? "") || null : null;
      const inviterName = typeof inviter === "object" ? oneLine(inviter.name || inviter.email || "") : "";
      const { token, row } = store.issueToken({ kind: "invite", email, role, invitedBy, ttlMs: config.inviteTtlMs });
      const sent = await send("invite", { to: email, dedupeKey: `invite:${row.id}`,
        vars: { url: link(config.invitePath, token), inviter: inviterName, role, hours: Math.round(config.inviteTtlMs / 3_600_000) } });
      if (sent.status === "failed") {
        return res.status(502).json({ error: "email_not_sent", code: sent.last_error, invite_id: row.id, message_id: sent.id });
      }
      return res.status(201).json({ invite_id: row.id, message_id: sent.id, status: sent.status });
    } catch (err) { return next(err); }
  });

  router.post("/api/email/invites/accept", json, async (req, res, next) => {
    try {
      const row = store.useToken("invite", req.body && req.body.token);
      if (!row) return bad(res, 400, "invalid_token");
      await onInviteAccepted({ email: row.email, role: row.role, invitedBy: row.invited_by }, req);
      return res.json({ email: row.email, role: row.role });
    } catch (err) { return next(err); }
  });

  // ---- the mail catcher (builds, previews, development; never production) -----------------------
  const caught = () => (transport.name === "catcher" ? transport.messages : null);
  const devHeaders = (res) => res.set({ "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer" });

  router.get("/dev/mail", (req, res) => {
    devHeaders(res).set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'");
    res.type("html").send(inboxPage(caught(), transport.name, config.appName));
  });

  router.get("/dev/mail/messages", (req, res) => {
    const messages = caught();
    devHeaders(res).json({
      enabled: Boolean(messages), provider: transport.name,
      messages: (messages || []).map(({ id, at, to, subject, template }) => ({ id, at, to, subject, template })),
    });
  });

  router.get("/dev/mail/messages/:id", (req, res) => {
    const found = (caught() || []).find((m) => m.id === req.params.id);
    if (!found) return bad(devHeaders(res), 404, "not_found");
    return devHeaders(res).json(found);
  });

  router.delete("/dev/mail/messages", (req, res) => {
    if (!caught()) return bad(devHeaders(res), 404, "catcher_off");
    transport.clear();
    return devHeaders(res).status(204).end();
  });

  // Errors are fixed codes: never a stack trace, a provider's answer or an address.
  router.use((err, req, res, next) => {
    if (res.headersSent) return next(err);
    if (err instanceof EmailInputError) return bad(res, 400, "bad_request");
    if (err && err.type === "entity.parse.failed") return bad(res, 400, "bad_json");
    if (err && err.type === "entity.too.large") return bad(res, 413, "too_large");
    log.error(`[email] ${req.method} ${req.path} failed: ${err && err.name}`);
    return bad(res, 500, "email_error");
  });

  return { router, send, notify, store, transport, config };
}

function linkify(escaped) {
  return escaped.replace(/(https?:\/\/[^\s<"]+)/g, '<a href="$1" rel="noreferrer">$1</a>');
}

export function inboxPage(messages, provider, appName) {
  const head = `<!doctype html><html><head><meta charset="utf-8"><title>Mail catcher · ${escapeHtml(appName)}</title>` +
    "<style>body{font-family:system-ui,sans-serif;margin:24px;max-width:860px;color:#1f2328}" +
    "details{border:1px solid #d0d7de;border-radius:6px;margin:8px 0;padding:8px 12px}" +
    "summary{cursor:pointer}pre{white-space:pre-wrap;background:#f6f8fa;padding:12px;border-radius:6px}" +
    ".dim{color:#6e7781}</style></head><body>";
  if (!messages) {
    return `${head}<h1>Mail catcher</h1><p>The mail catcher is off: this app sends email through ` +
      `<b>${escapeHtml(provider)}</b>${provider === "off" ? " (no provider is set, so nothing is sent)" : ""}.</p></body></html>`;
  }
  const items = messages.map((m) => `<details><summary><b>${escapeHtml(m.subject)}</b> ` +
    `<span class="dim">to ${escapeHtml(m.to)} · ${escapeHtml(m.at)} · ${escapeHtml(m.template)}</span></summary>` +
    `<pre>${linkify(escapeHtml(m.text))}</pre></details>`).join("");
  return `${head}<h1>Mail catcher</h1><p class="dim">Every email this app sends is kept here instead of being ` +
    "delivered (no provider is set). It holds the newest 200; restarting the app empties it.</p>" +
    (items || "<p>No mail yet.</p>") + "</body></html>";
}
