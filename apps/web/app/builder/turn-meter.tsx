"use client";

/**
 * Builder turn meter (BOSSAPP-1246). One job plate: a travel tape for elapsed
 * work, a perforated stub for money that receipts already settled, and a
 * credit check-in before any further reservation.
 *
 * Plate #243044, tape #F4E4C4, copper #8C4A2F, moss #1F6B4A, hold #A33B24.
 * Digits in Chivo Mono, labels in Barlow Condensed, notes in Figtree.
 * The signature is the copper needle on the tape, not a stat card.
 */
import React, { useEffect, useRef, useState } from "react";

export type TurnCheckin = {
  id: string;
  request_key: string;
  dedupe_key: string;
  amount_usd_micros: number;
  status: string;
  reservation_id: string | null;
  receipt_id: string | null;
  error: string | null;
  deadletter_id: string | null;
};

export type TurnMeterView = {
  turn_id: string;
  project_id: string;
  status: string;
  elapsed_ms: number;
  settled_usd_micros: number | null;
  settled_known: boolean;
  error: string | null;
  retry?: boolean;
  checkin: TurnCheckin | null;
};

const TAPE_MS = 10 * 60 * 1000;

export function dollarsToMicros(raw: string): number | null {
  const trimmed = raw.trim();
  if (!/^\d{1,7}(\.\d{1,6})?$/.test(trimmed)) return null;
  const [whole, frac = ""] = trimmed.split(".");
  const micros = Number(whole) * 1_000_000 + Number((frac + "000000").slice(0, 6));
  if (!Number.isSafeInteger(micros) || micros <= 0) return null;
  return micros;
}

export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Exact dollars from micros. Null stays unsettled — never a made-up $0. */
export function formatSettled(micros: number | null | undefined): string {
  if (micros === null || micros === undefined) return "Not settled";
  const sign = micros < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(micros));
  const dollars = Math.floor(abs / 1_000_000);
  const frac = String(abs % 1_000_000).padStart(6, "0");
  return `${sign}$${dollars}.${frac}`;
}

function statusWord(status: string): string {
  if (status === "paused_for_credit") return "Paused";
  if (status === "declined") return "Stopped";
  if (status === "failed") return "Failed";
  return "Working";
}

async function pull(url: string, init?: RequestInit): Promise<TurnMeterView> {
  const res = await fetch(url, init);
  const data = await res.json();
  if (data && typeof data.turn_id === "string") return data as TurnMeterView;
  const detail = typeof data?.detail === "string" ? data.detail : "Meter unavailable";
  throw new Error(detail);
}

