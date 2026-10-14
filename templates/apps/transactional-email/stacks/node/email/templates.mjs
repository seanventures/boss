// The connector's message templates: password reset, invite and a general notification. Each one
// turns its variables into {subject, text, html}. Every variable is escaped in the HTML part, and
// CR/LF never reach a subject (no header injection). Restyle them in the app's own voice and
// brand by passing `templates` to createEmail; keep the escaping.

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function oneLine(value) {
  return String(value ?? "").replace(/[\r\n\t]+/g, " ").trim().slice(0, 200);
}

function layout(appName, bodyHtml) {
  return `<!doctype html><html><body style="font-family:system-ui,sans-serif;line-height:1.5;color:#1f2328">` +
    `${bodyHtml}<p style="color:#6e7781;font-size:12px">${escapeHtml(appName)}</p></body></html>`;
}

function button(url, label) {
  return `<p><a href="${escapeHtml(url)}" style="display:inline-block;padding:10px 16px;border-radius:6px;` +
    `background:#1f6feb;color:#fff;text-decoration:none">${escapeHtml(label)}</a></p>`;
}

export const DEFAULT_TEMPLATES = {
  password_reset: ({ appName, url, minutes }) => ({
    subject: `Reset your ${appName} password`,
    text: `Someone asked to reset the password for this address on ${appName}.\n\n` +
      `Open this link within ${minutes} minutes to choose a new one:\n${url}\n\n` +
      "If it was not you, ignore this email: your password stays the same.",
    html: layout(appName, `<p>Someone asked to reset the password for this address on ${escapeHtml(appName)}.</p>` +
      button(url, "Choose a new password") +
      `<p>The link works once, within ${escapeHtml(minutes)} minutes. If it was not you, ignore this email: ` +
      "your password stays the same.</p>"),
  }),
  invite: ({ appName, url, inviter, role, hours }) => ({
    subject: `${inviter ? `${inviter} invited you` : "You are invited"} to ${appName}`,
    text: `${inviter || "Someone"} invited you to ${appName} as ${role}.\n\n` +
      `Accept within ${Math.round(hours / 24)} days:\n${url}`,
    html: layout(appName, `<p>${escapeHtml(inviter || "Someone")} invited you to ${escapeHtml(appName)} as ` +
      `<b>${escapeHtml(role)}</b>.</p>` + button(url, "Accept the invite") +
      `<p>The invite works once, within ${escapeHtml(Math.round(hours / 24))} days.</p>`),
  }),
  notification: ({ appName, title, body, url, action }) => ({
    subject: title,
    text: `${body}${url ? `\n\n${url}` : ""}`,
    html: layout(appName, `<p><b>${escapeHtml(title)}</b></p><p>${escapeHtml(body).replace(/\n/g, "<br>")}</p>` +
      (url ? button(url, action || "Open") : "")),
  }),
};

export function render(templates, name, vars) {
  const make = templates[name];
  if (typeof make !== "function") throw new Error(`no email template named ${name}`);
  const out = make(vars);
  const subject = oneLine(out.subject);
  if (!subject || !out.text) throw new Error(`email template ${name} gave no subject or text`);
  return { subject, text: String(out.text), html: out.html ? String(out.html) : undefined };
}
