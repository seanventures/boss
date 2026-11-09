"use client";

/**
 * ADD A SERVER (docs/product/wireframes/agentic-factory/3-add-a-server.html): the reads and the
 * one write the sheet and the server page make. The engine is services/add_server.py; every
 * write goes through apiWrite (infra-actions `send`), so a stale second factor opens "Confirm
 * it's you" and a cancel is said in words.
 */
import { arr, num, obj, str, usePolled, type Polled } from "./factory-floor";
import { apiWrite } from "./api-write";
import { declareAction } from "./identity/guarded-action";
import { send, type Result } from "../app/infra/infra-actions";

export type ServerKind = "ssh" | "dials_in" | "cloud";
export type Role = "staging" | "prod" | "builds";
export type AccessMode = "boss_key" | "my_key" | "stored_key";

export const KIND_WORDS: Record<ServerKind, string> = { ssh: "SSH server", dials_in: "It dials in", cloud: "Cloud account" };
export const ROLE_WORDS: Record<Role, string> = { staging: "Staging pool", prod: "Production", builds: "Builds" };
export const ROLE_LINE: Record<Role, string> = {
  staging: "any project can deploy staging here; no app is moved",
  prod: "a production server; each prod deploy still asks a person",
  builds: "builds and tests run here; no app is moved",
};
export const STORES: { key: string; label: string; tip: string }[] = [
  { key: "database", label: "Database", tip: "database (survives restarts)" },
  { key: "file", label: "File", tip: "file (local disk — lost on redeploy)" },
  { key: "platform_secret_store", label: "Secret store", tip: "platform_secret_store" },
];

export type Probe = { address: string; port: number; portOpen: boolean; hostKey: string | null; hostKeyType: string | null;
  web: Record<string, string>; said: string };
export type StoredKey = { id: string; name: string; fingerprint: string | null };
export type ServerRead = {
  kind: ServerKind; address: string | null; user: string | null; userDetected: boolean; port: number; identityFile: string | null;
  role: Role; name: string; words: string; probe: Probe | null; provider: string | null; storedKeys: StoredKey[];
  plan: string[]; outwardSsh: boolean; hostWrites: boolean; hostWritesWords: string | null;
};

const asRole = (v: unknown): Role => (v === "prod" || v === "builds" ? v : "staging");
const asKind = (v: unknown): ServerKind => (v === "dials_in" || v === "cloud" ? v : "ssh");

export function parseRead(w: unknown): ServerRead {
  const o = obj(w);
  const p = o.probe ? obj(o.probe) : null;
  const pre = obj(o.preconditions);
  return {
    kind: asKind(o.kind), address: o.address ? str(o.address) : null, user: o.user ? str(o.user) : null,
    userDetected: !!o.user_detected, port: num(o.port, 22), identityFile: o.identity_file ? str(o.identity_file) : null,
    role: asRole(o.role), name: str(o.name), words: str(o.words),
    probe: p ? { address: str(p.address), port: num(p.port, 22), portOpen: !!p.port_open, hostKey: p.host_key ? str(p.host_key) : null,
                 hostKeyType: p.host_key_type ? str(p.host_key_type) : null,
                 web: Object.fromEntries(Object.entries(obj(p.web)).map(([k, v]) => [k, str(v)])), said: str(p.said) } : null,
    provider: o.provider ? str(o.provider) : null,
    storedKeys: arr(o.stored_keys).map((k) => { const x = obj(k); return { id: str(x.id), name: str(x.name), fingerprint: x.fingerprint ? str(x.fingerprint) : null }; }),
    plan: arr(o.plan).map((x) => str(x)), outwardSsh: pre.outward_ssh !== false, hostWrites: !!pre.host_writes,
    hostWritesWords: pre.host_writes_words ? str(pre.host_writes_words) : null,
  };
}

