// The transactional-email connector's own tests, against its HTTP routes, with SMTP and Resend
// replaced by in-process fakes. No test reaches the network or uses a real credential.
import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";

import { createApp } from "../../server.mjs";
import { EmailConfigError, RESEND_API, loadEmailConfig } from "../../email/config.mjs";
import { RESET_LIMIT } from "../../email/routes.mjs";
import { TRANSITIONS, hashToken } from "../../email/store.mjs";
import { startFakeResend, fakeResendKey } from "./fake-resend.mjs";
import { startFakeSmtp } from "./fake-smtp.mjs";

const BASE_URL = "https://shop.example.test";
const quiet = { error() {}, log() {} };

// One app per test; the users are the app's own (here, a map), reached only through the hooks.
async function boot({ env = {}, options = {} } = {}) {
  const users = new Map([["ada@example.com", { id: "u1", email: "ada@example.com", password: null }]]);
  const accepted = [];
  const failures = [];
  const clock = { t: Date.parse("2026-10-14T12:00:00Z") };
  const config = loadEmailConfig({ APP_BASE_URL: BASE_URL, APP_NAME: "Shop", ...env });
  const app = createApp(config, {
    now: () => clock.t,
    sleep: async () => {},
    logger: quiet,
    findUserByEmail: async (email) => users.get(email) || null,
    setPassword: async (id, password) => {
      for (const u of users.values()) if (u.id === id) u.password = password;
    },
    onInviteAccepted: async (invite) => accepted.push(invite),
    onFailure: (row) => failures.push(row),
    ...options,
  });
  const server = await new Promise((resolve) => {
    const s = app.listen(0, "127.0.0.1", () => resolve(s));
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const email = app.locals.email;
  const ctx = {
    app, email, users, accepted, failures, clock, base,
    async post(path, body, headers = {}) {
      const r = await fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body || {}) });
      const text = await r.text();
      return { status: r.status, body: text ? JSON.parse(text) : null };
    },
    async get(path) {
      const r = await fetch(base + path);
      const text = await r.text();
      const json = (r.headers.get("content-type") || "").includes("json");
      return { status: r.status, body: json ? JSON.parse(text) : text, headers: r.headers };
    },
    caught() {
      return email.transport.messages || [];
    },
    tokenIn(message) {
      const m = message.text.match(/token=([A-Za-z0-9_-]+)/);
      assert.ok(m, "the email carries a link with a token");
      return decodeURIComponent(m[1]);
    },
    close: () => new Promise((r) => server.close(r)),
  };
  return ctx;
}

test("without a provider, a build or preview uses the mail catcher and /dev/mail shows the mail", async () => {
  const t = await boot();
  try {
    const status = await t.get("/api/email/status");
    assert.equal(status.status, 200);
    assert.equal(status.body.provider, "catcher");
    assert.equal(status.body.catcher_url, "/dev/mail");
    assert.deepEqual(status.body.templates, ["password_reset", "invite", "notification"]);
    await t.email.notify("ada@example.com", { title: "Your report <b>is</b> ready", body: "Open it", url: "/reports/7" });
    const list = await t.get("/dev/mail/messages");
    assert.equal(list.body.enabled, true);
    assert.equal(list.body.messages.length, 1);
    assert.equal(list.body.messages[0].to, "ada@example.com");
    const one = await t.get(`/dev/mail/messages/${list.body.messages[0].id}`);
    assert.match(one.body.text, /https:\/\/shop\.example\.test\/reports\/7/);
    const page = await t.get("/dev/mail");
    assert.equal(page.status, 200);
    assert.match(page.headers.get("content-security-policy"), /default-src 'none'/);
    assert.equal(page.headers.get("cache-control"), "no-store");
    assert.ok(page.body.includes("Your report &lt;b&gt;is&lt;/b&gt; ready"), "the inbox escapes what it shows");
    assert.ok(!page.body.includes("<b>is</b>"));
    const cleared = await fetch(t.base + "/dev/mail/messages", { method: "DELETE" });
    assert.equal(cleared.status, 204);
    assert.equal((await t.get("/dev/mail/messages")).body.messages.length, 0);
  } finally { await t.close(); }
});

