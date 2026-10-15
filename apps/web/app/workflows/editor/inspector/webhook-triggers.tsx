"use client";

import React, { useCallback, useEffect, useState } from "react";

import { useGate } from "../../../../components/gated";
import { TOAST_EVENT } from "../../../../lib/api-write";
import { formatTime } from "../../../../lib/datetime";
import {
  createWebhook, curlExample, deliveryLabel, deliveryTone, listDeliveries, listWebhooks,
  rotateWebhook, setWebhookEnabled, type WebhookDelivery, type WebhookTrigger,
} from "../../../../lib/workflow-webhooks";
import { Section } from "./step-inspector";

function toast(type: "success" | "error", message: string) {
  try { window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { type, message } })); } catch { /* */ }
}

async function copy(text: string, what: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast("success", `${what} copied`);
  } catch {
    toast("error", "Copy refused by the browser");
  }
}

/**
 * The Trigger panel's webhook half: add a trigger, copy its URL, see the secret ONCE (at creation
 * and at rotation), turn it off and on, and read its recent deliveries with each refusal's reason.
 *
 * The secret is held in this component's state only until Done; nothing re-reads it, because the
 * API never returns it again. Explanations live in tooltips (`title`), not paragraphs.
 */
export function WebhookTriggers({ projectId, workflowKey, actor }: {
  projectId: string; workflowKey: string; actor?: string;
}) {
  const [hooks, setHooks] = useState<WebhookTrigger[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [revealed, setRevealed] = useState<{ id: string; secret: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [howTo, setHowTo] = useState<string | null>(null);
  const gate = useGate("workflow_schedule:manage", projectId);

  const load = useCallback(async () => {
    const r = await listWebhooks(projectId, workflowKey);
    if (r.ok) { setHooks(r.data.webhooks ?? []); setError(""); } else setError(r.error);
  }, [projectId, workflowKey]);

  useEffect(() => { void load(); }, [load]);

  const add = async () => {
    setBusy(true);
    const r = await createWebhook(projectId, workflowKey, `${workflowKey} webhook ${(hooks?.length ?? 0) + 1}`, actor);
    setBusy(false);
    if (!r.ok) { toast("error", r.error); return; }
    setRevealed({ id: r.data.id, secret: r.data.secret });
    await load();
  };

  const rotate = async (hook: WebhookTrigger) => {
    if (!window.confirm(`Rotate the secret of "${hook.name}"? The old one stops working now.`)) return;
    setBusy(true);
    const r = await rotateWebhook(projectId, hook.id, actor);
    setBusy(false);
    if (!r.ok) { toast("error", r.error); return; }
    setRevealed({ id: hook.id, secret: r.data.secret });
    await load();
  };

  const toggle = async (hook: WebhookTrigger) => {
    setBusy(true);
    const r = await setWebhookEnabled(projectId, hook.id, !hook.enabled, actor);
    setBusy(false);
    if (!r.ok) { toast("error", r.error); return; }
    await load();
  };

  const canWrite = gate.allowed && !busy;
  return (
    <Section label="Webhooks" testId="wf-webhooks"
             tip="Another system starts this workflow by POSTing signed JSON to the trigger's URL. The body becomes the run's input.">
      {error ? <span className="wf-field-error" role="alert" title={error}>Couldn't load webhooks</span> : null}
      {hooks && hooks.length === 0 ? <span className="wf-faint">No webhooks</span> : null}
      {(hooks ?? []).map((hook) => (
        <div key={hook.id} className="wf-hook" data-testid={`wf-webhook-${hook.id}`}>
          <div className="wf-row">
            <span className="wf-mono wf-hook-name" data-user-text title={hook.path}>{hook.name}</span>
            <span className={hook.enabled ? "wf-hook-on" : "wf-faint"}>{hook.enabled ? "On" : "Off"}</span>
          </div>
          {!hook.enabled && hook.last_error ? (
            <span className="wf-field-error" role="status" title={hook.last_error}
                  data-testid="wf-webhook-paused">Paused: renew authority</span>
          ) : null}
          <div className="wf-row wf-hook-actions">
            <button type="button" className="wf-btn wf-btn-sm" title="Path on your BOSS API's public address"
                    data-testid="wf-webhook-copy-url" onClick={() => void copy(hook.path, "Webhook path")}>Copy URL</button>
            <button type="button" {...gate.props} className="wf-btn wf-btn-sm" disabled={!canWrite}
                    data-testid="wf-webhook-toggle" onClick={() => void toggle(hook)}>{hook.enabled ? "Turn off" : "Turn on"}</button>
            <button type="button" title="New secret; the old one stops working at once" {...gate.props}
                    className="wf-btn wf-btn-sm wf-btn-danger" disabled={!canWrite}
                    data-testid="wf-webhook-rotate" onClick={() => void rotate(hook)}>Rotate secret</button>
            <button type="button" className="wf-btn wf-btn-sm" aria-pressed={open === hook.id}
                    data-testid="wf-webhook-deliveries" onClick={() => setOpen(open === hook.id ? null : hook.id)}>Deliveries</button>
            <button type="button" className="wf-btn wf-btn-sm" aria-pressed={howTo === hook.id}
                    data-testid="wf-webhook-howto" onClick={() => setHowTo(howTo === hook.id ? null : hook.id)}>How to sign</button>
          </div>
          {revealed?.id === hook.id ? (
            <div className="wf-hook-secret" data-testid="wf-webhook-secret">
              <span className="wf-label" title="BOSS keeps it in the secret store and never shows it again">Copy now: shown once</span>
              <div className="wf-row">
                <input className="wf-input wf-mono" readOnly value={revealed.secret} aria-label="Signing secret"
                       onFocus={(e) => e.currentTarget.select()} />
                <button type="button" className="wf-btn wf-btn-sm" onClick={() => void copy(revealed.secret, "Secret")}>Copy</button>
                <button type="button" className="wf-btn wf-btn-sm" data-testid="wf-webhook-secret-done"
                        onClick={() => setRevealed(null)}>Done</button>
              </div>
            </div>
          ) : null}
          {howTo === hook.id ? (
            <textarea className="wf-text wf-mono wf-hook-curl" readOnly rows={10} aria-label="Sample curl"
                      data-testid="wf-webhook-curl" value={curlExample(hook.path)} />
          ) : null}
          {open === hook.id ? <Deliveries projectId={projectId} hook={hook} /> : null}
        </div>
      ))}
      <button type="button" {...gate.props} className="wf-link" disabled={!canWrite}
              data-testid="wf-webhook-add" onClick={() => void add()}>+ Webhook</button>
    </Section>
  );
}

function Deliveries({ projectId, hook }: { projectId: string; hook: WebhookTrigger }) {
  const [items, setItems] = useState<WebhookDelivery[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    void listDeliveries(projectId, hook.id).then((r) => {
      if (!live) return;
      if (r.ok) setItems(r.data.deliveries ?? []); else setError(r.error);
    });
    return () => { live = false; };
  }, [projectId, hook.id]);
  if (error) return <span className="wf-field-error" role="alert" title={error}>Couldn't load deliveries</span>;
  if (!items) return <span className="wf-faint">Loading…</span>;
  return (
    <ul className="wf-hook-deliveries" data-testid="wf-webhook-delivery-list">
      {items.length === 0 ? <li className="wf-faint">No deliveries yet</li> : null}
      {items.map((d) => (
        <li key={d.id} className="wf-row" data-tone={deliveryTone(d)} data-testid="wf-webhook-delivery">
          <span className="wf-mono wf-faint" title={d.received_at}>{formatTime(d.received_at)}</span>
          <span className="wf-hook-status" title={d.delivery_id ?? undefined}>{deliveryLabel(d)}</span>
          {d.run_id ? <a className="wf-link" href={`?area=delivery_workflows&wfr=${encodeURIComponent(d.run_id)}`}>Run</a> : null}
        </li>
      ))}
      {hook.refused_unrecorded ? (
        <li className="wf-faint" title="Refusals past 30 a minute are counted, not listed">
          +{hook.refused_unrecorded} more refused
        </li>
      ) : null}
    </ul>
  );
}
