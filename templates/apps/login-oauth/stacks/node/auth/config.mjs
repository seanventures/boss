// Configuration for the login-oauth template, read from the environment. Client ids and secrets
// are app secrets: BOSS puts them in the environment at run time from its vault. Never write one
// into code, a config file or a test.

const DEFAULTS = {
  APP_BASE_URL: "http://localhost:3000",
  GOOGLE_ISSUER: "https://accounts.google.com",
  GITHUB_URL: "https://github.com",
  GITHUB_API_URL: "https://api.github.com",
};

function list(value) {
  return String(value || "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

export function loadConfig(env = process.env) {
  const get = (name) => (env[name] ?? DEFAULTS[name] ?? "").toString().trim();
  const baseUrl = get("APP_BASE_URL").replace(/\/+$/, "");
  const sessionSecret = get("SESSION_SECRET");
  if (sessionSecret.length < 32) {
    throw new Error("SESSION_SECRET must be set to at least 32 characters (BOSS generates one)");
  }
  const providers = {};
  if (get("GOOGLE_CLIENT_ID")) {
    providers.google = {
      kind: "oidc",
      name: "Google",
      issuer: get("GOOGLE_ISSUER"),
      clientId: get("GOOGLE_CLIENT_ID"),
      clientSecret: get("GOOGLE_CLIENT_SECRET"),
      scope: "openid email profile",
    };
  }
  if (get("GITHUB_CLIENT_ID")) {
    const web = get("GITHUB_URL").replace(/\/+$/, "");
    providers.github = {
      kind: "oauth2",
      name: "GitHub",
      issuer: web,
      authorizationEndpoint: `${web}/login/oauth/authorize`,
      tokenEndpoint: `${web}/login/oauth/access_token`,
      apiUrl: get("GITHUB_API_URL").replace(/\/+$/, ""),
      clientId: get("GITHUB_CLIENT_ID"),
      clientSecret: get("GITHUB_CLIENT_SECRET"),
      scope: "read:user user:email",
    };
  }
  // "Sign in with BOSS": any OIDC issuer with discovery. BOSS fills these when the app is
  // registered as a client of its OIDC provider.
  if (get("OIDC_BOSS_ISSUER") && get("OIDC_BOSS_CLIENT_ID")) {
    providers.boss = {
      kind: "oidc",
      name: "BOSS",
      issuer: get("OIDC_BOSS_ISSUER"),
      clientId: get("OIDC_BOSS_CLIENT_ID"),
      clientSecret: get("OIDC_BOSS_CLIENT_SECRET"),
      scope: "openid email profile",
    };
  }
  return {
    baseUrl,
    secureCookies: baseUrl.startsWith("https://"),
    sessionSecret,
    adminEmails: list(get("AUTH_ADMIN_EMAILS")),
    providers,
  };
}
