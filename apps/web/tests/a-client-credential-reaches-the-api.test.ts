/**
 * A PASS-THROUGH WHOSE CALLER BRINGS ITS OWN CREDENTIAL MUST DELIVER IT (BOSSAPP-948).
 *
 * `git clone http://localhost:3000/git/<p>.git` with a valid BOSS token got 401: app/git's route
 * copied git's Authorization into its upstream call, but the call went out on the global fetch,
 * which lib/client.ts wraps for every call to BOSS_API_URL — and the identity wrapper
 * (lib/identity/server-fetch.ts) strips Authorization by design. The route's own tests would have
 * passed with a mocked fetch; the header was lost in the real wrappers, so this test runs the real
 * route modules with the real wrappers installed (by importing lib/client.ts with BOSS_API_URL set)
 * against a real HTTP server standing in for the API, and checks what arrived on the socket.
 *
 * The class: every module that forwards a request's own Authorization calls lib/direct-fetch.ts,
 * never the global fetch. The static check finds any new one that forgets.
 */
import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

type Seen = { method: string; url: string; headers: IncomingHttpHeaders; body: string };
const seen: Seen[] = [];
let server: Server;
let apiUrl = "";

const BASIC = `Basic ${Buffer.from("boss:boss_pat_test-token").toString("base64")}`;

beforeAll(async () => {
  server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      seen.push({ method: req.method ?? "", url: req.url ?? "", headers: req.headers, body: Buffer.concat(chunks).toString() });
      const authed = Boolean(req.headers.authorization);
      res.writeHead(authed ? 200 : 401, { "content-type": "application/x-git-upload-pack-advertisement" });
      res.end(authed ? "ok" : "unauthenticated");
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const addr = server.address();
  apiUrl = `http://127.0.0.1:${typeof addr === "object" && addr ? addr.port : 0}`;
  vi.stubEnv("BOSS_API_URL", apiUrl);
  vi.resetModules();
  // The wrappers, exactly as every server module gets them.
  const client = await import("../lib/client");
  expect(client.API_URL).toBe(apiUrl);
  const g = globalThis as unknown as { __bossIdentityFetch?: true; __bossFeedCache?: true };
  expect(g.__bossIdentityFetch && g.__bossFeedCache).toBe(true);
});

afterAll(async () => {
  vi.unstubAllEnvs();
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

function last(): Seen {
  const s = seen.at(-1);
  if (!s) throw new Error("the API was never called");
  return s;
}

describe("a client's own credential reaches the API through the console", () => {
  it("the installed wrappers strip Authorization from a plain fetch (why the pass-throughs must not use it)", async () => {
    await fetch(`${apiUrl}/git/p.git/info/refs`, { headers: { authorization: BASIC } });
    expect(last().headers.authorization).toBeUndefined();
  });

  it("git: info/refs carries git's Basic token and the host git named", async () => {
    const { GET } = await import("../app/git/[...path]/route");
    const req = new Request("http://localhost:3000/git/helpdesk-laptop.git/info/refs?service=git-upload-pack", {
      headers: { authorization: BASIC, "git-protocol": "version=2", "user-agent": "git/2.47.0" },
    });
    const res = await GET(req, { params: Promise.resolve({ path: ["helpdesk-laptop.git", "info", "refs"] }) });
    expect(res.status).toBe(200);
    const s = last();
    expect(s.url).toBe("/git/helpdesk-laptop.git/info/refs?service=git-upload-pack");
    expect(s.headers.authorization).toBe(BASIC);
    expect(s.headers["x-forwarded-host"]).toBe("localhost:3000");
    expect(s.headers["git-protocol"]).toBe("version=2");
  });

  it("git: a pack POST streams up with the token", async () => {
    const { POST } = await import("../app/git/[...path]/route");
    const body = "0032want 0123456789012345678901234567890123456789\n00000009done\n";
    const req = new Request("http://localhost:3000/git/helpdesk-laptop.git/git-upload-pack", {
      method: "POST", body,
      headers: { authorization: BASIC, "content-type": "application/x-git-upload-pack-request" },
    });
    const res = await POST(req, { params: Promise.resolve({ path: ["helpdesk-laptop.git", "git-upload-pack"] }) });
    expect(res.status).toBe(200);
    expect(last().headers.authorization).toBe(BASIC);
    expect(last().body).toBe(body);
  });

  it("SCIM: the IdP's bearer arrives", async () => {
    const { GET } = await import("../app/scim/v2/[...path]/route");
    const res = await GET(new Request("http://localhost:3000/scim/v2/Users", { headers: { authorization: "Bearer scim-key" } }));
    expect(res.status).toBe(200);
    expect(last().headers.authorization).toBe("Bearer scim-key");
  });

  it("OIDC: an app's client credential at /token arrives", async () => {
    const { POST } = await import("../app/oidc/[...path]/route");
    const res = await POST(new Request("http://localhost:3000/oidc/pool_1/token", {
      method: "POST", body: "grant_type=authorization_code",
      headers: { authorization: "Basic Y2xpZW50OnNlY3JldA==", "content-type": "application/x-www-form-urlencoded" },
    }));
    expect(res.status).toBe(200);
    expect(last().headers.authorization).toBe("Basic Y2xpZW50OnNlY3JldA==");
  });

  it("monitoring: a source's token on an ingest post arrives", async () => {
    const { forwardMonitoring } = await import("../lib/monitoring-proxy");
    const res = await forwardMonitoring(new Request("http://localhost:3000/v1/ingest/src_1/alertmanager", {
      method: "POST", body: "{}", headers: { authorization: "Bearer src-token", "content-type": "application/json" },
    }), { apiUrl });
    expect(res.status).toBe(200);
    expect(last().headers.authorization).toBe("Bearer src-token");
  });
});

/** Source with comments removed, so a word in prose is not read as a call. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

function sources(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (name === "node_modules" || name === ".next") continue;
    if (statSync(p).isDirectory()) sources(p, out);
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

describe("every pass-through of a client's Authorization uses the platform fetch", () => {
  const root = join(__dirname, "..");
  const forwarders = [...sources(join(root, "app")), ...sources(join(root, "lib"))]
    .map((file) => ({ file: relative(root, file), text: code(readFileSync(file, "utf8")) }))
    // Copies a request's Authorization: a lower-case header name it reads or lists to forward.
    .filter(({ text }) => /(?:\.get\(\s*|\[[^\]]*)["']authorization["']/i.test(text) && /fetch/i.test(text));

  it("finds the known forwarders (the scan still sees them)", () => {
    const files = forwarders.map((f) => f.file).sort();
    for (const f of ["app/git/[...path]/route.ts", "lib/identity/protocol-proxy.ts", "lib/monitoring-proxy.ts", "lib/oidc-proxy.ts"]) {
      expect(files).toContain(f);
    }
  });

  it("none of them calls the global fetch", () => {
    const offenders = forwarders.filter(({ text }) => /(?:^|[^\w.$])fetch\s*\(/m.test(text)).map((f) => f.file);
    expect(offenders, "forward the client's credential with lib/direct-fetch.ts directFetch").toEqual([]);
  });
});