test("a reset link is built from APP_BASE_URL, never from the request's Host header", async () => {
  const t = await boot();
  try {
    const r = await t.post("/api/email/password-reset", { email: " Ada@Example.com " }, { "X-Forwarded-Host": "evil.example", Host: "evil.example" });
    assert.equal(r.status, 202);
    const [mail] = t.caught();
    assert.equal(mail.to, "ada@example.com");
    assert.equal(mail.template, "password_reset");
    assert.match(mail.text, /https:\/\/shop\.example\.test\/reset-password\?token=/);
    assert.ok(!mail.text.includes("evil.example") && !mail.html.includes("evil.example"));
    // Only the hash is stored; the raw token is in the email alone.
    const token = t.tokenIn(mail);
    const rows = [...t.email.store.tokens.values()];
    assert.equal(rows.length, 1);
    assert.equal(rows[0].token_hash, hashToken(token));
    assert.ok(!JSON.stringify(rows).includes(token));
  } finally { await t.close(); }
});

test("an unknown address gets the same answer and no mail (no account enumeration)", async () => {
  const t = await boot();
  try {
    const known = await t.post("/api/email/password-reset", { email: "ada@example.com" });
    const unknown = await t.post("/api/email/password-reset", { email: "nobody@example.com" });
    assert.equal(unknown.status, known.status);
    assert.deepEqual(unknown.body, known.body);
    assert.equal(t.caught().length, 1);
    assert.equal((await t.post("/api/email/password-reset", { email: "not an address" })).status, 400);
    assert.equal((await t.post("/api/email/password-reset", { email: "a@b.com\r\nBcc: x@y.com" })).status, 400);
  } finally { await t.close(); }
});

test("a reset token sets the password once, and expires", async () => {
  const t = await boot();
  try {
    await t.post("/api/email/password-reset", { email: "ada@example.com" });
    const token = t.tokenIn(t.caught()[0]);
    assert.equal((await t.post("/api/email/password-reset/confirm", { token, password: "short" })).body.error, "weak_password");
    const ok = await t.post("/api/email/password-reset/confirm", { token, password: "correct horse battery" });
    assert.equal(ok.status, 204);
    assert.equal(t.users.get("ada@example.com").password, "correct horse battery");
    const again = await t.post("/api/email/password-reset/confirm", { token, password: "another long one" });
    assert.equal(again.status, 400);
    assert.equal(again.body.error, "invalid_token");
    assert.equal(t.users.get("ada@example.com").password, "correct horse battery");

    await t.post("/api/email/password-reset", { email: "ada@example.com" });
    const late = t.tokenIn(t.caught()[0]);
    t.clock.t += 31 * 60_000; // the default lifetime is 30 minutes
    assert.equal((await t.post("/api/email/password-reset/confirm", { token: late, password: "too late now!" })).body.error, "invalid_token");
    assert.equal((await t.post("/api/email/password-reset/confirm", { token: "fake-forged-token-value", password: "fake password 1" })).body.error, "invalid_token");
  } finally { await t.close(); }
});

test("a new reset link replaces the earlier one, and reset mail is rate limited per address", async () => {
  const t = await boot();
  try {
    await t.post("/api/email/password-reset", { email: "ada@example.com" });
    const first = t.tokenIn(t.caught()[0]);
    await t.post("/api/email/password-reset", { email: "ada@example.com" });
    const second = t.tokenIn(t.caught()[0]);
    assert.notEqual(first, second);
    assert.equal((await t.post("/api/email/password-reset/confirm", { token: first, password: "first link pw" })).status, 400);
    for (let i = 2; i < RESET_LIMIT + 3; i++) {
      assert.equal((await t.post("/api/email/password-reset", { email: "ada@example.com" })).status, 202);
    }
    assert.equal(t.caught().length, RESET_LIMIT);
    t.clock.t += 16 * 60_000;
    await t.post("/api/email/password-reset", { email: "ada@example.com" });
    assert.equal(t.caught().length, RESET_LIMIT + 1);
  } finally { await t.close(); }
});

