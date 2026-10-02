"use client";

import React, { useEffect, useRef, useState } from "react";
import { useFormStatus } from "react-dom";

import { INTENT_FIELD, newIntentKey } from "../lib/api-write";

/**
 * A submit button that shows it was pressed.
 *
 * Every action in this console is a `<form action={serverAction}>`. A plain `<button>` inside
 * one renders identically before and after a click: the server action runs for seconds, the
 * page eventually swaps under you, and in between there is no evidence the click registered.
 * People click Approve again. And again.
 *
 * `useFormStatus()` is read from *inside* the form, so this needs no wiring at the call site —
 * no state to thread, no handler to remember. That matters more than elegance here: the last
 * attempt at this fixed one area by hand and left sixteen others silent.
 *
 * Multiple submits in one form (Approve / Revise / Reject) all share a single pending status,
 * so `status.data` is used to identify which button was actually pressed — otherwise every
 * button in the row spins at once and the UI lies about what is happening.
 *
 * ONE PRESS, ONE KEY. `disabled` lands only after React re-renders, so a fast double click
 * still submits twice — "Stop and run again" pressed twice started two workflows (2026-10-02).
 * Each button carries a hidden `__boss_intent` that names the press; middleware.ts lifts it into
 * the request, and every API write the server action makes derives its Idempotency-Key from it
 * (lib/api-write.ts), so the API runs a repeated press once. A new key is drawn only when the
 * press has finished, so everything submitted while it is pending shares it. Drawn after mount,
 * never during render: a server-rendered random value would not match the client's.
 */
export function SubmitButton({
  children,
  pendingLabel,
  disabled,
  className = "",
  name,
  value,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { pendingLabel?: string }) {
  const status = useFormStatus();
  const [intent, setIntent] = useState("");
  const wasPending = useRef(false);
  useEffect(() => {
    if (!intent || (wasPending.current && !status.pending)) setIntent(newIntentKey());
    wasPending.current = status.pending;
  }, [status.pending, intent]);

  // Which button submitted? With name/value we can tell precisely; without them, a lone
  // submit in the form is unambiguous anyway.
  const submittedValue = name && status.data ? status.data.get(name) : null;
  const isThisButton = name ? submittedValue === value : true;
  const pending = status.pending && isThisButton;

  // Siblings stay disabled while a sibling runs — pressing Reject mid-Approve is not a thing
  // anyone means to do — but only the pressed one shows the spinner.
  const blocked = disabled || status.pending;

  return (
    <>
    {intent ? <input type="hidden" name={INTENT_FIELD} value={intent} /> : null}
    <button
      {...rest}
      name={name}
      value={value}
      disabled={blocked}
      aria-busy={pending || undefined}
      data-pending={pending ? "true" : undefined}
      className={`${className} btn-submit${pending ? " is-pending" : ""}`.trim()}
    >
      {pending ? (
        <>
          <span className="btn-spinner" aria-hidden />
          <span>{pendingLabel ?? children}</span>
        </>
      ) : (
        children
      )}
    </button>
    </>
  );
}
