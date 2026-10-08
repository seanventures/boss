/**
 * A session row as the API sends it (S1: GET /v1/identity/me/sessions and, for admins,
 * /v1/identity/sessions?principal_id=) → the console's SessionRow. The API sends the raw
 * `user_agent` and `created_at`; the console shows a device, a client name and "started".
 * Tolerant: a row already in console shape (the dev stub) passes through.
 */
import type { SessionRow } from "./types";

type RawSession = Partial<SessionRow> & {
  id: string;
  user_agent?: string | null;
  created_at?: string | null;
  expires_at?: string | null;
  methods?: string[] | null;
};

export function deviceOf(ua: string): SessionRow["device"] {
  if (/iPad|Tablet/i.test(ua)) return "tablet";
  if (/iPhone|Android.*Mobile|Mobile Safari|Mobi/i.test(ua)) return "phone";
  return "desktop";
}

/** A browser and system from a User-Agent ("Chrome on macOS", "Safari on iOS", "BOSS CLI"): the ONE
 *  vocabulary for a session, used by My account › Sessions and an admin's view of a person alike. */
export function clientOf(ua: string): string {
  if (!ua) return "Unknown client";
  if (/boss/i.test(ua)) return "BOSS CLI";
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android" : /Mac OS X|Macintosh/.test(ua) ? "macOS"
    : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "";
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : /curl|python|node|Go-http/i.test(ua) ? "API client" : "Browser";
  return os ? `${browser} on ${os}` : browser;
}

export function toSessionRow(raw: RawSession): SessionRow {
  const ua = String(raw.user_agent ?? "");
  return {
    id: raw.id,
    current: Boolean(raw.current),
    device: raw.device ?? deviceOf(ua),
    client: raw.client ?? clientOf(ua),
    location: raw.location ?? null,
    ip: raw.ip ?? null,
    methods: Array.isArray(raw.methods) ? raw.methods : [],
    started_at: raw.started_at ?? raw.created_at ?? raw.last_seen_at ?? new Date(0).toISOString(),
    last_seen_at: raw.last_seen_at ?? raw.started_at ?? raw.created_at ?? new Date(0).toISOString(),
  };
}

export function toSessionRows(list: unknown): SessionRow[] {
  return Array.isArray(list) ? list.filter((x): x is RawSession => !!x && typeof x === "object" && "id" in x).map(toSessionRow) : [];
}
