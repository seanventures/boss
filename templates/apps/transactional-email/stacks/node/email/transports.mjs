// The ways a message leaves the app. Each transport's send(message, {idempotencyKey}) resolves to
// {providerId} or throws a TransportError that says whether trying again could help (transient)
// and a fixed code. A provider's raw answer is never put in an error: it can echo addresses or,
// for a misconfigured client, the credential.
//
//   smtp     nodemailer, STARTTLS required by default (SMTP_TLS=implicit for port 465; `none` only
//            outside production, for a local catcher such as Mailpit)
//   resend   Resend's HTTP API on fetch (no SDK), with Resend's Idempotency-Key header
//   catcher  the local mail catcher: the message is kept by the app and shown at /dev/mail
//   off      production with no provider: every send fails visibly
import { randomUUID } from "node:crypto";

import nodemailer from "nodemailer";

import { RESEND_API } from "./config.mjs";

export class TransportError extends Error {
  constructor(code, { transient = false, status = null } = {}) {
    super(code);
    this.code = code;
    this.transient = transient;
    this.status = status;
  }
}

export function smtpTransport(smtp, { connectionTimeout = 10_000 } = {}) {
  const transporter = nodemailer.createTransport({
    host: smtp.host,
    port: smtp.port,
    secure: smtp.tls === "implicit",
    requireTLS: smtp.tls === "starttls",
    ignoreTLS: smtp.tls === "none",
    auth: smtp.user ? { user: smtp.user, pass: smtp.password } : undefined,
    connectionTimeout,
    greetingTimeout: connectionTimeout,
    socketTimeout: connectionTimeout * 3,
    disableFileAccess: true, // a template can never attach a file from this machine
    disableUrlAccess: true, // ... or make the server fetch a URL
  });
  return {
    name: "smtp",
    async send(message) {
      try {
        const info = await transporter.sendMail({
          from: message.from, to: message.to, replyTo: message.replyTo || undefined,
          subject: message.subject, text: message.text, html: message.html,
          headers: message.headers,
        });
        return { providerId: info.messageId || null };
      } catch (err) {
        if (err && err.code === "EAUTH") throw new TransportError("smtp_auth_failed");
        const code = Number(err && err.responseCode);
        if (code >= 500 && code < 600) throw new TransportError("smtp_rejected", { status: code });
        if (code >= 400 && code < 500) throw new TransportError("smtp_try_later", { transient: true, status: code });
        throw new TransportError("smtp_unreachable", { transient: true });
      }
    },
  };
}

export function resendTransport(resend, { apiBase = RESEND_API, fetchImpl = fetch } = {}) {
  return {
    name: "resend",
    async send(message, { idempotencyKey } = {}) {
      const headers = { Authorization: `Bearer ${resend.apiKey}`, "Content-Type": "application/json" };
      // Resend keeps the key for 24 hours: a retry of the same message is never sent twice.
      if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey.slice(0, 256);
      const body = {
        from: message.from, to: [message.to], subject: message.subject, text: message.text,
        html: message.html, reply_to: message.replyTo || undefined, headers: message.headers,
      };
      let r;
      try {
        r = await fetchImpl(`${apiBase}/emails`, { method: "POST", headers, body: JSON.stringify(body), signal: AbortSignal.timeout(15_000) });
      } catch {
        throw new TransportError("resend_unreachable", { transient: true });
      }
      const out = await r.json().catch(() => ({}));
      if (r.ok && out && out.id) return { providerId: String(out.id) };
      if (r.status === 429 || r.status >= 500) throw new TransportError("resend_try_later", { transient: true, status: r.status });
      if (r.status === 401 || r.status === 403) throw new TransportError("resend_auth_failed", { status: r.status });
      throw new TransportError("resend_rejected", { status: r.status });
    },
  };
}

//: The catcher keeps at most this many messages (the oldest go first).
export const CATCHER_LIMIT = 200;

export function catcherTransport({ limit = CATCHER_LIMIT, now = () => Date.now() } = {}) {
  const messages = [];
  return {
    name: "catcher",
    messages,
    async send(message) {
      const id = randomUUID();
      messages.unshift({ id, at: new Date(now()).toISOString(), from: message.from, to: message.to,
        subject: message.subject, text: message.text, html: message.html || null, template: message.template });
      messages.length = Math.min(messages.length, limit);
      return { providerId: `catcher-${id}` };
    },
    clear() {
      messages.length = 0;
    },
  };
}

export function offTransport() {
  return {
    name: "off",
    async send() {
      throw new TransportError("email_not_configured");
    },
  };
}

export function transportFor(config, options = {}) {
  if (options.transport) return options.transport;
  switch (config.provider) {
    case "smtp": return smtpTransport(config.smtp, options.smtp);
    case "resend": return resendTransport(config.resend, { apiBase: options.resendApiBase, fetchImpl: options.fetchImpl });
    case "catcher": return catcherTransport({ now: options.now });
    default: return offTransport();
  }
}