/** The sheet's read: words + the network probe. Creates nothing (no step-up). */
export async function readServer(projectId: string, text: string, kind: ServerKind | null, signal?: AbortSignal): Promise<Result<ServerRead>> {
  try {
    const r = await apiWrite(`/v1/projects/${encodeURIComponent(projectId)}/servers/read`, {
      method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" }, signal,
      body: JSON.stringify({ text, kind, probe: true }),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) {
      const b = obj(data);
      return { ok: false, status: r.status, error: str(b.message || b.detail, `The API answered ${r.status}.`) };
    }
    return { ok: true, data: parseRead(data) };
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") return { ok: false, error: "", status: 0 };
    return { ok: false, error: e instanceof Error ? e.message : "The API did not answer." };
  }
}

export type CreateSpec = {
  project_id: string; actor: string; address: string; ssh_user: string; ssh_port: number; name: string;
  arch?: string; admin_identity_file?: string; role: Role; port_min?: number; port_max?: number; max_apps?: number;
  access: { mode: AccessMode; backend: string; private_key?: string; secret_ref_id?: string };
  setup: { docker: boolean; exporter: boolean; caddy: boolean };
  watch: { host_health: boolean; disk_pct: number; prune_cache: boolean };
  probe?: Record<string, unknown>;
};

/** Add and set up: one press, gated by host:manage (step-up). */
export const createServer = (spec: CreateSpec) =>
  send<{ host_id?: string; name?: string; href?: string }>("POST", "/v1/servers", spec as unknown as Record<string, unknown>,
    { scope: "infra-add-server", label: "Add and set up" });
declareAction(createServer, "host:manage");

// ── the server page's setup ──────────────────────────────────────────────────────────────────

export type StepState = "done" | "now" | "blocked" | "todo";
export type SetupStep = { key: string; label: string; state: StepState; detail: string | null };
export type SetupView = {
  hostId: string; name: string; address: string; user: string; role: Role; roleWords: string; roleLine: string;
  access: AccessMode; phase: string; state: string; blocked: string | null; steps: SetupStep[];
  now: { text: string; tone: string }; ready: boolean;
  gate: { user: string; address: string; line: string; fromLaptop: string | null; cloudInit: string | null } | null;
  stats: { memory: string | null; cpus: number | null; arch: string | null; os: string | null; diskFree: string | null;
           diskPct: number | null; alertPct: number; freePorts: number | null; portRange: string; docker: string | null };
  log: { at: string; who: string; line: string }[];
};

export function parseSetup(w: unknown): SetupView {
  const o = obj(w);
  const g = o.gate ? obj(o.gate) : null;
  const s = obj(o.stats);
  const n = (v: unknown) => (typeof v === "number" ? v : null);
  return {
    hostId: str(o.host_id), name: str(o.name), address: str(o.address), user: str(o.user), role: asRole(o.role),
    roleWords: str(o.role_words), roleLine: str(o.role_line), access: (str(o.access) || "boss_key") as AccessMode,
    phase: str(o.phase), state: str(o.state), blocked: o.blocked ? str(o.blocked) : null,
    steps: arr(o.steps).map((x) => { const st = obj(x); return { key: str(st.key), label: str(st.label), state: (str(st.state) || "todo") as StepState, detail: st.detail ? str(st.detail) : null }; }),
    now: { text: str(obj(o.now).text), tone: str(obj(o.now).tone) }, ready: !!o.ready,
    gate: g ? { user: str(g.user), address: str(g.address), line: str(g.line), fromLaptop: g.from_laptop ? str(g.from_laptop) : null,
                cloudInit: g.cloud_init ? str(g.cloud_init) : null } : null,
    stats: { memory: s.memory ? str(s.memory) : null, cpus: n(s.cpus), arch: s.arch ? str(s.arch) : null, os: s.os ? str(s.os) : null,
             diskFree: s.disk_free ? str(s.disk_free) : null, diskPct: n(s.disk_pct), alertPct: num(s.alert_pct, 80),
             freePorts: n(s.free_ports), portRange: str(s.port_range), docker: s.docker ? str(s.docker) : null },
    log: arr(o.log).map((x) => { const l = obj(x); return { at: str(l.at), who: str(l.who), line: str(l.line) }; }),
  };
}

/** Every 3 s while the agent works (so the key landing is SEEN), 30 s once it watches. */
export const useServerSetup = (hostId: string | null): Polled<SetupView> =>
  usePolled(hostId ? `/v1/servers/${encodeURIComponent(hostId)}/setup` : null, parseSetup,
            (d) => (d && d.ready ? 30_000 : 3_000));

/** What the one input reads as, instantly (the server's read confirms it). */
export function looksLike(text: string): { address: string | null; user: string | null } {
  const t = text.trim();
  const m = t.match(/(?:^|\s)(?:ssh\s+(?:-\w\s+\S+\s+)*)?(?:([a-z_][\w.-]*)@)?((?:\d{1,3}\.){3}\d{1,3}|[a-z0-9-]+(?:\.[a-z0-9-]+)+)(?=$|[\s:,])/i);
  return m ? { address: m[2], user: m[1] ?? null } : { address: null, user: null };
}
