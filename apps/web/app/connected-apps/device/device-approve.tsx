"use client";

/** The signed-in person approves or denies the code a terminal is showing. The protocol already points here. */
import React, { useRef, useState } from "react";

import { IdentityError, idPost, refusalSentence } from "../../../lib/identity/api";

import "./device.css";

type Decision = { user_code: string; status: string };

function said(e: unknown): string {
  if (e instanceof IdentityError) {
    const desc = e.body.error_description;
    if (typeof desc === "string" && /\s/.test(desc)) return desc;
  }
  return refusalSentence(e);
}

export function DeviceApprove({ initialCode = "" }: { initialCode?: string }) {
  const [code, setCode] = useState(initialCode.trim().toUpperCase());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<Decision | null>(null);
  const inflight = useRef(false);

  async function decide(approve: boolean) {
    const userCode = code.trim();
    if (!userCode) {
      setError("Enter the code first.");
      setDone(null);
      return;
    }
    if (inflight.current) return;
    inflight.current = true;
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      setDone(await idPost<Decision>("/v1/mcp-oauth/device/approve", { user_code: userCode, approve }));
    } catch (e) {
      setError(said(e));
    } finally {
      inflight.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="idn-card idn-device" aria-labelledby="device-h">
      <p className="idn-label">Terminal</p>
      <h1 id="device-h">Approve this code</h1>
      <p className="idn-prose" data-copy="prose">The terminal is waiting. Approving lets that client act as you.</p>
      <form onSubmit={(e) => { e.preventDefault(); void decide(true); }}>
        <div className="idn-field">
          <label htmlFor="device-code">Code</label>
          <input
            id="device-code"
            className="idn-input idn-device-code"
            autoComplete="one-time-code"
            spellCheck={false}
            autoCapitalize="characters"
            value={code}
            onChange={(e) => { setCode(e.target.value.toUpperCase()); setDone(null); }}
            data-testid="device-code"
          />
        </div>
        <div className="idn-acts">
          <button type="submit" className="idn-btn primary" disabled={busy} data-testid="device-approve">Approve</button>
          <button type="button" className="idn-btn danger" disabled={busy} data-testid="device-deny" onClick={() => void decide(false)}>Deny</button>
        </div>
      </form>
      {error ? <div className="idn-err" role="alert">{error}</div> : null}
      {done?.status === "approved" ? <p className="idn-note info" role="status" data-copy="prose">Approved. The terminal can continue.</p> : null}
      {done?.status === "denied" ? <p className="idn-note bad" role="status" data-copy="prose">Denied. That terminal was not connected.</p> : null}
    </section>
  );
}
