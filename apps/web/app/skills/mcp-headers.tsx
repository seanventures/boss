"use client";

/**
 * UPSTREAM HEADERS (BOSSAPP-873), in the MCP drawer under Sign in.
 *
 *   Send         [x] Run id  [x] Agent  [ ] Trace
 *   Only send    X-Tenant-Key, X-Region        (empty: no allowlist)
 *   Secret       X-Tenant-Key ••••0042  [Remove]
 *                [name] [value]                [Save]
 *
 * Secret header values go to the secret store and never come back to the browser (last four
 * only). A saved policy replaces the old one; kept secrets are sent back as {keep: true}.
 */
import React, { useCallback, useEffect, useState } from "react";

import { CheckGroup, CheckRow, Field, FormGrid } from "../../components/form-kit";
import { GatedButton } from "../../components/gated";
import { headersPolicyInput, mcpApi, type ForwardKey, type HeadersPolicyView } from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";

const FORWARD_WORDS: Record<ForwardKey, string> = { run_id: "Run id", agent: "Agent", traceparent: "Trace" };

export function HeadersSection({ projectId, actor, serverId }: { projectId: string; actor: string; serverId: string }) {
  const [view, setView] = useState<HeadersPolicyView | null>(null);
  const [forward, setForward] = useState<ForwardKey[]>([]);
  const [allow, setAllow] = useState("");
  const [remove, setRemove] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await mcpApi.headersPolicy(projectId, serverId);
    if (!r.ok || !r.data?.headers_policy) return;
    setView(r.data);
    setForward(r.data.headers_policy.forward ?? []);
    setAllow((r.data.headers_policy.allow ?? []).join(", "));
    setRemove([]); setName(""); setValue("");
  }, [projectId, serverId]);
  useEffect(() => { void load(); }, [load]);
  if (!view) return null;

  const policy = view.headers_policy;
  const save = async () => {
    setBusy(true);
    const r = await mcpApi.setHeadersPolicy(projectId, actor, serverId, headersPolicyInput(policy, {
      allow, forward, remove, add: name.trim() && value ? { name, value } : null }));
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", "Headers saved");
    await load();
  };
  const toggle = (k: ForwardKey) => setForward((f) => (f.includes(k) ? f.filter((x) => x !== k) : [...f, k]));

  return (
    <div className="kit-signin" data-testid="kit-mcp-headers">
      {/* The section says what it is (the walker met an unnamed block of controls, BOSSAPP-1039). */}
      <h4 className="kit-signin-head">Upstream headers</h4>
      {Object.entries(policy.secret_headers).map(([n, s]) => (
        <div key={n} className="kit-field" data-testid={`kit-mcp-headers-secret-${n}`}>
          <span className="kit-field-label" data-tip="A header whose value is kept in the secret store">Secret</span>
          <span className="kit-mono" data-user-text>{n} {s.last4 ? `••••${s.last4}` : "set"}</span>
          <button type="button" className="kit-link" aria-pressed={remove.includes(n)} data-testid={`kit-mcp-headers-remove-${n}`}
                  onClick={() => setRemove((r) => (r.includes(n) ? r.filter((x) => x !== n) : [...r, n]))}>
            {remove.includes(n) ? "Keep" : "Remove"}
          </button>
        </div>
      ))}
      {/* The one form kit (BOSSAPP-1039): every control in the section is a kit field, the Send
          boxes sit ON their words, never ~130px away. */}
      <FormGrid label="Upstream headers">
        <CheckGroup label="Send" tip="Tag each call so the server's own audit can be joined to BOSS's">
          {view.forward_choices.map((c) => (
            <CheckRow key={c.key} label={FORWARD_WORDS[c.key]} tip={`Adds the ${c.header} header to each call`}
                      checked={forward.includes(c.key)} testId={`kit-mcp-headers-forward-${c.key}`}
                      onChange={() => toggle(c.key)} />
          ))}
        </CheckGroup>
        <Field label="Only send" wide tip="Only these extra headers go upstream; the sign-in always does. Empty: no limit">
          <input type="text" value={allow} spellCheck={false} placeholder="Any header" data-testid="kit-mcp-headers-allow"
                 onChange={(e) => setAllow(e.target.value)} />
        </Field>
        <Field label="Secret header" tip="Add a header whose value is a secret">
          <input type="text" value={name} placeholder="X-Tenant-Key" aria-label="Header name" data-testid="kit-mcp-headers-name"
                 onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Value">
          <input type="password" value={value} autoComplete="new-password" aria-label="Header value" data-testid="kit-mcp-headers-value"
                 onChange={(e) => setValue(e.target.value)} />
        </Field>
        <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm" disabled={busy}
                     data-testid="kit-mcp-headers-save" onClick={() => void save()}>Save</GatedButton>
      </FormGrid>
    </div>
  );
}
