"use client";

/**
 * ADD A TICKET SYSTEM (BOSSAPP-1047, wireframe T2), and the same drawer for a connection's
 * Settings. Four short sections, one main action (Save):
 *
 *   1 · Pick one            provider tiles from GET /v1/ticket-systems/catalog, plus "+ Your
 *                           adapter" (an info tile: docs/infra/TICKET_CONNECTORS.md)
 *   2 · Sign in to <Jira>   the connector's own fields; the token is WRITE-ONLY (never prefilled;
 *                           a stored one shows as "Set, …1234"). Test BOSS's sign-in stores
 *                           nothing unless it passes (the API tests first). Where the catalog
 *                           offers OAuth, "Connect with Atlassian" lets each person act as
 *                           themselves (BOSS falls back to the bot token).
 *   3 · Which tickets go where   mappings (their project → a BOSS project), write-back chips,
 *                           and the pick-up rule: label "boss" → ask me first | start automatically
 *   Advanced                status names, webhook or check every N min (a webhook's URL and
 *                           secret are shown ONCE, with Copy), Rotate.
 *
 * Save connects (with the settings) when nothing is stored yet, else PATCHes only the settings.
 */
import React, { useEffect, useMemo, useState } from "react";
import { ArrowSquareOut, Check, CopySimple, Plus, PuzzlePiece, Trash, WarningCircle } from "@phosphor-icons/react";

import { CheckRow, Field, FormError, FormGrid } from "../../components/form-kit";
import { GatedButton } from "../../components/gated";
import { ProviderMono, TkReadState } from "../tickets/ticket-parts";
import {
  connectTicketSystem, parseConnectAnswer, parseWebhook, readCatalog, readExternalProjects, rotateTicketWebhook,
  saveTicketSettings, startTicketOauth, type CatalogItem, type Connection, type ExternalProject, type InboundMode,
  type Mapping, type PickupMode, type Read, type StatusMap, type WebhookSecret, type WriteBack,
} from "../../lib/tickets";
import { Drawer } from "./drawer";
import { CONNECTORS_DOC } from "./ticket-systems-row";

type Draft = {
  mappings: Mapping[]; write_back: WriteBack; pickup: { label: string; mode: PickupMode };
  inbound: { mode: InboundMode; poll_minutes: number }; status_map: StatusMap;
};
const EMPTY_STATUS: StatusMap = { todo: "", doing: "", review: "", done: "" };
const draftOf = (c: Connection | null): Draft => ({
  mappings: c?.settings.mappings ?? [],
  write_back: c?.settings.write_back ?? { comment: true, transition: true, link_pr: true },
  pickup: c?.settings.pickup ?? { label: "boss", mode: "ask" },
  inbound: c?.settings.inbound ?? { mode: "webhook", poll_minutes: 5 },
  status_map: c?.settings.status_map ?? EMPTY_STATUS,
});
const STATUS_WORDS: [keyof StatusMap, string][] = [["todo", "To do"], ["doing", "In progress"], ["review", "In review"], ["done", "Done"]];

/** What a passing test could do, in one line ("can read 4 projects · can comment and move tickets"). */
function canWords(caps: Record<string, boolean>, keys: string[]): string {
  const bits = [keys.length ? `can read ${keys.length} project${keys.length === 1 ? "" : "s"}` : caps.read ? "can read tickets" : ""];
  const moves = [caps.write_comment ? "comment" : "", caps.transition ? "move tickets" : ""].filter(Boolean);
  if (moves.length) bits.push(`can ${moves.join(" and ")}`);
  return bits.filter(Boolean).join(" · ");
}

function copy(text: string) {
  try { void navigator.clipboard?.writeText(text); } catch { /* no clipboard */ }
}