export function TurnMeter({ projectId, turnId }: { projectId: string; turnId: string }) {
  const [meter, setMeter] = useState<TurnMeterView | null>(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const keyRef = useRef("");

  useEffect(() => {
    if (!projectId || !turnId) return undefined;
    let live = true;
    setProblem("");
    void pull(`/v1/builder/turns/${encodeURIComponent(turnId)}?project_id=${encodeURIComponent(projectId)}`)
      .then((view) => { if (live) setMeter(view); })
      .catch((error: Error) => { if (live) setProblem(error.message || "Meter unavailable"); });
    return () => { live = false; };
  }, [projectId, turnId]);

  const post = async (path: string, body: Record<string, unknown>) => {
    setBusy(true);
    setProblem("");
    try {
      const view = await pull(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      setMeter(view);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "Meter unavailable");
    } finally {
      setBusy(false);
    }
  };

  const ask = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const raw = String(new FormData(event.currentTarget).get("credit") || "");
    const amount = dollarsToMicros(raw);
    if (amount === null) {
      setProblem("Enter a dollar amount");
      return;
    }
    if (!keyRef.current) {
      const nonce = typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID().slice(0, 8)
        : String(Date.now());
      keyRef.current = `ask-${amount}-${nonce}`;
    }
    void post(`/v1/builder/turns/${encodeURIComponent(turnId)}/credit`, {
      project_id: projectId,
      request_key: keyRef.current,
      amount_usd_micros: amount,
    });
  };

  const answer = (decision: "approve" | "decline") => {
    const key = meter?.checkin?.request_key || keyRef.current;
    if (!key) return;
    void post(`/v1/builder/turns/${encodeURIComponent(turnId)}/credit/answer`, {
      project_id: projectId,
      request_key: key,
      decision,
    }).then(() => {
      if (decision === "decline" || decision === "approve") keyRef.current = "";
    });
  };

  const pending = meter?.checkin?.status === "pending";
  const held = meter?.checkin?.status === "approved" && meter.checkin.reservation_id
    ? meter.checkin.amount_usd_micros
    : null;
  const needle = Math.max(0, Math.min(100, ((meter?.elapsed_ms ?? 0) / TAPE_MS) * 100));
  const hold = meter?.status === "paused_for_credit" || meter?.status === "declined" || meter?.status === "failed";
  const fault = meter?.error || problem;

  return (
    <section className="tm1246" aria-label="Builder turn" data-testid="turn-meter" data-status={meter?.status || "loading"}>
      <style>{CSS}</style>
      <header className="tm1246-head">
        <span className="tm1246-kicker">Builder turn</span>
        <span className="tm1246-id" data-user-text data-testid="turn-id">{turnId}</span>
        <span className="tm1246-state" data-testid="turn-status">{meter ? statusWord(meter.status) : "Reading"}</span>
      </header>
      <div className="tm1246-tape" data-hold={hold || undefined} role="img"
           aria-label={meter ? `Elapsed ${formatElapsed(meter.elapsed_ms)}` : "Elapsed"}>
        <span className="tm1246-rules" aria-hidden="true" />
        <span className="tm1246-needle" style={{ left: `${needle}%` }} data-testid="elapsed-needle" />
        <span className="tm1246-digits" data-testid="elapsed-figure">{meter ? formatElapsed(meter.elapsed_ms) : "--:--"}</span>
      </div>
      <p className="tm1246-caption">
        <span title="Clock time since this turn started">Elapsed</span>
        <span title="Only money already on a receipt">Settled</span>
      </p>
      <div className="tm1246-stub" data-known={meter?.settled_known || undefined} data-testid="settled-stub">
        <span className="tm1246-stub-label">Settled</span>
        <span className="tm1246-stub-figure" data-testid="settled-figure">
          {meter ? formatSettled(meter.settled_usd_micros) : "Not settled"}
        </span>
        {held !== null ? (
          <span className="tm1246-held" data-testid="held-figure" title="Reserved, not settled">
            Held {formatSettled(held)}
          </span>
        ) : null}
      </div>
      {fault ? <p className="tm1246-fault" data-testid="turn-error">{fault}</p> : null}
      {pending ? (
        <div className="tm1246-stamps">
          <span className="tm1246-asking" data-testid="asked-figure" title="This credit is not reserved yet">
            Asking {formatSettled(meter?.checkin?.amount_usd_micros ?? null)}
          </span>
          <button type="button" className="tm1246-stamp tm1246-allow" data-testid="allow-credit"
                  title="Reserve this amount and continue" disabled={busy}
                  onClick={() => answer("approve")}>Allow more</button>
          <button type="button" className="tm1246-stamp tm1246-stop" data-testid="stop-credit"
                  title="Decline. No new reservation." disabled={busy}
                  onClick={() => answer("decline")}>Stop here</button>
        </div>
      ) : (
        <form className="tm1246-ask" onSubmit={ask}>
          <label className="tm1246-field">
            <span>More credit</span>
            <input name="credit" inputMode="decimal" required autoComplete="off"
                   placeholder="0.00" aria-label="More credit in dollars" data-testid="credit-amount" />
          </label>
          <button type="submit" className="tm1246-stamp" data-testid="ask-credit" disabled={busy}
                  title="Pause until this credit is allowed">Ask</button>
          {meter?.retry ? (
            <button type="button" className="tm1246-stamp" data-testid="retry-credit" disabled={busy}
                    title="Try the same credit again" onClick={() => answer("approve")}>Retry</button>
          ) : null}
        </form>
      )}
    </section>
  );
}

