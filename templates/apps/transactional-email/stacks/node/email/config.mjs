// Configuration for the transactional-email connector, read from the environment. SMTP_PASSWORD
// and RESEND_API_KEY are app secrets: BOSS puts them in the environment at run time from its
// vault. Never write one into code, a config file or a test.
//
// Which way mail goes:
//   EMAIL_PROVIDER unset   -> resend when RESEND_API_KEY is set, else smtp when SMTP_HOST is set,
//                             else the local MAIL CATCHER (builds, previews, development)
//   EMAIL_PROVIDER=smtp | resend | catcher -> that one, and its settings must be present
// The catcher never runs in production: there, with no provider, email is OFF and every send
// fails visibly (`email_not_configured`), so a password reset can never land in a page that
// anyone on the internet could read.

export class EmailConfigError extends Error {}

//: Resend's API host is FIXED; it is never read from configuration (a test passes its fake's
//: address to createEmail directly, as an option, not through the environment).
export const RESEND_API = "https://api.resend.com";

const PROVIDERS = ["smtp", "resend", "catcher"];
const TLS_MODES = ["starttls", "implicit", "none"];

const DEFAULTS = {
  APP_BASE_URL: "http://localhost:3000",
  APP_NAME: "the app",
  APP_ENV: "development",
  SMTP_PORT: "587",
  SMTP_TLS: "starttls",
  EMAIL_RESET_PATH: "/reset-password",
  EMAIL_INVITE_PATH: "/accept-invite",
  EMAIL_RESET_TTL_MINUTES: "30",
  EMAIL_INVITE_TTL_HOURS: "168",
  EMAIL_MAX_ATTEMPTS: "3",
};

export function isProduction(env = process.env) {
  const nodeEnv = String(env.NODE_ENV || "").trim().toLowerCase();
  const appEnv = String(env.APP_ENV || "").trim().toLowerCase();
  return nodeEnv === "production" || appEnv === "production";
}

function positiveInt(value, name) {
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) throw new EmailConfigError(`${name} must be a positive whole number`);
  return n;
}

function localPath(value, name) {
  if (!/^\/[A-Za-z0-9/_-]*$/.test(value)) throw new EmailConfigError(`${name} must be a path on this app, like /reset-password`);
  return value;
}

export function loadEmailConfig(env = process.env) {
  const get = (name) => (env[name] ?? DEFAULTS[name] ?? "").toString().trim();
  const production = isProduction(env);
  const baseUrl = get("APP_BASE_URL").replace(/\/+$/, "");
  if (!/^https?:\/\/[^\s/]+/.test(baseUrl)) throw new EmailConfigError("APP_BASE_URL must be an http(s) address");

  const smtp = {
    host: get("SMTP_HOST"),
    port: positiveInt(get("SMTP_PORT"), "SMTP_PORT"),
    user: get("SMTP_USER"),
    password: get("SMTP_PASSWORD"),
    tls: get("SMTP_TLS").toLowerCase(),
  };
  if (!TLS_MODES.includes(smtp.tls)) throw new EmailConfigError(`SMTP_TLS must be one of ${TLS_MODES.join(", ")}`);
  const resend = { apiKey: get("RESEND_API_KEY") };

  let provider = get("EMAIL_PROVIDER").toLowerCase();
  if (provider && !PROVIDERS.includes(provider)) {
    throw new EmailConfigError(`EMAIL_PROVIDER must be one of ${PROVIDERS.join(", ")} (or unset)`);
  }
  if (!provider) provider = resend.apiKey ? "resend" : smtp.host ? "smtp" : production ? "off" : "catcher";
  // Name the variable, never echo a value.
  if (provider === "resend" && !resend.apiKey) throw new EmailConfigError("EMAIL_PROVIDER=resend needs RESEND_API_KEY");
  if (provider === "smtp" && !smtp.host) throw new EmailConfigError("EMAIL_PROVIDER=smtp needs SMTP_HOST");
  if (provider === "smtp" && smtp.user && !smtp.password) throw new EmailConfigError("SMTP_USER is set, but SMTP_PASSWORD is not set");
  if (production && provider === "catcher") {
    throw new EmailConfigError("EMAIL_PROVIDER=catcher is for builds and previews; in production set SMTP or Resend");
  }
  if (production && provider === "smtp" && smtp.tls === "none") {
    throw new EmailConfigError("SMTP_TLS=none sends passwords and reset links in clear text; production needs starttls or implicit");
  }

  const appName = get("APP_NAME");
  const host = new URL(baseUrl).hostname;
  const from = get("EMAIL_FROM") || `${appName.replace(/[<>"\r\n]/g, "")} <no-reply@${host}>`;
  return {
    provider,
    production,
    baseUrl,
    appName,
    from,
    replyTo: get("EMAIL_REPLY_TO"),
    smtp,
    resend,
    resetPath: localPath(get("EMAIL_RESET_PATH"), "EMAIL_RESET_PATH"),
    invitePath: localPath(get("EMAIL_INVITE_PATH"), "EMAIL_INVITE_PATH"),
    resetTtlMs: positiveInt(get("EMAIL_RESET_TTL_MINUTES"), "EMAIL_RESET_TTL_MINUTES") * 60_000,
    inviteTtlMs: positiveInt(get("EMAIL_INVITE_TTL_HOURS"), "EMAIL_INVITE_TTL_HOURS") * 3_600_000,
    maxAttempts: positiveInt(get("EMAIL_MAX_ATTEMPTS"), "EMAIL_MAX_ATTEMPTS"),
  };
}
