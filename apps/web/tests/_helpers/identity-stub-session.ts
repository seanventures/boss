/** Sessions against the identity stub API, for tests (same calls the console makes). */
import { SESSION_COOKIE } from "../../lib/identity/forward";
import { stubHandle, type StubRequest } from "../../lib/identity/stub/server";

export function req(method: string, path: string, opts: { body?: unknown; cookies?: Record<string, string>; csrf?: string } = {}): StubRequest {
  const u = new URL(`http://x${path}`);
  return { method, path: u.pathname, query: u.searchParams, body: opts.body ?? null, cookies: opts.cookies ?? {},
    headers: new Headers(opts.csrf ? { "x-boss-csrf": opts.csrf } : {}) };
}

export function cookieFrom(setCookies: string[] | undefined, name: string): string | undefined {
  const c = (setCookies ?? []).find((x) => x.startsWith(`${name}=`));
  return c ? c.slice(name.length + 1).split(";")[0] : undefined;
}

/** Sign a fixture person in through the stub (password, then the stub passkey when MFA is on). */
export function signIn(email: string): { cookies: Record<string, string>; csrf: string } {
  const login = stubHandle(req("POST", "/v1/auth/login", { body: { email, password: "pw" } }));
  let session = cookieFrom(login.setCookies, SESSION_COOKIE);
  if (!session) {
    const pre = cookieFrom(login.setCookies, "__Host-boss_preauth")!;
    const mfa = stubHandle(req("POST", "/v1/auth/mfa/totp", { body: { code: "123456" }, cookies: { "__Host-boss_preauth": pre } }));
    session = cookieFrom(mfa.setCookies, SESSION_COOKIE)
      ?? cookieFrom(stubHandle(req("POST", "/v1/auth/webauthn/verify", { body: { purpose: "mfa", credential: { stub: true } }, cookies: { "__Host-boss_preauth": pre } })).setCookies, SESSION_COOKIE);
  }
  const cookies = { [SESSION_COOKIE]: session! };
  const me = stubHandle(req("GET", "/v1/identity/me", { cookies })).body as { csrf_token: string };
  return { cookies, csrf: me.csrf_token };
}

