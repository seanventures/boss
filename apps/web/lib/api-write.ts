/**
 * ONE PRESS, ONE EFFECT: how every console write carries its press. A fetch write goes through
 * `apiWrite` here; a server action goes through a SubmitButton form or `callAction`
 * (lib/call-action.ts), which share this module's in-flight registry. Those are the only doors.
 *
 * The same duplicate kept coming back on a different button. 2026-08-22 the operator and the
 * autopilot started one run twice; 2026-08-30 "Open preview happened twice"; 2026-09-10
 * duplicate chat actions; 2026-10-02 "Stop and run again" pressed twice started two workflows.
 * Each fix guarded one button (a ref here, a dedupe key there), and the next button had the same
 * hole. The API now honours an `Idempotency-Key` on every write (apps/api/app/idempotency.py);
 * this is the console's half, in one place, and `tests/every-write-goes-through-api-write.test.ts`
 * fails the build if a fetch write or a server action goes around it.
 *
 * IN THE BROWSER, a call is one press:
 *   - it gets a fresh Idempotency-Key, so the API runs it at most once;
 *   - a second identical call while the first is in flight (the double click — `disabled` only
 *     lands after React re-renders) does NOT send anything: it gets the first call's response;
 *   - a call made as an operator's PRESS (`press: { scope, label }`) occupies its scope while it
 *     runs, and a DIFFERENT press in the same scope is refused — and the refusal is SAID, in the
 *     operator's words, naming what is still running ("Retry was not sent — Stop is still in
 *     progress"), by this module, so no button can drop a press silently;
 *   - a call made WITHOUT a press (an automatic recheck, a poll that writes) takes no scope: it
 *     can never block a press, and a press can never swallow it. The first version of this put
 *     the delivery panel's automatic recheck in the press scope, so of several stopped runs only
 *     the first was put back and every Retry pressed meanwhile vanished without a word.
 *
 * A DUPLICATE WAITS FOR THE ORIGINAL'S ANSWER. If the API says the key is still running (409
 * idempotency_key_in_progress — a second tab, a resubmitted form), the call waits and asks again
 * with the same key until the first request's stored answer is replayed. A duplicate of a press
 * that is working must never show the operator an error that makes them press again.
 *
 * Server actions invoked from code (not from a SubmitButton form) go through
 * `callAction` in lib/call-action.ts, which uses the same press registry below.
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
/** An operator's press: `scope` is what one operator acts on one-at-a-time (a panel, a run),
 *  `label` is the button's own words, used when a second press has to be refused. */
export type Press = { scope: string; label: string };
export type WriteOptions = {
  /** Set ONLY for an operator's press. Background writes leave it out and take no scope. */
  press?: Press;
  /** Reuse a key the caller already holds for this intent (e.g. a retried press). */
  key?: string;
};

/** The one sentence a refused press is told with, everywhere in the console. */
export function refusalSentence(attempted: string, running: string): string {
  const what = attempted === running ? `the previous ${running}` : running;
  return `${attempted} was not sent — ${what} is still in progress. `
    + "Try again when it finishes.";
}

export class WriteInFlightError extends Error {
  readonly attempted: string;
  readonly running: string;
  constructor(attempted: string, running: string) {
    super(refusalSentence(attempted, running));
    this.name = "WriteInFlightError";
    this.attempted = attempted;
    this.running = running;
  }
}

/** The console-wide toast (app/client-forms.tsx `Toaster`, mounted in the layout). Dispatched
 *  here rather than imported so this module stays importable by middleware and server code. */
export const TOAST_EVENT = "boss:toast";

function announce(message: string): void {
  try {
    window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type: "error", message } }));
  } catch { /* no window: nothing to show it on */ }
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

type Joiner<T> = { resolve: (value: T) => void; reject: (e: unknown) => void };
type Pending = { fingerprint: string; label: string; joiners: Joiner<unknown>[] };
const inFlight = new Map<string, Pending>();
let opaqueBodies = 0;

