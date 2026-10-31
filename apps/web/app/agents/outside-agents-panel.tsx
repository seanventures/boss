"use client";

/**
 * Agents › Outside agents (KG P6b console): a bank's own HTTP-hosted agent, registered,
 * tested, suspended or removed. One surface, the API's own words for every refusal.
 *
 * THE AUTH SECRET NEVER COMES BACK. The form writes it through `/v1/secrets` (infra's own
 * store) and only hands the resulting `secret_ref_id` to `/v1/external-agents` — the same two
 * steps the API itself requires (routers/external_agents.py `_public` never includes a value).
 * Suspend/Remove ask a NAMED confirm first: pressing once shows "Suspend <name>? / Confirm", a
 * second, different press cancels it, so nothing is revoked on a stray click.
 */
import React, { useCallback, useEffect, useState } from "react";

import { useCan } from "../../components/identity/identity-provider";
import {
  afterOutsideAgentWrite, hostOf, listOutsideAgents, putOutsideAgentSecret, registerOutsideAgent,
  removeOutsideAgent, suspendOutsideAgent, testOutsideAgent, type OutsideAgent,
} from "./outside-agents-actions";

export function OutsideAgentsPanel({ projectId, actor }: { projectId: string; actor: string }) {
  const [agents, setAgents] = useState<OutsideAgent[]>([]);
  // One gate per permission `outside-agents-actions.ts` presses (lib/identity/write-permissions.ts):
  // registerOutsideAgent + putOutsideAgentSecret (Add), testOutsideAgent (Test), suspend/removeOutsideAgent (Suspend/Remove).
  const canAdd = useCan("agent:create");
  const canManageSecret = useCan("secret:manage");
  const canTest = useCan("agent:test");
  const canLifecycle = useCan("agent:lifecycle");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [secret, setSecret] = useState("");
  const [sponsor, setSponsor] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [testWord, setTestWord] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState<{ id: string; action: "suspend" | "remove" } | null>(null);
  const [rowError, setRowError] = useState<Record<string, string>>({});

  const reload = useCallback(() => {
    if (!projectId) return;
    void listOutsideAgents(projectId).then((r) => {
      if (r.ok) { setAgents(r.data.items ?? []); setLoadError(null); } else setLoadError(r.error);
    });
  }, [projectId]);
  useEffect(() => { reload(); }, [reload]);

  const submit = async () => {
    setFormError(null);
    if (!name.trim() || !endpoint.trim() || !secret || !sponsor.trim()) {
      setFormError("Name, endpoint, auth secret and sponsor are all required.");
      return;
    }
    setBusy("add");
    const secretResult = await putOutsideAgentSecret(
      projectId, actor, { name: `external-agent/${name.trim()}`, value: secret });
    if (!secretResult.ok) { setBusy(null); setFormError(secretResult.error); return; }
    const secretRefId = secretResult.data.secret_ref_id;
    if (!secretRefId) { setBusy(null); setFormError("The secret store did not return a reference."); return; }
    const result = await registerOutsideAgent(projectId, actor, {
      display_name: name.trim(), endpoint_url: endpoint.trim(),
      secret_ref_id: secretRefId, sponsor_principal_id: sponsor.trim(),
    });
    setBusy(null);
    if (!result.ok) { setFormError(result.error); return; }
    setName(""); setEndpoint(""); setSecret(""); setSponsor(""); setAdding(false);
    afterOutsideAgentWrite(reload);
  };

  const runTest = async (agent: OutsideAgent) => {
    setBusy(`test-${agent.id}`);
    const r = await testOutsideAgent(projectId, actor, agent.id);
    setBusy(null);
    setTestWord((w) => ({ ...w, [agent.id]: !r.ok ? r.error : r.data.reachable ? "Reachable" : "Unreachable" }));
  };

  const act = async (agent: OutsideAgent, action: "suspend" | "remove") => {
    setConfirming(null);
    setRowError((e) => ({ ...e, [agent.id]: "" }));
    setBusy(`${action}-${agent.id}`);
    const r = action === "suspend"
      ? await suspendOutsideAgent(projectId, actor, agent.id)
      : await removeOutsideAgent(projectId, actor, agent.id);
    setBusy(null);
    if (!r.ok) { setRowError((e) => ({ ...e, [agent.id]: r.error })); return; }
    afterOutsideAgentWrite(reload);
  };

  return (
    <section className="ag-outside" data-testid="ag-outside-agents">
      <div className="ct-block-title">Outside agents</div>
      {loadError ? <p className="detail" data-testid="ag-outside-load-error">{loadError}</p> : null}
      <table className="inf-table" data-testid="ag-outside-table">
        <thead>
          <tr>
            <th scope="col">Name</th><th scope="col">Endpoint</th><th scope="col">Sponsor</th>
            <th scope="col">Status</th><th scope="col"><span className="ag-sr">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {agents.length === 0 ? (
            <tr><td colSpan={5} className="detail" data-testid="ag-outside-empty">No outside agents yet</td></tr>
          ) : null}
          {agents.map((agent) => (
            <tr key={agent.id} data-testid={`ag-outside-row-${agent.id}`}>
              <td>{agent.display_name}</td>
              <td className="inf-mono" title={agent.endpoint_url}>{hostOf(agent.endpoint_url)}</td>
              <td>{agent.sponsor_principal_id}</td>
              <td>{agent.status}</td>
              <td>
                <button type="button" className="ag-btn" disabled={busy !== null || !canTest.allowed}
                        title={!canTest.allowed ? canTest.reason : "Call it and say, in words, whether it answered"}
                        data-testid={`ag-outside-test-${agent.id}`} onClick={() => void runTest(agent)}>
                  {busy === `test-${agent.id}` ? "Testing…" : "Test"}
                </button>
                {testWord[agent.id] ? (
                  <span data-testid={`ag-outside-test-word-${agent.id}`}> {testWord[agent.id]}</span>
                ) : null}
                {confirming?.id === agent.id ? (
                  <span data-testid={`ag-outside-confirm-${agent.id}`}>
                    {" "}{confirming.action === "suspend" ? `Suspend ${agent.display_name}?` : `Remove ${agent.display_name}?`}
                    <button type="button" className="ag-btn ag-btn-primary" disabled={busy !== null || !canLifecycle.allowed}
                            title={!canLifecycle.allowed ? canLifecycle.reason : undefined}
                            data-testid={`ag-outside-confirm-do-${agent.id}`}
                            onClick={() => void act(agent, confirming.action)}>Confirm</button>
                    <button type="button" className="ag-btn" disabled={busy !== null}
                            data-testid={`ag-outside-confirm-cancel-${agent.id}`}
                            onClick={() => setConfirming(null)}>Cancel</button>
                  </span>
                ) : (
                  <>
                    <button type="button" className="ag-btn" disabled={busy !== null || !canLifecycle.allowed}
                            title={!canLifecycle.allowed ? canLifecycle.reason
                              : "Revoke every live token this agent holds; a run in flight loses access at its next call"}
                            data-testid={`ag-outside-suspend-${agent.id}`}
                            onClick={() => setConfirming({ id: agent.id, action: "suspend" })}>Suspend</button>
                    <button type="button" className="ag-btn" disabled={busy !== null || !canLifecycle.allowed}
                            title={!canLifecycle.allowed ? canLifecycle.reason : "Revoke every live token this agent holds and remove it"}
                            data-testid={`ag-outside-remove-${agent.id}`}
                            onClick={() => setConfirming({ id: agent.id, action: "remove" })}>Remove</button>
                  </>
                )}
                {rowError[agent.id] ? (
                  <p className="detail" data-testid={`ag-outside-row-error-${agent.id}`}>{rowError[agent.id]}</p>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {adding ? (
        <form className="inf-form" data-testid="ag-outside-form"
              onSubmit={(e) => { e.preventDefault(); void submit(); }}>
          <div className="inf-form-row">
            <label className="inf-field" title="Shown everywhere this agent is picked"><span>Name</span>
              <input value={name} onChange={(e) => setName(e.target.value)} data-testid="ag-outside-name"
                     autoComplete="off" /></label>
            <label className="inf-field" title="Where BOSS calls it to run a contract stage"><span>Endpoint</span>
              <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} data-testid="ag-outside-endpoint"
                     placeholder="https://partner.example.com/agent" autoComplete="off" /></label>
          </div>
          <div className="inf-form-row">
            <label className="inf-field" title="Written through the secret store; never shown again">
              <span>Auth secret</span>
              <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)}
                     data-testid="ag-outside-secret" placeholder="Never shown again" autoComplete="off" /></label>
            <label className="inf-field" title="Who on this project answers for what this agent does"><span>Sponsor</span>
              <input value={sponsor} onChange={(e) => setSponsor(e.target.value)} data-testid="ag-outside-sponsor"
                     autoComplete="off" /></label>
          </div>
          <div className="inf-actions">
            <button type="submit" className="ag-btn ag-btn-primary"
                    disabled={busy !== null || !canAdd.allowed || !canManageSecret.allowed}
                    title={!canAdd.allowed ? canAdd.reason : !canManageSecret.allowed ? canManageSecret.reason : undefined}
                    data-testid="ag-outside-submit">
              {busy === "add" ? "Adding…" : "Add outside agent"}
            </button>
            <button type="button" className="ag-btn" disabled={busy !== null}
                    onClick={() => { setAdding(false); setFormError(null); }}>Cancel</button>
            {formError ? <p className="detail" data-testid="ag-outside-form-error">{formError}</p> : null}
          </div>
        </form>
      ) : (
        <button type="button" className="ag-btn ag-btn-primary" data-testid="ag-outside-open-form"
                disabled={!canAdd.allowed} title={!canAdd.allowed ? canAdd.reason : undefined}
                onClick={() => setAdding(true)}>+ Add outside agent</button>
      )}
    </section>
  );
}
