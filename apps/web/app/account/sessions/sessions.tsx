"use client";

/**
 * MY ACCOUNT › SESSIONS (IDENTITY.md §4.3, J6): every browser signed in as you, with where and
 * how it signed in. End one, or sign out of every other; an ended session is refused at its
 * next request.
 */
import React, { useState } from "react";
import { DeviceMobile, Desktop, SignOut } from "@phosphor-icons/react";

import { IdentityError, idDelete, idGet, refusalSentence } from "../../../lib/identity/api";
import type { SessionRow } from "../../../lib/identity/types";
import { toSessionRows } from "../../../lib/identity/adapt-session";
import { useGuardedCall } from "../../../components/identity/identity-provider";
import { Ago, ErrorLine, Pill } from "../../../components/identity/kit";
import { showToast } from "../../client-forms";
import { publicAddress } from "../../../lib/identity/address";
import { methodWords as oneMethod } from "../../../lib/identity/words";

/** How a session signed in: one vocabulary with an admin's view of the same session
 *  (lib/identity/words.ts), so "TOTP" here and "authenticator app" there cannot both appear. */
export function methodWords(methods: string[]): string {
  return methods.map(oneMethod).join(" + ");
}

export function MySessions({ sessions: initial }: { sessions: SessionRow[] }) {
  const [sessions, setSessions] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const guarded = useGuardedCall();
  const reload = async () => setSessions(toSessionRows((await idGet<{ sessions: unknown[] }>("/v1/identity/me/sessions")).sessions));
  const end = (call: () => Promise<unknown>, toast: string) => {
    setError(null);
    guarded("Ending sessions signs those browsers out.", call)
      .then(reload).then(() => showToast("success", toast))
      .catch((e) => setError(refusalSentence(e)));
  };
  const others = sessions.filter((s) => !s.current);
  return (
    <section className="idn-card" aria-labelledby="sess-h">
      <div className="idn-card-h">
        <h2 id="sess-h">Sessions</h2>
        <span className="idn-sub">{sessions.length} active</span>
        <span className="idn-spacer" />
        {others.length ? (
          <button type="button" className="idn-btn sm danger" data-testid="end-others" onClick={() => end(() => idDelete("/v1/identity/me/sessions"), "Signed out elsewhere")}>
            <SignOut size={14} aria-hidden /> Sign out of all others
          </button>
        ) : null}
      </div>
      <ul className="idn-plain" data-testid="my-sessions">
        {sessions.map((s) => (
          <li key={s.id} className="idn-sess">
            <span className="idn-d">{s.device === "phone" ? <DeviceMobile size={17} aria-hidden /> : <Desktop size={17} aria-hidden />}</span>
            <span>
              <b data-copy="data">{s.client}</b> {s.current ? <Pill tone="info">this one</Pill> : null}
              <small data-copy="data">{[s.location, publicAddress(s.ip), methodWords(s.methods)].filter(Boolean).join(" · ")} · started <Ago iso={s.started_at} /></small>
            </span>
            {s.current ? <span className="idn-dim">now</span> : (
              <button type="button" className="idn-btn sm" aria-label={`End ${s.client}`} onClick={() => end(() => idDelete(`/v1/identity/me/sessions/${encodeURIComponent(s.id)}`), "Session ended")}>End</button>
            )}
          </li>
        ))}
      </ul>
      <ErrorLine error={error} />
    </section>
  );
}
