"use client";

/**
 * Test crew (spec §5.6): every Ready ability's test, then for each Essential "can at least one
 * agent use it right now?" — the holders, the approval rule and the server, read the way the
 * runtime reads them. The live check (optional) runs one call AS Juno and one AS Strata through
 * their own grants, with receipts.
 */
import { GatedButton } from "../../components/gated";
import React, { useState } from "react";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { CheckCircle, XCircle } from "@phosphor-icons/react";

import type { Ability, CrewTest } from "../../lib/kit-types";
import { kitApi } from "../../lib/kit-api";
import { AgentAvatar } from "../agent-avatar";
import { TIP, agentName } from "./kit-copy";
import { ModalShell } from "./overlay";

export function TestCrewModal({ projectId, actor, abilities, onClose, onDone, onOpenAbility }: {
  projectId: string;
  actor: string;
  abilities: Ability[];
  onClose: () => void;
  onDone: (passed: boolean) => void;
  onOpenAbility: (key: string) => void;
}) {
  useIdentityVersion(); // names repaint when identities load
  const [live, setLive] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<CrewTest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const label = (key: string) => abilities.find((a) => a.key === key)?.label ?? key;

  const run = async () => {
    setRunning(true); setError(null); setResult(null);
    const r = await kitApi.testCrew(projectId, actor, live);
    setRunning(false);
    if (!r.ok) { setError(r.error); return; }
    setResult(r.data);
    const passed = r.data.rows.every((row) => row.ok) && (r.data.live ?? []).every((c) => c.ok);
    onDone(passed);
  };

  const passed = result ? result.rows.every((r) => r.ok) && (result.live ?? []).every((c) => c.ok) : false;
  return (
    <ModalShell label="Test crew" testId="kit-crew-modal" onClose={onClose}>
        <header className="kit-modal-head">
          <h3 data-tip={TIP.testCrew}>{passed ? "Crew ready" : "Test crew"}</h3>
          <button type="button" className="kit-icon-btn" aria-label="Close" onClick={onClose}>×</button>
        </header>
        <label className="kit-check">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)}
                 data-testid="kit-crew-live" />
          <span data-tip="Juno reads a page and Strata reads the repo, as themselves">Include a live check</span>
        </label>
        <ul className="kit-crew-rows" data-testid="kit-crew-rows">
          {(result?.rows ?? []).map((row) => (
            <li key={row.ability} data-ok={row.ok} data-testid={`kit-crew-row-${row.ability}`}>
              {row.ok ? <CheckCircle className="kit-ok" size={18} weight="fill" /> : <XCircle className="kit-bad" size={18} weight="fill" />}
              <span className="kit-crew-name">{label(row.ability)}</span>
              {row.holders.length ? (
                <span className="kit-crew-holders" data-tip={row.holders.map((h) => agentName(h)).join(", ")}>
                  {row.holders.slice(0, 4).map((h) => <AgentAvatar key={h} agent={h} size={20} aiBadge={false} />)}
                </span>
              ) : null}
              {!row.ok ? (
                <button type="button" className="kit-link" data-tip={row.detail}
                        onClick={() => onOpenAbility(row.ability)}>
                  {row.detail === "No one holds it" ? "Give to agents" : "Fix"}
                </button>
              ) : null}
            </li>
          ))}
          {(result?.live ?? []).map((c) => (
            <li key={c.role_key} data-ok={c.ok} data-testid={`kit-crew-live-${c.role_key}`}>
              {c.ok ? <CheckCircle className="kit-ok" size={18} weight="fill" /> : <XCircle className="kit-bad" size={18} weight="fill" />}
              <AgentAvatar agent={c.role_key} size={20} state={c.ok ? "done" : "stuck"} aiBadge={false} />
              <span className="kit-crew-name" data-tip={c.detail}>{agentName(c.role_key)}</span>
            </li>
          ))}
        </ul>
        {error ? <div className="kit-err" role="alert" data-tip={error}>Test failed</div> : null}
        <footer className="kit-modal-foot">
          <GatedButton permission="skill:invoke" type="button" className="kit-btn kit-btn-primary" disabled={running}
                  data-testid="kit-crew-run" onClick={() => void run()}>
            {running ? "Testing…" : "Run"}
          </GatedButton>
        </footer>
    </ModalShell>
  );
}
