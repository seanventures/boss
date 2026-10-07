"use client";

/**
 * Connection (spec §5.3 1): the account or key an ability needs, with Save & test.
 *
 * SAVE & TEST IS ATOMIC, server side: the API tests the values typed here and stores them only
 * if the test passes. A failure comes back with the field the provider named, and its short
 * reason lands under that field. A SECRET IS NEVER PREFILLED: a stored one shows as
 * `Set · …a1f3` with `Replace`, and its value never reaches this page.
 */
import React, { useEffect, useMemo, useState } from "react";
import { ArrowSquareOut, CheckCircle, XCircle } from "@phosphor-icons/react";

import type { Ability, Provider, ProviderField } from "../../lib/kit-types";
import { kitApi } from "../../lib/kit-api";
import { GatedButton } from "../../components/gated";
import { showToast } from "../client-forms";
import { REPO_HREF } from "./ability-card";
import { shortError, TIP } from "./kit-copy";
import { YoutubeConnection } from "./youtube-connection";

/** "Tested just now" / "Tested 2m ago", computed after mount (no clock read during render, so
 *  the server's HTML and the first client render agree) and kept fresh. */
function TestedAge({ at }: { at: string }) {
  const [label, setLabel] = useState("Tested");
  useEffect(() => {
    const tick = () => {
      const s = Math.max(0, (Date.now() - Date.parse(at)) / 1000);
      setLabel(Number.isNaN(s) ? "Tested" : s < 60 ? "Tested just now" : s < 3600 ? `Tested ${Math.floor(s / 60)}m ago`
        : s < 86400 ? `Tested ${Math.floor(s / 3600)}h ago` : `Tested ${Math.floor(s / 86400)}d ago`);
    };
    tick();
    const id = window.setInterval(tick, 30_000);
    return () => window.clearInterval(id);
  }, [at]);
  return <>{label}</>;
}

export function TestRow({ test }: { test: Ability["last_test"] }) {
  if (!test) return null;
  return (
    <div className="kit-testrow" data-ok={test.ok} data-testid="kit-test-row">
      {test.ok ? <CheckCircle className="kit-ok" size={16} weight="fill" /> : <XCircle className="kit-bad" size={16} weight="fill" />}
      <span data-tip={test.ok ? undefined : test.error ?? undefined}>
        {test.ok ? <TestedAge at={test.at} /> : shortError(test.error)}
      </span>
      <span className="kit-faint">{test.latency_ms} ms</span>
      {test.ok && test.fact ? <span className="kit-fact" data-tip={test.fact}>{test.fact}</span> : null}
    </div>
  );
}

function Field({ field, value, stored, error, replacing, onChange, onReplace }: {
  field: ProviderField; value: string; stored: string | undefined; error: string | null;
  replacing: boolean; onChange: (v: string) => void; onReplace: () => void;
}) {
  const id = `kit-field-${field.name}`;
  if (field.options?.length) {
    // A CHOICE, NOT TEXT (K7): Email's TLS is None · STARTTLS · SSL, so a plain relay on port 25
    // or 1025 (Mailpit, postfix) connects, where STARTTLS-or-nothing refused it.
    return (
      <div className="kit-field" data-testid={id}>
        <span className="kit-field-label">{field.label}</span>
        <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label={field.label} data-testid={`${id}-options`}>
          {field.options.map((o) => (
            <button key={o.value} type="button" role="radio" aria-checked={value === o.value}
                    data-testid={`${id}-${o.value}`} onClick={() => onChange(o.value)}>{o.label}</button>
          ))}
        </span>
        {error ? <span className="kit-field-err" role="alert" data-testid={`${id}-error`} data-tip={error}>{shortError(error)}</span> : null}
      </div>
    );
  }
  if (field.secret && stored && !replacing) {
    return (
      <div className="kit-field" data-testid={id}>
        <span className="kit-field-label">{field.label}</span>
        <span className="kit-secret-set" data-testid={`${id}-set`}>Set · {stored}</span>
        <button type="button" className="kit-link" data-testid={`${id}-replace`} onClick={onReplace}>Replace</button>
      </div>
    );
  }
  return (
    <label className="kit-field" data-testid={id}>
      <span className="kit-field-label">{field.label}</span>
      <input type={field.secret ? "password" : "text"} value={value} autoComplete={field.secret ? "new-password" : "off"}
             spellCheck={false} placeholder={field.placeholder ?? ""} aria-invalid={error ? true : undefined}
             data-testid={`${id}-input`} onChange={(e) => onChange(e.target.value)} />
      {error ? <span className="kit-field-err" role="alert" data-testid={`${id}-error`} data-tip={error}>{shortError(error)}</span> : null}
    </label>
  );
}

