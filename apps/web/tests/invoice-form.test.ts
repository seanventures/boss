/**
 * Invoice import and seat price from the reconciliation card.
 * monthlyMicros turns a dollar string into micros. A provider outside
 * anthropic, openai and openrouter is refused before any fetch.
 */
import { describe, expect, it, vi } from "vitest";

import { guardProvider, monthlyMicros, postInvoice, postSeatPrice } from "../lib/invoice-form";

describe("monthlyMicros", () => {
  it("is null when blank, negative, or not a number", () => {
    expect(monthlyMicros("")).toBeNull();
    expect(monthlyMicros("   ")).toBeNull();
    expect(monthlyMicros("-1")).toBeNull();
    expect(monthlyMicros("-0.01")).toBeNull();
    expect(monthlyMicros("nope")).toBeNull();
    expect(monthlyMicros("NaN")).toBeNull();
    expect(monthlyMicros("Infinity")).toBeNull();
  });

  it("rounds dollars to micros", () => {
    expect(monthlyMicros("0")).toBe(0);
    expect(monthlyMicros("1")).toBe(1_000_000);
    expect(monthlyMicros("1.25")).toBe(Math.round(1.25 * 1_000_000));
    expect(monthlyMicros(" 12.5 ")).toBe(12_500_000);
  });
});

describe("provider guard", () => {
  it("refuses a provider outside the three before fetch", async () => {
    expect(guardProvider("digitalocean")).toBeTruthy();
    expect(guardProvider("anthropic")).toBeNull();
    expect(guardProvider("OpenAI")).toBeNull();
    expect(guardProvider(" openrouter ")).toBeNull();

    const fetchImpl = vi.fn();
    const invoice = await postInvoice(fetchImpl, { projectId: "p1", provider: "aws", content: "date,amount" });
    const price = await postSeatPrice(fetchImpl, {
      projectId: "p1", provider: "azure", plan: "pro", seats: "2", monthlyDollars: "20",
    });
    expect(invoice.ok).toBe(false);
    expect(price.ok).toBe(false);
    if (!invoice.ok) expect(invoice.error.length).toBeGreaterThan(0);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("posts the import and the seat price for an allowed provider", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ added: 2, skipped_existing: 1 }), {
      status: 201, headers: { "Content-Type": "application/json" },
    }));
    const invoice = await postInvoice(fetchImpl, { projectId: "p1", provider: "anthropic", content: "csv" });
    expect(invoice).toMatchObject({ ok: true, added: 2, skipped_existing: 1 });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("/v1/invoices/import");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(init?.headers).not.toHaveProperty("Authorization");
    expect(JSON.parse(String(init?.body))).toEqual({ project_id: "p1", provider: "anthropic", content: "csv" });

    fetchImpl.mockResolvedValueOnce(new Response(JSON.stringify({ id: "sp1" }), { status: 201 }));
    const price = await postSeatPrice(fetchImpl, {
      projectId: "p1", provider: "openai", plan: "team", seats: "3", monthlyDollars: "20",
    });
    expect(price.ok).toBe(true);
    const [, priceInit] = fetchImpl.mock.calls[1];
    expect(fetchImpl.mock.calls[1][0]).toBe("/v1/subscription-prices");
    expect(JSON.parse(String(priceInit?.body))).toEqual({
      project_id: "p1", provider: "openai", plan: "team", seats: 3, monthly_usd_micros: 20_000_000,
    });
  });

  it("returns the error string and does not claim the month closed", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({
      error: "invoice_refused", detail: "The invoice file is empty.",
    }), { status: 422 }));
    const invoice = await postInvoice(fetchImpl, { projectId: "p1", provider: "anthropic", content: "" });
    expect(invoice).toEqual({ ok: false, error: "The invoice file is empty." });
    if (!invoice.ok) expect(invoice.error.toLowerCase()).not.toContain("month closed");
  });
});
