/**
 * Invoice import and seat price posted from the reconciliation card.
 * Providers are only the three the server accepts. A refusal happens before fetch.
 */

export const INVOICE_PROVIDERS = ["anthropic", "openai", "openrouter"] as const;
export type InvoiceProvider = (typeof INVOICE_PROVIDERS)[number];

const PROVIDER_REFUSAL = "Use Anthropic, OpenAI or OpenRouter.";

export type JsonFetch = (url: string, init?: RequestInit) => Promise<Response>;

export type PostResult =
  | { ok: true; added?: number; skipped_existing?: number }
  | { ok: false; error: string };

export function monthlyMicros(dollars: string): number | null {
  const text = dollars.trim();
  if (!text) return null;
  const n = Number(text);
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.round(n * 1_000_000);
}

export function invoiceProvider(value: string): InvoiceProvider | null {
  const name = value.trim().toLowerCase();
  return (INVOICE_PROVIDERS as readonly string[]).includes(name) ? name as InvoiceProvider : null;
}

/** A refusal string, or null when the provider may be posted. Callers must not fetch on a refusal. */
export function guardProvider(provider: string): string | null {
  return invoiceProvider(provider) ? null : PROVIDER_REFUSAL;
}

function wholeSeats(raw: string): number | null {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return null;
  const n = Number(text);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text.trim()) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

export function errorString(body: unknown, status: number): string {
  if (typeof body === "string" && body.trim()) return body.trim();
  if (body && typeof body === "object") {
    const rec = body as Record<string, unknown>;
    for (const key of ["detail", "error", "message"]) {
      const value = rec[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  return `BOSS answered ${status}`;
}

function counts(body: unknown): { added?: number; skipped_existing?: number } {
  if (!body || typeof body !== "object") return {};
  const rec = body as Record<string, unknown>;
  const out: { added?: number; skipped_existing?: number } = {};
  if (typeof rec.added === "number" && Number.isFinite(rec.added)) out.added = rec.added;
  if (typeof rec.skipped_existing === "number" && Number.isFinite(rec.skipped_existing)) out.skipped_existing = rec.skipped_existing;
  return out;
}

const JSON_POST = { "Content-Type": "application/json" };

export async function postInvoice(doFetch: JsonFetch, input: {
  projectId: string; provider: string; content: string;
}): Promise<PostResult> {
  const refused = guardProvider(input.provider);
  if (refused) return { ok: false, error: refused };
  const res = await doFetch("/v1/invoices/import", {
    method: "POST",
    headers: JSON_POST,
    cache: "no-store",
    body: JSON.stringify({
      project_id: input.projectId,
      provider: invoiceProvider(input.provider),
      content: input.content,
    }),
  });
  const body = await readBody(res);
  if (!res.ok) return { ok: false, error: errorString(body, res.status) };
  return { ok: true, ...counts(body) };
}

export async function postSeatPrice(doFetch: JsonFetch, input: {
  projectId: string; provider: string; plan: string; seats: string; monthlyDollars: string;
}): Promise<PostResult> {
  const refused = guardProvider(input.provider);
  if (refused) return { ok: false, error: refused };
  const plan = input.plan.trim();
  const seats = wholeSeats(input.seats);
  const monthly_usd_micros = monthlyMicros(input.monthlyDollars);
  if (!plan) return { ok: false, error: "Name the plan." };
  if (seats === null) return { ok: false, error: "At least one whole seat." };
  if (monthly_usd_micros === null) return { ok: false, error: "Need a monthly amount." };
  const res = await doFetch("/v1/subscription-prices", {
    method: "POST",
    headers: JSON_POST,
    cache: "no-store",
    body: JSON.stringify({
      project_id: input.projectId,
      provider: invoiceProvider(input.provider),
      plan,
      seats,
      monthly_usd_micros,
    }),
  });
  const body = await readBody(res);
  if (!res.ok) return { ok: false, error: errorString(body, res.status) };
  return { ok: true, ...counts(body) };
}

/** Short visible counts. Null when the body did not say them. Never a month-close claim. */
export function importNotice(result: PostResult): string | null {
  if (!result.ok || typeof result.added !== "number" || typeof result.skipped_existing !== "number") return null;
  return `Added ${result.added}, skipped ${result.skipped_existing}`;
}
