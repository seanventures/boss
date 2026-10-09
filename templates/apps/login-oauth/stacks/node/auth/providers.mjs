// One openid-client Configuration per provider, discovered on first use and cached (the app
// starts without reaching any provider). Google and BOSS are OpenID Connect with discovery;
// GitHub is plain OAuth 2.0 with fixed endpoints and its REST API for the profile.
import * as oidc from "openid-client";

// Plain http is accepted only for a provider on this machine (the tests' fake provider, a
// local BOSS). Anything else must be https.
function isLocal(url) {
  const { protocol, hostname } = new URL(url);
  return protocol === "http:" && ["localhost", "127.0.0.1", "[::1]", "::1"].includes(hostname);
}

export class Providers {
  constructor(config) {
    this.config = config;
    this.cache = new Map();
  }

  has(id) {
    return Object.hasOwn(this.config.providers, id);
  }

  list() {
    return Object.entries(this.config.providers).map(([id, p]) => ({
      id,
      name: p.name,
      login_url: `/auth/login/${id}`,
    }));
  }

  spec(id) {
    return this.config.providers[id];
  }

  async get(id) {
    if (this.cache.has(id)) return this.cache.get(id);
    const p = this.spec(id);
    const local = isLocal(p.issuer);
    let configuration;
    if (p.kind === "oidc") {
      configuration = await oidc.discovery(
        new URL(p.issuer),
        p.clientId,
        p.clientSecret,
        undefined,
        local ? { execute: [oidc.allowInsecureRequests] } : undefined,
      );
    } else {
      configuration = new oidc.Configuration(
        {
          issuer: p.issuer,
          authorization_endpoint: p.authorizationEndpoint,
          token_endpoint: p.tokenEndpoint,
        },
        p.clientId,
        p.clientSecret,
      );
      if (local) oidc.allowInsecureRequests(configuration);
    }
    this.cache.set(id, configuration);
    return configuration;
  }

  // The signed-in person's profile, normalised: {subject, email, emailVerified, name, avatar}.
  async profile(id, tokens) {
    const p = this.spec(id);
    if (p.kind === "oidc") {
      const claims = tokens.claims();
      return {
        subject: String(claims.sub),
        email: claims.email || null,
        emailVerified: claims.email_verified === true,
        name: claims.name || null,
        avatar: claims.picture || null,
      };
    }
    // GitHub: the profile, then the primary email and whether GitHub has verified it.
    const headers = {
      Authorization: `Bearer ${tokens.access_token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "boss-login-oauth",
    };
    const userRes = await fetch(`${p.apiUrl}/user`, { headers });
    if (!userRes.ok) throw new Error(`github /user answered ${userRes.status}`);
    const user = await userRes.json();
    const emailsRes = await fetch(`${p.apiUrl}/user/emails`, { headers });
    const emails = emailsRes.ok ? await emailsRes.json() : [];
    const primary = emails.find((e) => e.primary) || null;
    return {
      subject: String(user.id),
      email: primary ? primary.email : null,
      emailVerified: Boolean(primary && primary.verified),
      name: user.name || user.login || null,
      avatar: user.avatar_url || null,
    };
  }
}
