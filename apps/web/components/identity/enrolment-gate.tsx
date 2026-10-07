"use client";

/**
 * Rendered INSTEAD of the console for an enrolment-only session (lib/identity/enrolment.ts): every
 * read the console would make is refused `mfa_enrolment_required`, so it would show broken pages.
 * It goes to /enrol on its own (a render, not a server redirect: the console streams, and a late
 * redirect reaches the browser as a 500) and says why, with the link, in case script is slow.
 */
import { useEffect } from "react";

export function EnrolmentGate({ href }: { href: string }) {
  useEffect(() => { window.location.replace(href); }, [href]);
  return (
    <main className="idn" data-testid="enrolment-gate">
      <section className="idn-card" role="status">
        <h1>Add a second factor first</h1>
        <p data-copy="prose">Your role needs a second factor before BOSS opens. It takes a minute: a passkey or an authenticator app.</p>
        <a className="idn-btn" href={href}>Add a second factor</a>
      </section>
    </main>
  );
}