test("nobody may invite until the app says who can; an invite is accepted once", async () => {
  const closed = await boot();
  try {
    const r = await closed.post("/api/email/invites", { email: "grace@example.com" });
    assert.equal(r.status, 403);
    assert.equal(closed.caught().length, 0);
  } finally { await closed.close(); }

  const t = await boot({ options: {
    canInvite: (req) => (req.get("X-CSRF-Token") === "csrf-fake-value" ? { id: "u1", name: "Ada\r\nBcc: x@y.com" } : null),
    inviteRoles: ["member", "admin"],
  } });
  try {
    assert.equal((await t.post("/api/email/invites", { email: "grace@example.com" })).status, 403);
    const h = { "X-CSRF-Token": "csrf-fake-value" };
    assert.equal((await t.post("/api/email/invites", { email: "grace@example.com", role: "owner" }, h)).body.error, "bad_role");
    assert.equal((await t.post("/api/email/invites", { email: "nope" }, h)).body.error, "bad_email");
    const sent = await t.post("/api/email/invites", { email: "grace@example.com", role: "admin" }, h);
    assert.equal(sent.status, 201);
    assert.equal(sent.body.status, "caught");
    const [mail] = t.caught();
    assert.equal(mail.subject, "Ada Bcc: x@y.com invited you to Shop", "no CR/LF reaches a subject");
    assert.match(mail.text, /https:\/\/shop\.example\.test\/accept-invite\?token=/);
    const token = t.tokenIn(mail);
    const ok = await t.post("/api/email/invites/accept", { token });
    assert.deepEqual(ok.body, { email: "grace@example.com", role: "admin" });
    assert.deepEqual(t.accepted, [{ email: "grace@example.com", role: "admin", invitedBy: "u1" }]);
    assert.equal((await t.post("/api/email/invites/accept", { token })).body.error, "invalid_token");
    // A reset token is not an invite token.
    await t.post("/api/email/password-reset", { email: "ada@example.com" });
    assert.equal((await t.post("/api/email/invites/accept", { token: t.tokenIn(t.caught()[0]) })).status, 400);
  } finally { await t.close(); }
});

test("a notification with the same dedupe key is sent once", async () => {
  const t = await boot();
  try {
    const a = await t.email.notify("ada@example.com", { title: "Order shipped", body: "On its way", dedupeKey: "order:7:shipped" });
    const b = await t.email.notify("ada@example.com", { title: "Order shipped", body: "On its way", dedupeKey: "order:7:shipped" });
    assert.equal(a.id, b.id);
    assert.equal(t.caught().length, 1);
    await assert.rejects(t.email.notify("ada@example.com", { title: "x", body: "y", url: "javascript:alert(1)" }));
    await assert.rejects(t.email.notify("ada@example.com\nBcc: x@y.com", { title: "x", body: "y" }));
  } finally { await t.close(); }
});

test("SMTP: delivers through the server with AUTH, and a try-later answer is retried", async () => {
  const smtp = await startFakeSmtp({ user: "mailer", password: "smtp-fake-password" });
  const t = await boot({ env: { SMTP_HOST: smtp.host, SMTP_PORT: String(smtp.port), SMTP_USER: "mailer",
    SMTP_PASSWORD: "smtp-fake-password", SMTP_TLS: "none", EMAIL_FROM: "Shop <no-reply@shop.example.test>" } });
  try {
    assert.equal((await t.get("/api/email/status")).body.provider, "smtp");
    smtp.state.failNext.push(451);
    const row = await t.email.notify("ada@example.com", { title: "Hello", body: "From SMTP" });
    assert.equal(row.status, "sent");
    assert.equal(row.attempts, 2);
    assert.equal(smtp.state.messages.length, 1);
    const [m] = smtp.state.messages;
    assert.equal(m.from, "no-reply@shop.example.test");
    assert.deepEqual(m.to, ["ada@example.com"]);
    assert.match(m.raw, /Subject: Hello/);
    // The catcher is off when a provider is set; its pages say so and hold nothing.
    const list = await t.get("/dev/mail/messages");
    assert.deepEqual([list.status, list.body.enabled, list.body.messages], [200, false, []]);
    assert.match((await t.get("/dev/mail")).body, /mail catcher is off/);
  } finally { await t.close(); await smtp.close(); }
});

