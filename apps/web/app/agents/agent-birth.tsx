"use client";

/**
 * The birth (spec §3.11, §7): 900ms in which a new agent is put together — head, visor, hat
 * dropping on, emblem popping — and a `BORN` stamp lands on its certificate: ID, date, by You.
 * Then it becomes the Passport. Under reduced motion it is the finished certificate, stamped.
 * A press anywhere skips straight on; nothing waits on the animation.
 */
import React, { useEffect, useRef } from "react";

import type { AgentIdentity } from "../../lib/agent-world-types";
import { bornDate } from "../../lib/agent-world";
import { AgFace, AiBadge } from "./agent-face";

export const BIRTH_MS = 900;
const HOLD_MS = 1100;

export function AgentBirth({ identity, agentId, onDone }: {
  identity: AgentIdentity; agentId: string; onDone: () => void;
}) {
  const done = useRef(false);
  const finish = () => { if (!done.current) { done.current = true; onDone(); } };
  useEffect(() => {
    const t = setTimeout(finish, BIRTH_MS + HOLD_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <div className="ag-birth" role="status" aria-label={`${identity.name} is born`} data-testid="ag-birth"
         onClick={finish}>
      <div className="ag-birth-cert">
        <div className="ag-birth-face"><AgFace roleKey={identity.roleKey} identity={identity} size={96} decorative
                                                  aiBadge={false} motion="birth" /></div>
        <div className="ag-birth-name">{identity.callsign || identity.name}</div>
        <AiBadge long />
        <dl className="ag-birth-facts">
          <div><dt>ID</dt><dd><code>{agentId || "—"}</code></dd></div>
          <div><dt>Born</dt><dd>{bornDate(new Date().toISOString())}</dd></div>
          <div><dt>By</dt><dd>You</dd></div>
        </dl>
        <span className="ag-birth-stamp" aria-hidden>BORN</span>
      </div>
    </div>
  );
}