function fingerprintOf(url: string, init: WriteInit): string {
  const body = init.body;
  // Only a string body can be compared. Two uploads to one URL are never "the same press".
  const shown = body == null ? "" : typeof body === "string" ? body : `#opaque-${++opaqueBodies}`;
  return `${init.method} ${url} ${shown}`;
}

/** The label of the press running in this scope, or null. For a panel that wants to show the
 *  state before it is asked; refusing is done by runPress itself. */
export function pressInFlight(scope: string): string | null {
  return inFlight.get(`press:${scope}`)?.label ?? null;
}

/**
 * THE ONE REGISTRY OF WHAT IS IN FLIGHT, for fetch writes and server actions alike.
 *
 * `fingerprint` says what "the same call" is: an identical call joins the running one and gets
 * its result (through `share`, so a Response is cloned before the first caller reads it). With a
 * `press`, the press's scope is held; a different call there is refused, announced, and rejected
 * with WriteInFlightError. Without one, only the identical call is held. Registered before the
 * first await, so a second click in the same tick already sees it.
 */
export function runPress<T>(
  spec: { press?: Press; fingerprint: string; key?: string; share?: (value: T) => T },
  send: (key: string) => Promise<T>,
): Promise<T> {
  const slot = spec.press ? `press:${spec.press.scope}` : `call:${spec.fingerprint}`;
  const pending = inFlight.get(slot);
  if (pending) {
    if (pending.fingerprint === spec.fingerprint) {
      return new Promise<T>((resolve, reject) =>
        pending.joiners.push({ resolve: resolve as (v: unknown) => void, reject }));
    }
    const refused = new WriteInFlightError(spec.press?.label ?? "This", pending.label);
    announce(refused.message);
    return Promise.reject(refused);
  }
  const entry: Pending = { fingerprint: spec.fingerprint, label: spec.press?.label ?? "", joiners: [] };
  inFlight.set(slot, entry);
  return (async () => {
    try {
      const value = await send(spec.key ?? newIntentKey());
      for (const joiner of entry.joiners) joiner.resolve(spec.share ? spec.share(value) : value);
      return value;
    } catch (error) {
      for (const joiner of entry.joiners) joiner.reject(error);
      throw error;
    } finally {
      inFlight.delete(slot);
    }
  })();
}

export async function apiWrite(
  input: string | URL,
  init: WriteInit,
  options: WriteOptions = {},
): Promise<Response> {
  const url = String(input);
  const has = new Headers(init.headers).has(IDEMPOTENCY_HEADER);
  if (isBrowser()) {
    return runPress<Response>({
      press: options.press,
      fingerprint: fingerprintOf(url, init),
      key: options.key,
      // Copies are cut BEFORE the first caller can read the body; with no joiner nothing is
      // copied, so a streamed reply is never buffered a second time.
      share: (response) => response.clone(),
    }, (key) => sendKeyed(url, has ? init : { ...init, headers: withKey(init.headers, key) }));
  }
  if (has) return sendKeyed(url, init);
  const intent = options.key ?? (await serverIntent());
  if (!intent) return fetch(url, init);
  const key = await deriveKey(intent, init.method, url, init.body);
  return sendKeyed(url, { ...init, headers: withKey(init.headers, key) });
}

/** How long a duplicate waits for the original press's answer before giving up and showing the
 *  API's own 409. Longer than any ordinary write; an agent turn can run for minutes. */
export const IN_PROGRESS_WAIT_MS = 5 * 60_000;

/** Send a keyed write; while the API says the same key is still running, wait and ask again. */
async function sendKeyed(url: string, init: WriteInit): Promise<Response> {
  const started = Date.now();
  let delay = 250;
  for (;;) {
    const response = await fetch(url, init);
    if (response.status !== 409 || Date.now() - started > IN_PROGRESS_WAIT_MS
        || typeof init.body === "object" && init.body !== null) {
      // An opaque body (a stream, a FormData upload) cannot be sent a second time.
      return response;
    }
    const detail = await response.clone().json().then(
      (body: { detail?: unknown }) => String(body?.detail ?? ""), () => "");
    if (!detail.startsWith("idempotency_key_in_progress")) return response;
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 2000);
  }
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
