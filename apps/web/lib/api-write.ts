/**
 * ONE PRESS, ONE EFFECT: the only way the console changes anything.
 *
 * The same duplicate kept coming back on a different button. 2026-08-22 the operator and the
 * autopilot started one run twice; 2026-08-30 "Open preview happened twice"; 2026-09-10
 * duplicate chat actions; 2026-10-02 "Stop and run again" pressed twice started two workflows.
 * Each fix guarded one button (a ref here, a dedupe key there), and the next button had the same
 * hole. The API now honours an `Idempotency-Key` on every write (apps/api/app/idempotency.py);
 * this is the console's half, in one place, and `tests/every-write-goes-through-api-write.test.ts`
 * fails the build if a write goes around it.
 *
 * IN THE BROWSER, a call is one press:
 *   - it gets a fresh Idempotency-Key, so the API runs it at most once;
 *   - a second identical call while the first is in flight (the double click — `disabled` only
 *     lands after React re-renders) does NOT send anything: it gets the first call's response;
 *   - a DIFFERENT call in the same `action` scope while one is in flight is refused with
 *     WriteInFlightError, so one panel cannot fire two actions over each other.
 *
 * ON THE SERVER (route handlers, server actions, the typed client), the press happened in the
 * browser, so the key is DERIVED from it: the incoming request's Idempotency-Key, or the intent a
 * SubmitButton put in its form (lifted into `x-boss-intent` by middleware.ts). Each distinct API
 * call the handler makes gets `<intent>.<hash of method, path and body>`, so a repeated press
 * repeats the same keys and the API replays instead of acting twice, while the different calls
 * one press legitimately makes never collide. With no intent to derive from, nothing is added and
 * the request behaves exactly as before.
 */

export const IDEMPOTENCY_HEADER = "Idempotency-Key";
/** The form field SubmitButton renders, and the request header middleware.ts lifts it into. */
export const INTENT_FIELD = "__boss_intent";
export const INTENT_HEADER = "x-boss-intent";

export type WriteMethod = "POST" | "PUT" | "PATCH" | "DELETE";
export type WriteInit = Omit<RequestInit, "method"> & { method: WriteMethod };
export type WriteOptions = {
  /** Calls sharing an action scope are one-at-a-time; default: the exact request. */
  action?: string;
  /** Reuse a key the caller already holds for this intent (e.g. a retried press). */
  key?: string;
};

export class WriteInFlightError extends Error {
  constructor(action: string) {
    super(`Still working on the previous ${action} — it was not sent twice.`);
    this.name = "WriteInFlightError";
  }
}

// Per call, not at import: the same module is evaluated on both sides, and tests switch sides.
const isBrowser = () => typeof window !== "undefined";
const KEY_SHAPE = /^[\x21-\x7e]{1,128}$/;
const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function isWriteMethod(method: string | undefined): method is WriteMethod {
  return WRITE_METHODS.has((method ?? "GET").toUpperCase());
}

/** A UUIDv4. `crypto.randomUUID` exists only in secure contexts, and the console is opened over
 *  plain http on a LAN address often enough that relying on it would break every button there;
 *  `getRandomValues` works everywhere. */