const CSS = `
.tm1246 {
  --plate: #243044;
  --tape: #f4e4c4;
  --copper: #8c4a2f;
  --ink: #1b2330;
  --moss: #1f6b4a;
  --hold: #a33b24;
  --rule: #c9b48a;
  --paper: #efe2c6;
  box-sizing: border-box;
  max-width: 34rem;
  padding: 1.1rem 1.15rem 1.2rem;
  color: var(--tape);
  background:
    radial-gradient(circle at 12% 0%, rgba(244, 228, 196, 0.08), transparent 42%),
    var(--plate);
  border: 1px solid #3c4d63;
  font-family: Figtree, "Avenir Next", "Segoe UI", sans-serif;
}
.tm1246 * { box-sizing: border-box; }
.tm1246-head, .tm1246-caption, .tm1246-stub, .tm1246-stamps, .tm1246-ask {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
}
.tm1246-kicker, .tm1246-state, .tm1246-caption, .tm1246-stub-label, .tm1246-field span {
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-size: 0.78rem;
}
.tm1246-id {
  font-family: "Chivo Mono", ui-monospace, monospace;
  font-size: 0.72rem;
  color: var(--rule);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.tm1246-state { color: var(--tape); }
.tm1246[data-status="paused_for_credit"] .tm1246-state,
.tm1246[data-status="failed"] .tm1246-state,
.tm1246[data-status="declined"] .tm1246-state { color: #f0b7a8; }
.tm1246-tape {
  position: relative;
  margin: 0.85rem 0 0.35rem;
  height: 4.6rem;
  background: var(--tape);
  color: var(--ink);
  overflow: hidden;
  border-bottom: 2px solid var(--copper);
}
.tm1246-rules {
  position: absolute;
  inset: 0.55rem 0 auto;
  height: 0.7rem;
  background: repeating-linear-gradient(
    90deg,
    var(--copper) 0 2px,
    transparent 2px 1.6rem
  );
  opacity: 0.85;
}
.tm1246-needle {
  position: absolute;
  top: 0.35rem;
  bottom: 1.7rem;
  width: 2px;
  margin-left: -1px;
  background: var(--copper);
  transition: left 180ms linear;
}
.tm1246-needle::after {
  content: "";
  position: absolute;
  left: -5px;
  bottom: -6px;
  border-left: 6px solid transparent;
  border-right: 6px solid transparent;
  border-top: 8px solid var(--copper);
}
.tm1246-digits {
  position: absolute;
  left: 0.8rem;
  bottom: 0.35rem;
  font-family: "Chivo Mono", ui-monospace, monospace;
  font-weight: 700;
  font-size: 1.65rem;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.03em;
}
.tm1246-tape[data-hold="true"]::after {
  content: "";
  position: absolute;
  inset: 0;
  background: repeating-linear-gradient(
    -18deg,
    transparent 0 10px,
    rgba(163, 59, 36, 0.18) 10px 12px
  );
  pointer-events: none;
}
.tm1246-caption { margin: 0.35rem 0 0; color: var(--rule); }
.tm1246-stub {
  margin-top: 0.65rem;
  padding: 0.7rem 0.85rem 0.85rem;
  background: var(--paper);
  color: var(--ink);
  background-image: radial-gradient(circle at 8px 0, transparent 5px, var(--paper) 6px);
  background-size: 16px 10px;
  background-repeat: repeat-x;
  border-top: 1px dashed var(--copper);
  align-items: center;
}
.tm1246-stub-figure {
  font-family: "Chivo Mono", ui-monospace, monospace;
  font-size: 1.15rem;
  font-weight: 700;
}
.tm1246-stub[data-known="true"] .tm1246-stub-figure { color: var(--moss); }
.tm1246-held, .tm1246-asking {
  font-family: "Chivo Mono", ui-monospace, monospace;
  font-size: 0.75rem;
  color: var(--copper);
}
.tm1246-fault {
  margin: 0.75rem 0 0;
  color: #f3d2c8;
  font-size: 0.95rem;
  line-height: 1.35;
}
.tm1246-ask, .tm1246-stamps { margin-top: 0.85rem; align-items: end; }
.tm1246-field { display: flex; flex-direction: column; gap: 0.2rem; flex: 1; }
.tm1246-field input {
  width: 100%;
  border: 0;
  border-bottom: 2px solid var(--copper);
  background: transparent;
  color: var(--tape);
  font-family: "Chivo Mono", ui-monospace, monospace;
  font-size: 1.05rem;
  padding: 0.35rem 0;
}
.tm1246-field input:focus-visible, .tm1246-stamp:focus-visible {
  outline: 2px solid var(--tape);
  outline-offset: 3px;
}
.tm1246-stamp {
  border: 1px solid var(--tape);
  background: transparent;
  color: var(--tape);
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  font-size: 0.95rem;
  padding: 0.45rem 0.75rem;
  cursor: pointer;
}
.tm1246-stamp:disabled { opacity: 0.45; cursor: not-allowed; }
.tm1246-allow { background: var(--moss); border-color: var(--moss); color: #f4e4c4; }
.tm1246-stop { background: var(--hold); border-color: var(--hold); color: #f4e4c4; }
@media (max-width: 420px) {
  .tm1246 { padding: 0.9rem; }
  .tm1246-head, .tm1246-ask, .tm1246-stamps { flex-wrap: wrap; }
  .tm1246-digits { font-size: 1.35rem; }
}
@media (prefers-reduced-motion: reduce) {
  .tm1246-needle { transition: none; }
}
`;