test("SMTP: a rejected message fails visibly, once, with a fixed code", async () => {
  const smtp = await startFakeSmtp({ user: "mailer", password: "smtp-fake-password" });
  const t = await boot({ env: { SMTP_HOST: smtp.host, SMTP_PORT: String(smtp.port), SMTP_USER: "mailer",
    SMTP_PASSWORD: "smtp-fake-password", SMTP_TLS: "none" } });
  try {
    smtp.state.failNext.push(550);
    const row = await t.email.notify("ada@example.com", { title: "Hello", body: "Rejected" });
    assert.deepEqual([row.status, row.attempts, row.last_error], ["failed", 1, "smtp_rejected"]);
    assert.equal(t.failures.length, 1);
    assert.equal((await t.get("/api/email/status")).body.outbox.failed, 1);
    // Wrong credentials: not retried, and the password is in no answer.
    const wrong = await boot({ env: { SMTP_HOST: smtp.host, SMTP_PORT: String(smtp.port), SMTP_USER: "mailer",
      SMTP_PASSWORD: "smtp-wrong-fake-password", SMTP_TLS: "none" } });
    try {
      const w = await wrong.email.notify("ada@example.com", { title: "Hello", body: "Bad auth" });
      assert.deepEqual([w.status, w.last_error], ["failed", "smtp_auth_failed"]);
      assert.ok(!JSON.stringify(await wrong.get("/api/email/status")).includes("smtp-wrong-fake-password"));
    } finally { await wrong.close(); }
  } finally { await t.close(); await smtp.close(); }
});

test("Resend: the API key goes only in the Authorization header, with an Idempotency-Key per message", async () => {
  const apiKey = fakeResendKey();
  const resend = await startFakeResend({ apiKey });
  const t = await boot({ env: { RESEND_API_KEY: apiKey, EMAIL_FROM: "Shop <no-reply@shop.example.test>" },
    options: { resendApiBase: resend.url } });
  try {
    const status = await t.get("/api/email/status");
    assert.equal(status.body.provider, "resend");
    assert.ok(!JSON.stringify(status.body).includes(apiKey));
    resend.state.answerNext.push(500, 429);
    const row = await t.email.notify("ada@example.com", { title: "Hi", body: "From Resend", dedupeKey: "welcome:u1" });
    assert.deepEqual([row.status, row.attempts], ["sent", 3]);
    assert.equal(resend.state.emails.length, 1);
    const [e] = resend.state.emails;
    assert.equal(e.key, "welcome:u1");
    assert.deepEqual(e.body.to, ["ada@example.com"]);
    assert.equal(e.body.from, "Shop <no-reply@shop.example.test>");
    assert.equal(row.provider_message_id, e.id);
    // Every attempt carried the same key, so Resend can never send it twice.
    assert.deepEqual(new Set(resend.state.requests.map((r) => r.headers["idempotency-key"])), new Set(["welcome:u1"]));
    resend.state.answerNext.push(422);
    const bad = await t.email.notify("ada@example.com", { title: "Hi", body: "Rejected" });
    assert.deepEqual([bad.status, bad.attempts, bad.last_error], ["failed", 1, "resend_rejected"]);
  } finally { await t.close(); await resend.close(); }
});

