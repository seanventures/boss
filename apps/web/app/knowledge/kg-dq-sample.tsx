"use client";

/**
 * KNOWLEDGE GRAPH › DOCUMENTS › DQ SAMPLE (rulebook source G7). One row: "Load the NorthPeak Bank
 * rulebook" stages the shipped Loan Data Quality Rulebook v3.2 PDF through the SAME upload path a
 * person's own file takes (POST /v1/knowledge/samples/dq-rulebook → the preview → "Add the
 * document"); once it is in the graph, "Extract rules" has the Rulebook Analyst propose its rules
 * for Rules review. The row says where it stands: not loaded, waiting for your confirmation, or
 * loaded with how many rules are proposed and approved.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  DOCUMENT_MANAGE, getDqRulebookSample, getUpload, stageDqRulebook,
  type KgDqRulebookSample, type KgUploadPreview,
} from "../../lib/kg-view";
import { ExtractRules, UploadPreview } from "./kg-upload";

export function DqRulebookSample({ projectId, actor, orgId, archived = false, onAdded }: {
  projectId: string; actor: string; orgId?: string | null; archived?: boolean; onAdded?: () => void;
}) {
  const w = useMemo(() => ({ projectId, actor }), [projectId, actor]);
  const [sample, setSample] = useState<KgDqRulebookSample | null>(null);
  const [preview, setPreview] = useState<KgUploadPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const read = useCallback(async () => {
    const r = await getDqRulebookSample(w);
    if (r.ok) setSample(r.data);
  }, [w]);
  useEffect(() => { void read(); }, [read]);

  const load = async () => {
    setBusy(true); setMsg(null);
    const r = sample?.pending_upload_id ? await getUpload(w, sample.pending_upload_id) : await stageDqRulebook(w);
    setBusy(false);
    if (!r.ok) { setMsg({ ok: false, text: r.error }); return; }
    setPreview(r.data);
  };

  if (!sample || !sample.available) return null;
  const loaded = !!sample.document_id;
  const state = loaded
    ? `Loaded: ${sample.sections} sections, ${sample.rules.proposed} rules waiting for review, ${sample.rules.approved} approved`
    : sample.pending_upload_id ? "Read, waiting for you to check its sections" : "Not loaded";
  return (
    <section className="kg-doc" data-testid="kg-dq-sample" aria-label="DQ sample">
      <div className="kg-doc-row">
        <span className="kg-doc-pack-label">DQ sample</span>
        <span className="kg-doc-pack-value" data-user-text>{sample.title}</span>
        <span className="kg-quiet" data-testid="kg-dq-sample-state">{state}</span>
        {!preview ? (
          <GatedButton permission={DOCUMENT_MANAGE} className="ag-btn" data-testid="kg-dq-load" disabled={archived || busy}
                       title="Read the bank's 45-page rulebook PDF and propose its sections for you to check"
                       onClick={() => void load()}>
            {busy ? "Reading…" : loaded ? "Load it again" : sample.pending_upload_id ? "Check its sections" : "Load the NorthPeak Bank rulebook"}
          </GatedButton>
        ) : null}
        {loaded && sample.document_id ? (
          <ExtractRules w={w} documentId={sample.document_id} title={sample.title} orgId={orgId} archived={archived}
                        disabledWhy={sample.can_extract ? null : sample.extract_blocked_reason} />
        ) : null}
        {msg ? <span className={msg.ok ? "kg-ok" : "kg-inline-err"} role={msg.ok ? "status" : "alert"} data-testid="kg-dq-msg">{msg.text}</span> : null}
      </div>
      {preview ? (
        <UploadPreview w={w} preview={preview} archived={archived} onDone={(added) => {
          setPreview(null);
          setMsg(added ? { ok: true, text: `Added “${added.title}”: ${added.sections} sections, ${added.rule_cards} of them rule cards. Extract rules next.` }
                       : { ok: true, text: "Discarded: nothing was added." });
          void read();
          onAdded?.();
        }} />
      ) : null}
    </section>
  );
}