export function newIntentKey(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

type Joiner = { resolve: (r: Response) => void; reject: (e: unknown) => void };
type Pending = { fingerprint: string; joiners: Joiner[] };
const inFlight = new Map<string, Pending>();
let opaqueBodies = 0;

function fingerprintOf(url: string, init: WriteInit): string {
  const body = init.body;
  // Only a string body can be compared. Two uploads to one URL are never "the same press".
  const shown = body == null ? "" : typeof body === "string" ? body : `#opaque-${++opaqueBodies}`;
  return `${init.method} ${url} ${shown}`;
}

/** Whether a write in this action scope is still running. For a panel that must refuse a second
 *  action synchronously, before its own state has re-rendered. */
export function writeInFlight(action: string): boolean {
  return inFlight.has(action);
}

export async function apiWrite(
  input: string | URL,
  init: WriteInit,
  options: WriteOptions = {},
): Promise<Response> {
  const url = String(input);
  const has = new Headers(init.headers).has(IDEMPOTENCY_HEADER);
  if (isBrowser()) {
    const fingerprint = fingerprintOf(url, init);
    const scope = options.action ?? fingerprint;
    const pending = inFlight.get(scope);
    if (pending) {
      if (pending.fingerprint !== fingerprint) throw new WriteInFlightError(scope);
      return new Promise<Response>((resolve, reject) => pending.joiners.push({ resolve, reject }));
    }
    const headers = has ? init.headers : withKey(init.headers, options.key ?? newIntentKey());
    // Registered before the first await: a second click in the same tick already sees it.
    const entry: Pending = { fingerprint, joiners: [] };
    inFlight.set(scope, entry);
    try {
      const response = await fetch(url, { ...init, headers });
      // Copies are cut BEFORE the first caller can read the body; with no joiner nothing is
      // copied, so a streamed reply is never buffered a second time.
      for (const joiner of entry.joiners) joiner.resolve(response.clone());
      return response;
    } catch (error) {
      for (const joiner of entry.joiners) joiner.reject(error);
      throw error;
    } finally {
      inFlight.delete(scope);
    }
  }
  if (has) return fetch(url, init);
  const intent = options.key ?? (await serverIntent());
  if (!intent) return fetch(url, init);
  const key = await deriveKey(intent, init.method, url, init.body);
  return fetch(url, { ...init, headers: withKey(init.headers, key) });
}

/** Add the key in the caller's own header shape, so a plain object stays a plain object. */
function withKey(headers: HeadersInit | undefined, key: string): HeadersInit {
  if (headers == null || (typeof headers === "object" && !Array.isArray(headers)
      && !(headers instanceof Headers))) {
    return { ...(headers as Record<string, string> | undefined), [IDEMPOTENCY_HEADER]: key };
  }
  const merged = new Headers(headers);
  merged.set(IDEMPOTENCY_HEADER, key);
  return merged;
}

/** The typed client's transport: reads pass straight through, writes go through apiWrite. */
export function writeAwareFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (init && isWriteMethod(init.method) && !(input instanceof Request)) {
    return apiWrite(input, { ...init, method: init.method.toUpperCase() as WriteMethod });
  }
  return fetch(input, init);
}

type IntentResolver = () => Promise<string | null | undefined>;

/** Installed once per server process by instrumentation.ts, which can import next/headers;
 *  this module cannot, because client components import it too. On globalThis because Next
 *  gives route handlers and pages separate copies of every module. */
export function installServerIntentResolver(resolver: IntentResolver): void {
  (globalThis as { __bossServerIntent?: IntentResolver }).__bossServerIntent = resolver;
}

async function serverIntent(): Promise<string | undefined> {
  const resolver = (globalThis as { __bossServerIntent?: IntentResolver }).__bossServerIntent;
  if (!resolver) return undefined;
  try {
    const intent = (await resolver())?.trim();
    return intent && KEY_SHAPE.test(intent) ? intent : undefined;
  } catch {
    return undefined; // outside a request (a background render): no press to derive from
  }
}

/** `<intent>.<32 hex>`: stable for the same call under the same press, distinct across calls. */
export async function deriveKey(
  intent: string, method: string, url: string, body: unknown,
): Promise<string> {
  let target = url;
  try {
    const parsed = new URL(url, "http://console.invalid");
    target = parsed.pathname + parsed.search;
  } catch { /* keep the raw string */ }
  const shown = typeof body === "string" ? body : body == null ? "" : `#${newIntentKey()}`;
  const data = new TextEncoder().encode(`${method.toUpperCase()}\n${target}\n${shown}`);
  const digest = await globalThis.crypto.subtle.digest("SHA-256", data);
  const hex = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  return `${intent}.${hex.slice(0, 32)}`;
}

/** The intent a server-action request carries, from its encoded form body. Kept here, beside
 *  the field name, so middleware.ts and SubmitButton cannot drift apart. React may prefix form
 *  field names (`1___boss_intent`), so this looks for the field name, not an exact key. */
const INTENT_IN_BODY = new RegExp(
  `${INTENT_FIELD}[\\s\\S]{0,120}?([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})`,
  "i",
);

export function intentFromActionBody(body: string): string | null {
  return INTENT_IN_BODY.exec(body)?.[1] ?? null;
}