test("an invite whose email fails says so (502) instead of pretending it was sent", async () => {
  const apiKey = fakeResendKey();
  const resend = await startFakeResend({ apiKey });
  const t = await boot({ env: { RESEND_API_KEY: apiKey, EMAIL_MAX_ATTEMPTS: "2" },
    options: { resendApiBase: resend.url, canInvite: () => ({ id: "u1", name: "Ada" }) } });
  try {
    resend.state.answerNext.push(503, 503);
    const r = await t.post("/api/email/invites", { email: "grace@example.com" });
    assert.equal(r.status, 502);
    assert.deepEqual([r.body.error, r.body.code], ["email_not_sent", "resend_try_later"]);
    assert.equal(t.email.store.failed().length, 1);
  } finally { await t.close(); await resend.close(); }
});

test("production never runs the catcher: with no provider, email is off and every send fails visibly", async () => {
  assert.throws(() => loadEmailConfig({ NODE_ENV: "production", EMAIL_PROVIDER: "catcher" }), EmailConfigError);
  assert.throws(() => loadEmailConfig({ APP_ENV: "production", SMTP_HOST: "smtp.example.com", SMTP_TLS: "none" }), EmailConfigError);
  const t = await boot({ env: { NODE_ENV: "production" } });
  try {
    assert.equal((await t.get("/api/email/status")).body.provider, "off");
    assert.equal((await t.post("/api/email/password-reset", { email: "ada@example.com" })).status, 202);
    assert.equal(t.failures.length, 1);
    assert.equal(t.failures[0].last_error, "email_not_configured");
    const page = await t.get("/dev/mail");
    assert.equal(page.status, 200);
    assert.match(page.body, /no provider is set, so nothing is sent/);
    assert.ok(!page.body.includes("token="), "no link is ever shown on a production page");
    assert.equal((await t.get("/dev/mail/messages")).body.messages.length, 0);
  } finally { await t.close(); }
});

test("configuration names what is wrong without echoing a value, and Resend's host is fixed", () => {
  const secret = "re_fake_" + crypto.randomBytes(8).toString("hex");
  assert.throws(() => loadEmailConfig({ EMAIL_PROVIDER: "resend" }), /needs RESEND_API_KEY/);
  assert.throws(() => loadEmailConfig({ EMAIL_PROVIDER: "smtp" }), /needs SMTP_HOST/);
  assert.throws(() => loadEmailConfig({ EMAIL_PROVIDER: "sendmail" }), /EMAIL_PROVIDER must be one of/);
  assert.throws(() => loadEmailConfig({ SMTP_HOST: "smtp.example.com", SMTP_USER: "u" }), /SMTP_PASSWORD is not set/);
  try {
    loadEmailConfig({ EMAIL_PROVIDER: "smtp", SMTP_HOST: "smtp.example.com", SMTP_PORT: secret });
    assert.fail("a bad port was accepted");
  } catch (err) {
    assert.ok(!String(err.message).includes(secret));
  }
  const c = loadEmailConfig({ RESEND_API_KEY: secret, RESEND_API_URL: "https://evil.example" });
  assert.equal(c.provider, "resend");
  assert.equal(RESEND_API, "https://api.resend.com");
  assert.ok(!JSON.stringify(c).includes("evil.example"));
  assert.equal(loadEmailConfig({ APP_BASE_URL: "https://app.example.test/", APP_NAME: "Shop" }).from, "Shop <no-reply@app.example.test>");
  assert.throws(() => loadEmailConfig({ EMAIL_RESET_PATH: "https://evil.example/reset" }), /path on this app/);
});

test("the outbox only moves along its state machine", () => {
  assert.deepEqual(TRANSITIONS.sent, []);
  assert.deepEqual(TRANSITIONS.caught, []);
  assert.ok(TRANSITIONS.failed.includes("sending"), "a failed message may be sent again");
  assert.ok(!TRANSITIONS.queued.includes("sent"), "nothing is sent without an attempt");
});