export function AddTicketSystem({ projectId, connection: given, onClose, onSaved, onChanged }: {
  projectId: string; connection: Connection | null; onClose: () => void; onSaved: (words: string) => void; onChanged: () => void;
}) {
  const [catalog, setCatalog] = useState<Read<CatalogItem[]> | null>(null);
  const [provider, setProvider] = useState(given?.provider ?? "");
  const [conn, setConn] = useState<Connection | null>(given);
  const [values, setValues] = useState<Record<string, string>>(() => ({ ...(given?.fields ?? {}), ...(given?.base_url ? { __base: given.base_url } : {}) }));
  const [secret, setSecret] = useState("");
  const [replacing, setReplacing] = useState(!given);
  const [test, setTest] = useState<{ ok: boolean; line: string } | null>(given?.account ? { ok: true, line: `Signed in as ${given.account}` } : null);
  const [draft, setDraft] = useState<Draft>(() => draftOf(given));
  const [external, setExternal] = useState<ExternalProject[]>([]);
  const [hook, setHook] = useState<WebhookSecret | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<"" | "test" | "save" | "hook" | "oauth">("");
  const [adv, setAdv] = useState(false);

  useEffect(() => { void readCatalog().then(setCatalog); }, []);
  const items = catalog?.ok ? catalog.data : [];
  useEffect(() => { if (!provider && items.length) setProvider(items[0].provider); }, [items, provider]);
  const item = items.find((c) => c.provider === provider) ?? null;
  // Their projects, for the mapping picker, once there is a connection to ask.
  useEffect(() => {
    if (!conn || conn.status !== "connected") return;
    void readExternalProjects(conn.id).then((r) => { if (r.ok) setExternal(r.data); });
  }, [conn]);

  const baseField = item?.base_url_field ?? "";
  const fields = useMemo(() => (item?.auth_fields ?? []).filter((f) => !f.secret && f.name !== baseField && f.name !== "base_url"), [item, baseField]);
  const secretField = item?.auth_fields.find((f) => f.secret) ?? null;
  const hasBase = Boolean(item?.auth_fields.some((f) => f.name === baseField || f.name === "base_url") || baseField);
  const baseMeta = item?.auth_fields.find((f) => f.name === baseField || f.name === "base_url");
  const edit = Boolean(given);
  const set = (name: string, v: string) => setValues((x) => ({ ...x, [name]: v }));
  const patch = (p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p }));

  /** What stops a connect, in words, or null. */
  const missing = (): string | null => {
    if (!item) return "Pick a ticket system.";
    if (hasBase && !(values.__base ?? "").trim()) return `Enter the ${baseMeta?.label ?? "site URL"}${baseMeta?.placeholder ? `, e.g. ${baseMeta.placeholder}` : ""}.`;
    const empty = fields.find((f) => f.required && !(values[f.name] ?? "").trim());
    if (empty) return `Enter the ${empty.label.toLowerCase()}.`;
    if (secretField && !secret && (!conn || replacing)) return `Enter the ${secretField.label.toLowerCase()}.`;
    return null;
  };
  const body = (withSettings: boolean) => ({
    provider, base_url: (values.__base ?? "").trim(),
    fields: Object.fromEntries(fields.map((f) => [f.name, (values[f.name] ?? "").trim()]).filter(([, v]) => v)),
    secrets: secret && secretField ? { [secretField.name]: secret } : {},
    ...(withSettings ? { settings: settingsPatch() } : {}),
  });
  const settingsPatch = () => ({
    mappings: draft.mappings.filter((m) => m.external_project.trim()).map((m) => ({ ...m, external_project: m.external_project.trim() })),
    write_back: draft.write_back, pickup: { label: draft.pickup.label.trim(), mode: draft.pickup.mode },
    inbound: draft.inbound,
    status_map: Object.fromEntries(Object.entries(draft.status_map).filter(([, v]) => v.trim())),
  });

  /** Test BOSS's sign-in: the API tests first and stores only when it passes. */
  const signIn = async () => {
    const why = missing();
    if (why) { setErr(why); return; }
    setErr(null); setBusy("test");
    const r = await connectTicketSystem(projectId, body(false));
    setSecret("");
    setBusy("");
    if (!r.ok) { setTest({ ok: false, line: r.error ?? "The sign-in failed." }); return; }
    const done = parseConnectAnswer(r.body);
    setConn(done.connection); setReplacing(false); onChanged();
    if (!edit) setDraft((d) => ({ ...draftOf(done.connection), pickup: d.pickup, write_back: d.write_back, mappings: d.mappings.length ? d.mappings : done.connection.settings.mappings }));
    setTest(done.test.ok
      ? { ok: true, line: ["Tested", done.test.account ? `signed in as ${done.test.account}` : "", canWords(done.test.capabilities, done.test.project_keys)].filter(Boolean).join(" · ") }
      : { ok: false, line: done.test.message || "The sign-in failed." });
  };

  const save = async () => {
    setErr(null);
    if (!conn) {
      const why = missing();
      if (why) { setErr(why); return; }
      setBusy("save");
      const r = await connectTicketSystem(projectId, body(true));
      setSecret(""); setBusy("");
      if (!r.ok) { setErr(r.error ?? "Not connected."); return; }
      onSaved(`${item?.label ?? "Ticket system"} connected.`);
      return;
    }
    setBusy("save");
    const r = await saveTicketSettings(conn.id, settingsPatch());
    setBusy("");
    if (!r.ok) { setErr(r.error ?? "Not saved."); return; }
    onSaved(`${conn.label} saved.`);
  };

  const webhook = async () => {
    if (!conn) return;
    setBusy("hook"); setErr(null);
    const r = await rotateTicketWebhook(conn.id);
    setBusy("");
    if (!r.ok) { setErr(r.error ?? "No webhook was made."); return; }
    setHook(parseWebhook(r.body));
  };
  const oauth = async () => {
    if (!conn) return;
    setBusy("oauth"); setErr(null);
    const r = await startTicketOauth(conn.id);
    setBusy("");
    const url = typeof (r.body as { authorize_url?: unknown } | null)?.authorize_url === "string" ? (r.body as { authorize_url: string }).authorize_url : "";
    if (!r.ok || !url) { setErr(r.error ?? "Atlassian sign-in did not start."); return; }
    try { window.open(url, "_blank", "noopener"); } catch { /* blocked */ }
  };

  const projectOptions = external.length ? external.map((p) => ({ key: p.key, label: `${p.key} · ${p.name}` }))
    : (conn?.settings.project_keys ?? []).map((k) => ({ key: k, label: k }));

  return (
    <Drawer title={edit ? `${given!.label} settings` : "Add a ticket system"} onClose={onClose} testid="tk-drawer" wide>
      <div className="tk-drawer">
        {!edit ? (
          <section aria-labelledby="tk-s1">
            <h3 id="tk-s1" className="tk-step">1 · Pick one</h3>
            <TkReadState read={catalog} what="Ticket systems" onRetry={() => { setCatalog(null); void readCatalog().then(setCatalog); }} testid="tk-catalog" />
            <div className="tk-tiles" role="radiogroup" aria-label="Ticket system">
              {items.map((c) => (
                <button key={c.provider} type="button" role="radio" aria-checked={provider === c.provider} className="tk-tile"
                        data-testid={`tk-tile-${c.provider}`} title={c.description}
                        onClick={() => { setProvider(c.provider); setTest(null); setErr(null); setValues({}); setSecret(""); }}>
                  <ProviderMono provider={c.provider} size={24} />
                  <span><b>{c.label}</b><small>{c.description.split(/[—.]/)[0].trim()}</small></span>
                </button>
              ))}
              {catalog?.ok ? (
                <a className="tk-tile" data-info href={CONNECTORS_DOC} target="_blank" rel="noreferrer noopener" data-testid="tk-tile-yours"
                   title="ServiceNow, Salesforce or your own: one adapter file and a manifest">
                  <span className="tk-mono" aria-hidden style={{ width: 24, height: 24 }}><PuzzlePiece size={14} /></span>
                  <span><b>+ Your adapter</b><small>ServiceNow, Salesforce… <ArrowSquareOut size={10} aria-hidden /></small></span>
                </a>
              ) : null}
            </div>
          </section>
        ) : null}

        {item || edit ? (
          <section aria-labelledby="tk-s2">
            <h3 id="tk-s2" className="tk-step">{edit ? "Sign-in" : `2 · Sign in to ${item?.label ?? ""}`}</h3>
            <FormGrid label={`Sign in to ${item?.label ?? given?.label ?? "the ticket system"}`} testId="tk-signin">
              {hasBase ? (
                <Field label={baseMeta?.label ?? "Site"} wide>
                  <input aria-label={baseMeta?.label ?? "Site"} value={values.__base ?? ""} placeholder={baseMeta?.placeholder || "https://"}
                         onChange={(e) => set("__base", e.target.value)} data-testid="tk-field-base" />
                </Field>
              ) : null}
              {fields.map((f) => (
                <Field key={f.name} label={`${f.label}${f.required ? "" : " (optional)"}`} tip={f.help || undefined}>
                  <input aria-label={f.label} value={values[f.name] ?? ""} placeholder={f.placeholder} onChange={(e) => set(f.name, e.target.value)}
                         data-testid={`tk-field-${f.name}`} />
                </Field>
              ))}
              {secretField ? (conn && !replacing ? (
                <Field label={secretField.label} tip="Stored in BOSS's vault; never shown again">
                  <input aria-label={secretField.label} value={`Set${conn.last4 ? `, …${conn.last4}` : ""}`} readOnly data-testid="tk-secret-set" />
                </Field>
              ) : (
                <Field label={secretField.label} tip={secretField.help || "Write-only: stored in BOSS's vault, never shown again"}>
                  <input aria-label={secretField.label} type="password" autoComplete="off" value={secret} placeholder={secretField.placeholder}
                         onChange={(e) => setSecret(e.target.value)} data-testid="tk-secret" />
                </Field>
              )) : null}
              <div className="fk-wide tk-row">
                <GatedButton permission="integration:manage" projectId={projectId} className="su-btn su-btn-sm" data-testid="tk-signin-test"
                             disabled={busy === "test"} title="BOSS tests the sign-in first; nothing is stored unless it passes"
                             onClick={() => void signIn()}>{busy === "test" ? "Testing…" : conn && !replacing ? "Test again" : "Test sign-in"}</GatedButton>
                {conn && !replacing && secretField ? (
                  <button type="button" className="su-link" onClick={() => setReplacing(true)} data-testid="tk-secret-replace">Replace {secretField.label.toLowerCase()}</button>
                ) : null}
                {secretField?.help ? <span className="tk-faint" title={secretField.help}>Where to get one</span> : null}
              </div>
            </FormGrid>
            {test ? (
              <div className={test.ok ? "tk-ok tk-test" : "tk-error tk-test"} role="status" data-testid="tk-test-result">
                {test.ok ? <Check size={13} weight="bold" aria-hidden /> : <WarningCircle size={13} aria-hidden />} {test.line}</div>
            ) : null}
            {item?.oauth ? (
              <div className="tk-oauth" data-testid="tk-oauth">
                <GatedButton permission="connection:use_own" projectId={projectId} className="su-btn su-btn-sm" data-testid="tk-oauth-start"
                             disabled={!conn || busy === "oauth"} title={conn ? "Sign in as yourself; BOSS acts as you in Jira" : "Save the bot sign-in first; then each person can connect"}
                             onClick={() => void oauth()}>Connect with Atlassian</GatedButton>
                <span className="tk-faint" title="Each person can also sign in as themselves (Okta SSO works through Atlassian). BOSS falls back to the token above.">Or each person signs in</span>
              </div>
            ) : null}
          </section>
        ) : null}

        {item || edit ? (
          <section aria-labelledby="tk-s3" data-locked={!conn || undefined}>
            <h3 id="tk-s3" className="tk-step">{edit ? "Which tickets go where" : "3 · Which tickets go where"}</h3>
            <div className="tk-maps" data-testid="tk-maps">
              {draft.mappings.map((m, i) => (
                <div key={i} className="tk-map" data-testid={`tk-map-${i}`}>
                  {projectOptions.length ? (
                    <select aria-label={`Their project ${i + 1}`} value={m.external_project} data-testid={`tk-map-from-${i}`}
                            onChange={(e) => patch({ mappings: draft.mappings.map((x, j) => (j === i ? { ...x, external_project: e.target.value } : x)) })}>
                      {!projectOptions.some((p) => p.key === m.external_project) ? <option value={m.external_project}>{m.external_project || "Pick a project"}</option> : null}
                      {projectOptions.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
                    </select>
                  ) : (
                    <input aria-label={`Their project ${i + 1}`} value={m.external_project} placeholder="Project key, e.g. LEND" data-testid={`tk-map-from-${i}`}
                           onChange={(e) => patch({ mappings: draft.mappings.map((x, j) => (j === i ? { ...x, external_project: e.target.value } : x)) })} />
                  )}
                  <span aria-hidden>→</span>
                  <span className="tk-map-to" title="A mapping onto another BOSS project is set from that project's Setup">
                    {m.boss_project_id === projectId ? "This project" : m.boss_project_id}</span>
                  <button type="button" className="su-icon-btn" aria-label={`Remove mapping ${i + 1}`} data-testid={`tk-map-remove-${i}`}
                          onClick={() => patch({ mappings: draft.mappings.filter((_, j) => j !== i) })}><Trash size={13} /></button>
                </div>
              ))}
              {!draft.mappings.length ? <div className="tk-faint" style={{ margin: 0 }} title="Without a mapping, every project this sign-in can see comes to this BOSS project.">All projects come here</div> : null}
              <button type="button" className="su-link" data-testid="tk-map-add"
                      onClick={() => patch({ mappings: [...draft.mappings, { external_project: projectOptions[0]?.key ?? "", boss_project_id: projectId, default: !draft.mappings.length }] })}>
                <Plus size={12} aria-hidden /> Map a project</button>
            </div>
            <div className="tk-row tk-chips" role="group" aria-label="What BOSS writes back">
              <span className="tk-lbl">Writes back</span>
              {([["comment", "Comment progress"], ["transition", "Move status"], ["link_pr", "Link the PR"]] as [keyof WriteBack, string][]).map(([k, label]) => (
                <button key={k} type="button" className="tk-optchip" aria-pressed={draft.write_back[k]} data-testid={`tk-wb-${k}`}
                        title={k === "comment" ? "Comments on the ticket when BOSS starts, opens a PR and deploys" : k === "transition" ? "Moves the ticket To do → In progress → In review → Done" : "Links the pull request on the ticket"}
                        onClick={() => patch({ write_back: { ...draft.write_back, [k]: !draft.write_back[k] } })}>
                  {draft.write_back[k] ? <Check size={11} weight="bold" aria-hidden /> : null}{label}</button>
              ))}
            </div>
            <div className="tk-row tk-pickup">
              <span className="tk-lbl">New tickets</span>
              <label className="tk-inline">Label
                <input aria-label="Pick-up label" value={draft.pickup.label} placeholder="boss" data-testid="tk-pickup-label"
                       onChange={(e) => patch({ pickup: { ...draft.pickup, label: e.target.value } })} /></label>
              <span aria-hidden>→</span>
              <select aria-label="When a ticket gets the label" value={draft.pickup.mode} data-testid="tk-pickup-mode"
                      title="Ask me first: a card in Decisions and the Work tab. Start automatically: BOSS imports and starts it."
                      onChange={(e) => patch({ pickup: { ...draft.pickup, mode: e.target.value as PickupMode } })}>
                <option value="ask">ask me first</option><option value="auto_start">start automatically</option></select>
            </div>

            <details className="tk-adv" open={adv} onToggle={(e) => setAdv((e.currentTarget as HTMLDetailsElement).open)} data-testid="tk-adv">
              <summary data-testid="tk-adv-toggle">Advanced: status names, webhook or check every {draft.inbound.poll_minutes} min</summary>
              <FormGrid label="Advanced ticket settings" testId="tk-adv-form">
                {STATUS_WORDS.map(([k, label]) => (
                  <Field key={k} label={`${label} status`} tip="The status name in the ticket system; empty uses its own category">
                    <input aria-label={`${label} status`} value={draft.status_map[k]} placeholder={item?.status_map_defaults[k] || label}
                           onChange={(e) => patch({ status_map: { ...draft.status_map, [k]: e.target.value } })} data-testid={`tk-status-${k}`} />
                  </Field>
                ))}
                <Field label="New tickets arrive by">
                  <select aria-label="New tickets arrive by" value={draft.inbound.mode} data-testid="tk-inbound-mode"
                          onChange={(e) => patch({ inbound: { ...draft.inbound, mode: e.target.value as InboundMode } })}>
                    {item?.webhook !== false ? <option value="webhook">Webhook (instant)</option> : null}
                    <option value="poll">Check every few minutes</option><option value="off">Off</option></select>
                </Field>
                {draft.inbound.mode === "poll" ? (
                  <Field label="Every (minutes)">
                    <input aria-label="Check every (minutes)" type="number" min={1} max={1440} value={draft.inbound.poll_minutes} data-testid="tk-poll"
                           onChange={(e) => patch({ inbound: { ...draft.inbound, poll_minutes: Math.max(1, Number(e.target.value) || 1) } })} />
                  </Field>
                ) : null}
                {draft.inbound.mode === "webhook" && item?.webhook !== false ? (
                  <div className="fk-wide tk-stack">
                    <div className="tk-row">
                      <GatedButton permission="integration:manage" projectId={projectId} className="su-btn su-btn-sm" data-testid="tk-hook"
                                   disabled={!conn || busy === "hook"} title={conn ? "A new secret; the old one stops working at once" : "Save the connection first"}
                                   onClick={() => void webhook()}>{hook ? "Rotate again" : "Create or rotate webhook"}</GatedButton>
                      <span className="tk-faint" title={`Paste the URL and secret into ${item?.label ?? "the ticket system"}'s webhook settings.`}>Paste both into its webhooks</span>
                    </div>
                    {hook ? (
                      <div className="tk-hook" data-testid="tk-hook-once" role="status">
                        <b>Shown once. Copy both now.</b>
                        <div className="tk-row"><span className="tk-lbl">URL</span><code data-testid="tk-hook-url">{hook.url}</code>
                          <button type="button" className="su-icon-btn" aria-label="Copy the webhook URL" data-testid="tk-hook-copy-url" onClick={() => copy(hook.url)}><CopySimple size={13} /></button></div>
                        <div className="tk-row"><span className="tk-lbl">Secret</span><code data-testid="tk-hook-secret">{hook.secret}</code>
                          <button type="button" className="su-icon-btn" aria-label="Copy the webhook secret" data-testid="tk-hook-copy-secret" onClick={() => copy(hook.secret)}><CopySimple size={13} /></button></div>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </FormGrid>
            </details>
          </section>
        ) : null}

        <FormError>{err}</FormError>
        <div className="tk-drawer-foot">
          <button type="button" className="su-btn" onClick={onClose} data-testid="tk-cancel">Cancel</button>
          <GatedButton permission="integration:manage" projectId={projectId} className="su-btn su-btn-primary" data-testid="tk-save"
                       disabled={busy === "save" || (!item && !edit)} onClick={() => void save()}>{busy === "save" ? "Saving…" : "Save"}</GatedButton>
        </div>
      </div>
    </Drawer>
  );
}