/** An option field's value when nothing is chosen or stored. Security follows the port: SSL on
 *  465, none on 1025 (Mailpit's), else STARTTLS; any other choice its declared default. */
export function optionDefault(f: ProviderField, values: Record<string, string>, stored: Record<string, string>): string {
  if (!f.options?.length) return "";
  const has = (v: string) => f.options!.some((o) => o.value === v);
  if (f.name === "security") {
    const port = (values.port ?? stored.port ?? "").trim();
    if (port === "465" && has("ssl")) return "ssl";
    if (port === "1025" && has("none")) return "none";
    if (!f.default && has("starttls")) return "starttls";
  }
  return f.default ?? f.options[0].value;
}

export function ConnectionSection({ ability, projectId, actor, onChanged }: {
  ability: Ability; projectId: string; actor: string; onChanged: (a: Ability) => void;
}) {
  const conn = ability.connection;
  const providers: Provider[] = conn?.providers ?? [];
  const [provider, setProvider] = useState<string>(conn?.provider ?? providers[0]?.key ?? "");
  const [values, setValues] = useState<Record<string, string>>({});
  const [replacing, setReplacing] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<{ field: string | null; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => { setProvider(conn?.provider ?? providers[0]?.key ?? ""); }, [conn?.provider]); // eslint-disable-line react-hooks/exhaustive-deps

  const chosen = useMemo(() => providers.find((p) => p.key === provider) ?? providers[0], [providers, provider]);
  const stored = conn && conn.provider === provider ? conn.set_fields : {};

  if (!conn) {
    if (ability.status === "needs_repo") {
      return (
        <div className="kit-row" data-testid="kit-conn-repo">
          <span>Needs repo</span>
          <a className="kit-btn kit-btn-primary kit-btn-sm" href={REPO_HREF} data-tip={TIP.needsRepo}>Connect repo ↗</a>
        </div>
      );
    }
    if (ability.connect_href) {
      return (
        <div className="kit-row" data-testid="kit-conn-elsewhere">
          <span>{ability.status === "setup" ? "Set up" : "Connected"}</span>
          <a className="kit-btn kit-btn-sm" href={ability.connect_href}>Connect <ArrowSquareOut size={12} /></a>
        </div>
      );
    }
    return (
      <div className="kit-row" data-testid="kit-conn-none">
        <span data-tip={TIP.builtIn}>Built in · Nothing to connect</span>
        <TestRow test={ability.last_test} />
      </div>
    );
  }

  if (conn.flow === "channels") {
    return <YoutubeConnection projectId={projectId} actor={actor} />;
  }

  const save = async () => {
    if (!chosen) return;
    setBusy(true); setError(null);
    const fields: Record<string, string> = {};
    for (const f of chosen.fields) {
      const v = (values[f.name] ?? "").trim();
      if (f.secret) { if (v) fields[f.name] = v; }
      else fields[f.name] = v || (stored[f.name] ?? optionDefault(f, values, stored));
    }
    const r = await kitApi.connect(projectId, actor, ability.key, chosen.key, fields);
    setBusy(false);
    if (!r.ok) {
      setError({ field: r.field ?? null, message: r.error });
      showToast("error", `Test failed · ${shortError(r.error)}`);
      return;
    }
    setValues({}); setReplacing({});
    showToast("success", `Connected · ${r.data.last_test?.fact ?? chosen.label}`);
    onChanged(r.data);
  };

  const disconnect = async () => {
    setConfirm(false); setBusy(true);
    const r = await kitApi.disconnect(projectId, actor, ability.key);
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Disconnected ${chosen?.label ?? ability.label}`);
    onChanged(r.data);
  };

  // WHAT IS SHOWN IS THE STORED PROVIDER'S (K10): its Acts as and its last test belong to it, so
  // picking another provider's tab shows that provider's empty form, not GitHub's result.
  const onStored = conn.provider === chosen?.key;
  const reused = chosen?.reuses === "github";
  // Something stored to forget. Keyless Exa with nothing stored is the default, not a
  // connection a person made.
  const canDisconnect = Boolean(conn.connected) && !(
    Object.keys(conn.set_fields).length === 0 && !conn.acts_as
    && providers.find((p) => p.key === conn.provider)?.keyless);
  return (
    <div className="kit-conn" data-testid="kit-connection">
      {providers.length > 1 ? (
        <div className="kit-seg" role="radiogroup" aria-label="Provider" data-testid="kit-provider">
          {providers.map((p) => (
            <button key={p.key} type="button" role="radio" aria-checked={p.key === chosen?.key}
                    data-testid={`kit-provider-${p.key}`}
                    onClick={() => { setProvider(p.key); setError(null); }}>{p.label}</button>
          ))}
        </div>
      ) : null}
      {reused ? (
        <div className="kit-row" data-testid="kit-conn-reuses">
          <span data-tip="GitHub Issues uses the repo connected in Setup">Uses your repo</span>
          {ability.status === "needs_repo" ? <a className="kit-link" href={REPO_HREF}>Connect repo ↗</a> : null}
        </div>
      ) : null}
      {(chosen?.fields ?? []).map((f) => (
        <Field key={f.name} field={f} value={values[f.name] ?? (f.secret ? "" : (stored[f.name] ?? optionDefault(f, values, stored)))}
               stored={f.secret ? stored[f.name] : undefined}
               error={error && (error.field === f.name) ? error.message : null}
               replacing={Boolean(replacing[f.name])}
               onReplace={() => setReplacing((r) => ({ ...r, [f.name]: true }))}
               onChange={(v) => setValues((s) => ({ ...s, [f.name]: v }))} />
      ))}
      {error && !(chosen?.fields ?? []).some((f) => f.name === error.field) ? (
        <div className="kit-err" role="alert" data-testid="kit-conn-error" data-tip={error.message}>{shortError(error.message)}</div>
      ) : null}
      {chosen?.token_url ? (
        <a className="kit-link" href={chosen.token_url} target="_blank" rel="noreferrer noopener"
           data-tip={chosen.scopes_tip || ability.scopes_tip ? `Needs scopes: ${chosen.scopes_tip ?? ability.scopes_tip}` : undefined}>
          Get a token ↗
        </a>
      ) : null}
      {onStored && conn.acts_as ? (
        <div className="kit-row" data-testid="kit-acts-as">
          <span className="kit-field-label" data-tip={TIP.actsAs}>Acts as</span>
          <span className="kit-fact" data-tip={conn.acts_as}>{conn.acts_as}</span>
        </div>
      ) : null}
      {onStored ? <TestRow test={ability.last_test} /> : null}
      <div className="kit-actions">
        <GatedButton permission="integration:manage" className="kit-btn kit-btn-primary" disabled={busy || (reused && ability.status === "needs_repo")}
                data-testid="kit-save-test" onClick={() => void save()}>
          {busy ? "Testing…" : "Save & test"}
        </GatedButton>
        {canDisconnect && onStored ? (
          <span className="kit-confirm-wrap">
            <GatedButton permission="integration:manage" className="kit-btn" disabled={busy} data-testid="kit-disconnect"
                    onClick={() => setConfirm(true)}>Disconnect</GatedButton>
            {confirm ? (
              <span className="kit-pop" role="dialog" aria-label="Confirm disconnect" data-testid="kit-disconnect-confirm">
                <strong>Disconnect {chosen?.label}?</strong>
                <span>Agents lose {ability.label}</span>
                <span className="kit-pop-actions">
                  <button type="button" className="kit-btn kit-btn-danger kit-btn-sm" data-testid="kit-disconnect-yes"
                          onClick={() => void disconnect()}>Disconnect</button>
                  <button type="button" className="kit-btn kit-btn-sm" onClick={() => setConfirm(false)}>Cancel</button>
                </span>
              </span>
            ) : null}
          </span>
        ) : null}
      </div>
    </div>
  );
}
