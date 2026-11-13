"use client";

/**
 * Setup › Advanced › Several accounts per provider (BOSSAPP-886): a pool of Claude or Codex
 * subscriptions used as one. Each run takes the account with the most room and keeps it for its
 * task; an account at its limit is skipped, and only when every one is does work wait. Per
 * account: its room, Pause/Resume, a fresh login, Remove. A login is never shown again.
 *
 * The row keeps Setup's anatomy (label column, body column) and the Add form is the one form
 * kit (components/form-kit.tsx, BOSSAPP-1039) — never a hand-rolled stack of bare inputs.
 */
import React, { useCallback, useEffect, useState } from "react";

import { Field, FormError, FormGrid } from "../../components/form-kit";
import { GatedButton } from "../../components/gated";
import { SectionLabel } from "../section-label";
import {
  addPoolMember, createPool, ENGINE_WORDS, readPools, removePoolMember, ROOM_WORDS, updatePoolMember, type Pool,
} from "../../lib/subscription-pools";

const ENGINES: Pool["engine"][] = ["claude_code", "codex"];

export function PoolsRow({ projectId }: { projectId: string }) {
  const [pools, setPools] = useState<Pool[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await readPools(projectId);
    if (r.ok && "data" in r && r.data) { setPools(r.data); setError(null); }
    else if (!r.ok) setError(r.error === "missing" ? "Not on this install yet." : r.error);
  }, [projectId]);
  useEffect(() => { void load(); }, [load]);

  const act = async (p: Promise<{ ok: boolean; error: string | null }>, done: string) => {
    const r = await p;
    setSaid(r.ok ? done : r.error ?? "Not saved.");
    if (r.ok) void load();
  };

  const missing = ENGINES.filter((e) => !(pools ?? []).some((p) => p.engine === e));
  return (
    <section className="su-row" data-testid="su-row-pools" aria-label="Account pools">
      <div className="su-row-label">
        <SectionLabel as="span" tip="Several Claude or Codex subscriptions used as one pool.">Account pools</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        {error ? <div className="su-faint" data-testid="pools-error">{error}</div> : null}
        {(pools ?? []).map((pool) => (
          <div key={pool.id} className="su-stack" data-testid={`pool-${pool.engine}`}>
            <strong title={`${pool.members.length} accounts; each run takes the one with most room`}>
              {ENGINE_WORDS[pool.engine]} pool</strong>
            {pool.members.map((m) => (
              <div key={m.id} className="su-line" data-testid={`pool-member-${m.id}`} data-status={m.status}>
                <span>{m.label}</span>{" "}
                <span className="su-faint" data-testid={`pool-room-${m.id}`}
                      title={m.room?.resets_at ? `Resets ${m.room.resets_at}` : "Nothing reported yet"}>
                  {m.status === "needs_reauth" ? "Needs a new login" : m.status === "disabled" ? "Paused"
                    : ROOM_WORDS[m.room?.state ?? "plenty"] ?? "Plenty"}</span>
                <span className="su-line su-actions">
                  <GatedButton permission="entitlement:manage" projectId={projectId} className="su-link"
                               data-testid={`pool-member-toggle-${m.id}`}
                               onClick={() => void act(updatePoolMember(projectId, m.id, { status: m.status === "active" ? "disabled" : "active" },
                                                                        m.status === "active" ? "Pause" : "Resume"),
                                                       m.status === "active" ? "Paused." : "Back in rotation.")}>
                    {m.status === "active" ? "Pause" : "Resume"}</GatedButton>
                  <GatedButton permission="entitlement:manage" projectId={projectId} className="su-link"
                               data-testid={`pool-member-remove-${m.id}`}
                               onClick={() => void act(removePoolMember(projectId, m.id), "Removed, with its login.")}>Remove</GatedButton>
                </span>
              </div>
            ))}
            <AddAccount projectId={projectId} pool={pool} onAdded={() => { setSaid("Account added."); void load(); }} />
          </div>
        ))}
        {said ? <div role="status" data-testid="pools-said">{said}</div> : null}
        {missing.map((engine) => (
          <div key={engine} className="su-line">
            <GatedButton permission="entitlement:manage" projectId={projectId} className="su-link"
                         data-testid={`pools-start-${engine}`}
                         onClick={() => void act(createPool(projectId, engine), `${ENGINE_WORDS[engine]} pool started.`)}>
              Start a {ENGINE_WORDS[engine]} pool</GatedButton>
          </div>
        ))}
      </div>
    </section>
  );
}

function AddAccount({ projectId, pool, onAdded }: { projectId: string; pool: Pool; onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [login, setLogin] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    const r = await addPoolMember(projectId, pool.id, label.trim(), login);
    setLogin("");
    if (r.ok) { setOpen(false); setLabel(""); onAdded(); } else setErr(r.error ?? "Not saved.");
  };
  if (!open) {
    return (
      <div className="su-line">
        <GatedButton permission="entitlement:manage" projectId={projectId} className="su-link"
                     data-testid={`pool-add-open-${pool.engine}`} onClick={() => setOpen(true)}>Add account</GatedButton>
      </div>
    );
  }
  return (
    <FormGrid label={`Add a ${ENGINE_WORDS[pool.engine]} account`} testId={`pool-add-${pool.engine}`}>
      <Field label="Name"><input aria-label="Account name" value={label} placeholder="whose seat" onChange={(e) => setLabel(e.target.value)} /></Field>
      <Field label="Login" tip={pool.engine === "codex" ? "The account's auth.json" : "The account's long-lived token"}>
        <input aria-label="Account login" type="password" autoComplete="off" value={login}
               onChange={(e) => setLogin(e.target.value)} /></Field>
      <FormError>{err}</FormError>
      <GatedButton permission="entitlement:manage" projectId={projectId} className="su-link"
                   data-testid={`pool-add-save-${pool.engine}`} disabled={!label.trim() || !login}
                   onClick={() => void save()}>Save account</GatedButton>
    </FormGrid>
  );
}
