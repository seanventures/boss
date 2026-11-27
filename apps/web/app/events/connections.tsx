"use client";

/**
 * SETUP › EVENTS › CONNECTIONS (P9a-7, wireframe E1): what wakes your agents, and is it healthy.
 * Four tiles (events today, woke an agent, refused, dead letters), then one card per source:
 * its mark, where it is, a Live / Paused / Auth failed chip, events per minute over the last hour
 * as a sparkline, when the last event came, how long the source took to reach BOSS (lag), errors
 * in the hour and its rules. ⋯ tests, rotates a BOSS-minted secret (shown once), pauses or
 * removes it. Sources BOSS cannot connect yet are dashed "Coming" cards.
 *
 * The ONE main action is Connect a source: Slack opens its own three-step page (E2); a GitHub
 * or generic webhook connects in a sheet from the catalog's fields, the URL and secret shown once.
 */
import React, { useMemo, useState } from "react";
import { ArrowsClockwise, Clock, Copy, Lightning, ListBullets, Lock, Plus, Prohibit, Tray, Warning, XCircle } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { Field, FormError, FormGrid } from "../../components/form-kit";
import { formatRelativeTime } from "../../lib/datetime";
import {
  createConnection, deleteConnection, readCatalog, readEvents, rotateConnectionSecret, setConnectionPaused, testConnection,
  checkWords, parseConnection, parseTest, whereWords, type Connection, type EventList, type Source,
} from "../../lib/intake";
import { useNow } from "../agents/live-util";
import { Drawer } from "../setup/drawer";
import { RowMenu } from "../setup/parts";
import { EvFrame, useEv } from "./frame";
import { Chip, Empty, ReadState, Said, SourceMark, Spark, goEvents, useRead } from "./parts";

/** Sources BOSS cannot connect yet (phase P9b/P9c); a generic webhook covers them today. */
const COMING: { kind: string; label: string; line: string; phase: string }[] = [
  { kind: "teams", label: "Microsoft Teams", line: "People talk to agents in Teams", phase: "P9b" },
  { kind: "servicenow", label: "ServiceNow", line: "Incidents and changes wake an agent", phase: "P9b" },
  { kind: "salesforce", label: "Salesforce", line: "Record changes, streamed", phase: "P9c" },
  { kind: "mail", label: "Gmail / Outlook", line: "A shared inbox wakes an agent", phase: "P9c" },
];

const LIVE: Record<Connection["status"], { word: string; tone: string; tip: string }> = {
  active: { word: "Live", tone: "green", tip: "Receiving events" },
  paused: { word: "Paused", tone: "grey", tip: "Deliveries are answered and dropped until you resume" },
  disabled: { word: "Off", tone: "grey", tip: "Turned off" },
  auth_failed: { word: "Sign-in failed", tone: "red", tip: "The source refused BOSS's credentials; rotate or paste them again" },
  rate_limited: { word: "Rate limited", tone: "amber", tip: "The source is slowing BOSS down" },
};

/** Per connection, from the last hour of events: 12 five-minute buckets, the median lag, errors. */
export function hourStats(list: EventList | null, nowMs: number) {
  const out: Record<string, { buckets: number[]; lag: number | null; errors: number }> = {};
  if (!list) return out;
  const lags: Record<string, number[]> = {};
  for (const e of list.events) {
    const at = Date.parse(e.received_at);
    if (!Number.isFinite(at)) continue;
    const slot = out[e.connection_id] ?? (out[e.connection_id] = { buckets: Array(12).fill(0), lag: null, errors: 0 });
    const age = Math.max(0, nowMs - at);
    const i = 11 - Math.min(11, Math.floor(age / 300_000));
    slot.buckets[i] += 1;
    // A held event (a rate limit, quiet hours, a grant being asked for) is not an error.
    if (e.status === "dead" || (e.status === "failed" && !e.last_error.startsWith("deferred"))) slot.errors += 1;
    // Lag is delivery time: a record's own time (a PR updated yesterday) is not a delivery, so
    // anything over ten minutes is the record's age, not the source's lag.
    const t = Date.parse(e.event_time);
    if (Number.isFinite(t) && at >= t && at - t < 600_000) (lags[e.connection_id] ??= []).push((at - t) / 1000);
  }
  for (const [id, xs] of Object.entries(lags)) {
    xs.sort((x, y) => x - y);
    out[id].lag = xs[Math.floor(xs.length / 2)];
  }
  return out;
}
const lagWords = (s: number | null) => (s === null ? "lag —" : s < 10 ? `lag ${s.toFixed(1)} s` : s < 120 ? `lag ${Math.round(s)} s` : `lag ${Math.round(s / 60)} min`);
const perMin = (n: number) => (n === 0 ? "0/min" : n / 60 < 0.1 ? "<0.1/min" : n / 60 >= 10 ? `${Math.round(n / 60).toLocaleString("en-US")}/min` : `${(n / 60).toFixed(1)}/min`);
const copy = (t: string) => { try { void navigator.clipboard?.writeText(t); } catch { /* no clipboard */ } };

export function ConnectionsTab() {
  const { projectId, conns, reloadConns } = useEv();
  const manage = useGate("integration:manage", projectId);
  const now = useNow(30_000);
  const today = useRead(() => readEvents(projectId, { hours: 24, limit: 1 }), [projectId]);
  const hour = useRead(() => readEvents(projectId, { hours: 1, limit: 500 }), [projectId]);
  const catalog = useRead(readCatalog, []);
  const [picker, setPicker] = useState(false);
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" | "amber" } | null>(null);
  const [secret, setSecret] = useState<{ name: string; value: string } | null>(null);
  const [confirm, setConfirm] = useState<Connection | null>(null);

  const stats = useMemo(() => hourStats(hour.read?.ok ? hour.read.data : null, now ?? 0), [hour.read, now]);
  const counts = today.read?.ok ? today.read.data.counts : null;
  const items = conns?.ok ? conns.data : [];
  const known = new Set((catalog.read?.ok ? catalog.read.data : []).map((s) => s.key));

  const test = async (c: Connection) => {
    setSaid(null);
    const r = await testConnection(c.id);
    if (!r.ok) { setSaid({ text: r.error || "The test failed.", tone: "red" }); return; }
    const t = parseTest(r.body);
    const lines = checkWords(t);
    const bad = lines.filter((l) => !l.ok).map((l) => l.label);
    setSaid(t.ok ? { text: `${c.label} works · ${lines.length ? `${lines.map((l) => l.label).join(", ")} ✓ · ` : ""}${t.last_hour} events this hour.`, tone: "green" }
                 : { text: `${c.label} needs a look: ${bad.join("; ") || "the source check failed"}.`, tone: "red" });
    reloadConns();
  };
  const rotate = async (c: Connection) => {
    setSaid(null);
    const r = await rotateConnectionSecret(c.id);
    const b = (r.body ?? {}) as { secret?: string };
    if (r.ok && b.secret) { setSecret({ name: c.label, value: b.secret }); reloadConns(); }
    else setSaid({ text: r.error || "Not rotated.", tone: "red" });
  };
  const pause = async (c: Connection) => {
    const r = await setConnectionPaused(c.id, c.status !== "paused");
    setSaid(r.ok ? { text: c.status === "paused" ? `${c.label} resumed.` : `${c.label} paused. Deliveries are answered and dropped.`, tone: "green" } : { text: r.error || "Not changed.", tone: "red" });
    if (r.ok) reloadConns();
  };
  const remove = async (c: Connection) => {
    setConfirm(null);
    const r = await deleteConnection(c.id);
    setSaid(r.ok ? { text: `${c.label} removed. Its events stay in the stream.`, tone: "green" } : { text: r.error || "Not removed.", tone: "red" });
    if (r.ok) reloadConns();
  };

  return (
    <EvFrame tab="connections" title="Events" sub="What wakes your agents, and is it healthy."
             actions={<GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-kind="primary" data-testid="ev-connect"
                                   title="Slack, GitHub or any signed webhook" onClick={() => setPicker(true)}><Plus size={16} aria-hidden />Connect a source</GatedButton>}>
      <div className="ev-tiles" data-testid="ev-tiles">
        <Tile icon={<Tray size={16} aria-hidden />} label="Events today" value={counts?.total} onClick={() => goEvents({ events: "stream" })} testId="ev-tile-total" />
        <Tile icon={<Lightning size={16} aria-hidden />} label="Woke an agent" value={counts?.routed} tone="green" onClick={() => goEvents({ events: "stream" })} testId="ev-tile-routed" />
        <Tile icon={<Prohibit size={16} aria-hidden />} label="Refused" value={counts?.refused} tone="amber" onClick={() => goEvents({ events: "stream" })} testId="ev-tile-refused" />
        <Tile icon={<XCircle size={16} aria-hidden />} label="Dead letters" value={counts ? items.reduce((n, c) => n + c.dead, 0) : undefined} tone="red"
              onClick={() => goEvents({ events: "dead" })} testId="ev-tile-dead" />
      </div>
      <Said said={said} testId="ev-conn-said" />
      {secret ? (
        <div className="ev-secret-once" role="status" data-testid="ev-secret-once">
          <b>New signing secret for {secret.name}</b>
          <span className="ev-faint">Shown once. Paste it into the sender now; the old one stops in a few minutes.</span>
          <div className="ev-copy"><code data-testid="ev-secret-value">{secret.value}</code>
            <button type="button" className="ev-btn" data-size="sm" onClick={() => copy(secret.value)}><Copy size={13} aria-hidden /> Copy</button>
            <button type="button" className="ev-btn" data-size="sm" data-kind="ghost" onClick={() => setSecret(null)}>Done</button></div>
        </div>
      ) : null}
      <section aria-labelledby="ev-sources-h" className="ev-form">
        <h2 id="ev-sources-h" className="ev-sec-title">Sources</h2>
        <ReadState read={conns} what="Connections" onRetry={reloadConns} testId="ev-conns" />
        {conns?.ok && !items.length ? (
          <Empty icon={<Lightning size={30} aria-hidden />} title="Nothing wakes your agents yet" line="Connect Slack, GitHub or any signed webhook." testId="ev-conns-empty">
            {manage.allowed ? <button type="button" className="ev-btn" data-kind="primary" onClick={() => setPicker(true)}><Plus size={14} aria-hidden /> Connect a source</button> : null}
          </Empty>
        ) : null}
        <div className="ev-sources">
          {items.map((c) => {
            const st = stats[c.id];
            const live = LIVE[c.status];
            const digest = st && st.buckets.reduce((x, y) => x + y, 0) > 3000;
            return (
              <article key={c.id} className="ev-card" data-testid={`ev-src-${c.id}`} data-status={c.status}>
                <div className="ev-src-head">
                  <SourceMark kind={c.kind} />
                  <div className="ev-src-name"><b>{c.label}</b><span className="ev-src-where" title={c.url || c.url_path}>{whereWords(c)}</span></div>
                  <RowMenu label={`${c.label}: test, rotate secret, pause, remove`} testid={`ev-src-menu-${c.id}`} items={[
                    { label: "Test", testid: `ev-src-test-${c.id}`, disabled: !manage.allowed, onSelect: () => void test(c) },
                    ...(c.kind !== "slack" && c.secret_fields.includes("signing_secret")
                      ? [{ label: "Rotate secret", testid: `ev-src-rotate-${c.id}`, disabled: !manage.allowed, onSelect: () => void rotate(c) }] : []),
                    { label: c.status === "paused" ? "Resume" : "Pause", testid: `ev-src-pause-${c.id}`, disabled: !manage.allowed, onSelect: () => void pause(c) },
                    { label: "Remove", testid: `ev-src-remove-${c.id}`, danger: true, disabled: !manage.allowed, onSelect: () => setConfirm(c) },
                  ]} />
                </div>
                <div className="ev-chips">
                  <Chip tone={live.tone} tip={c.health.status_reason || live.tip} testId={`ev-src-state-${c.id}`}>{live.word}{c.mode === "socket" ? " · Socket Mode" : ""}</Chip>
                  {digest ? <Chip tone="violet" tip="A flood: rules run once every 5 minutes with a summary">Digest mode</Chip> : null}
                  {c.health.last_test_ok === false ? <Chip tone="red" tip="The last test failed">Test failed</Chip> : null}
                </div>
                <div className="ev-rate">
                  <Spark values={st?.buckets ?? []} tone={digest ? "violet" : undefined} />
                  <span className="ev-row-main"><b>{perMin(c.last_hour)}</b><span className="ev-faint">last hour</span></span>
                </div>
                <div className="ev-src-foot">
                  <span title="Last event received"><Clock size={13} aria-hidden />{c.last_event_at && now ? formatRelativeTime(c.last_event_at, now, { empty: "no events yet" }) : "no events yet"}</span>
                  <span title="Time from the source to BOSS saving the event (median, last hour)"><Lightning size={13} aria-hidden />{lagWords(st?.lag ?? null)}</span>
                  <span title="Errors in the last hour" data-tone={st?.errors ? "red" : undefined}><Warning size={13} aria-hidden />{st?.errors ?? 0} error{st?.errors === 1 ? "" : "s"}</span>
                  <button type="button" className="ev-link" onClick={() => goEvents({ events: "rules", conn: c.id })} data-testid={`ev-src-rules-${c.id}`}>
                    <ListBullets size={13} aria-hidden />{c.rules} rule{c.rules === 1 ? "" : "s"}</button>
                </div>
                {confirm?.id === c.id ? (
                  <div className="ev-row-line" role="alertdialog" aria-label={`Remove ${c.label}?`} data-testid="ev-remove-confirm">
                    <span className="ev-faint">Remove {c.label}? Its rules stop; events stay.</span>
                    <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-size="sm" data-kind="danger"
                                 onClick={() => void remove(c)} data-testid="ev-remove-yes">Remove</GatedButton>
                    <button type="button" className="ev-btn" data-size="sm" onClick={() => setConfirm(null)}>Keep it</button>
                  </div>
                ) : null}
              </article>
            );
          })}
          {COMING.filter((x) => !known.has(x.kind) && !items.some((c) => c.kind === x.kind)).map((x) => (
            <article key={x.kind} className="ev-card ev-card-coming" data-dashed="true" data-testid={`ev-coming-${x.kind}`}>
              <div className="ev-src-head"><SourceMark kind={x.kind} /><div className="ev-src-name"><b>{x.label}</b><span className="ev-faint">{x.line}</span></div></div>
              <div><Chip tone="grey" tip={`Planned for phase ${x.phase}`}><Clock size={12} aria-hidden /> Coming</Chip></div>
              <span className="ev-faint">You can wire it today with a generic webhook.</span>
            </article>
          ))}
        </div>
      </section>
      <div className="ev-vault"><Lock size={15} aria-hidden /><span>Secrets and bot tokens stay in BOSS&apos;s vault. Agents never hold them.</span></div>
      {picker ? <ConnectPicker sources={catalog.read?.ok ? catalog.read.data : []} onClose={() => setPicker(false)}
                               onDone={(words, shown) => { setPicker(false); setSaid({ text: words, tone: "green" }); if (shown) setSecret(shown); reloadConns(); }} /> : null}
    </EvFrame>
  );
}

function Tile({ icon, label, value, tone, onClick, testId }: { icon: React.ReactNode; label: string; value: number | undefined; tone?: string; onClick: () => void; testId: string }) {
  return (
    <button type="button" className="ev-tile" data-tone={tone} onClick={onClick} data-testid={testId}>
      <span className="ev-tile-label">{icon}{label}</span>
      <b className="ev-tile-num">{value === undefined ? "—" : value.toLocaleString("en-US")}</b>
    </button>
  );
}

/** Connect a source: pick from the catalog. Slack goes to its own page (E2); the rest connect here. */
function ConnectPicker({ sources, onClose, onDone }: {
  sources: Source[]; onClose: () => void; onDone: (words: string, shown: { name: string; value: string } | null) => void;
}) {
  const [kind, setKind] = useState<Source | null>(null);
  return (
    <Drawer title={kind ? `Connect ${kind.label}` : "Connect a source"} onClose={onClose} testid="ev-connect-sheet">
      {kind ? <ConnectForm source={kind} onBack={() => setKind(null)} onDone={onDone} /> : (
        <div className="ev-form">
          {sources.map((s) => (
            <button key={s.key} type="button" className="ev-rule" data-testid={`ev-pick-${s.key}`}
                    onClick={() => (s.key === "slack" ? (onClose(), goEvents({ events: "connect", kind: "slack" })) : setKind(s))}>
              <SourceMark kind={s.key} />
              <span className="ev-row-main"><b>{s.label}</b><span className="ev-faint">{s.key === "slack" ? "Three steps: create the app, paste two secrets, test" : s.description.split(". ")[0]}</span></span>
            </button>
          ))}
          {!sources.length ? <span className="ev-faint">Loading the sources BOSS can connect…</span> : null}
        </div>
      )}
    </Drawer>
  );
}

function ConnectForm({ source, onBack, onDone }: {
  source: Source; onBack: () => void; onDone: (words: string, shown: { name: string; value: string } | null) => void;
}) {
  const { projectId } = useEv();
  const [name, setName] = useState(source.label);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [made, setMade] = useState<{ url: string; secret: string } | null>(null);
  const plain = source.auth_fields.filter((f) => !f.secret);
  const secret = source.auth_fields.filter((f) => f.secret);
  const minted = secret.some((f) => f.name === "signing_secret");
  const missing = [...plain, ...secret].filter((f) => f.required && !(f.secret && f.name === "signing_secret") && !(values[f.name] || "").trim());
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError("");
    const config = Object.fromEntries(plain.map((f) => [f.name, (values[f.name] || "").trim()]).filter(([, v]) => v));
    const secrets = Object.fromEntries(secret.map((f) => [f.name, (values[f.name] || "").trim()]).filter(([, v]) => v));
    const r = await createConnection(projectId, { kind: source.key, name: name.trim() || source.label, mode: source.modes[0] || "push", config, secrets });
    setBusy(false);
    if (!r.ok) { setError(r.error || "Not connected."); return; }
    const c = parseConnection(r.body);
    const shown = (r.body as { secret?: string } | null)?.secret || "";
    setMade({ url: c.url || c.url_path, secret: shown });
  };
  if (made) {
    return (
      <div className="ev-form" data-testid="ev-connect-made">
        <b>{source.label} is connected</b>
        <span className="ev-faint">Paste this URL into the sender&apos;s webhook settings.</span>
        <div className="ev-copy"><code data-testid="ev-connect-url">{made.url}</code><button type="button" className="ev-btn" data-size="sm" onClick={() => copy(made.url)}><Copy size={13} aria-hidden /> Copy</button></div>
        {made.secret ? (
          <div className="ev-secret-once"><b>Signing secret · shown once</b>
            <div className="ev-copy"><code data-testid="ev-connect-secret">{made.secret}</code><button type="button" className="ev-btn" data-size="sm" onClick={() => copy(made.secret)}><Copy size={13} aria-hidden /> Copy</button></div></div>
        ) : null}
        <div className="ev-row-line"><span className="ev-spacer" />
          <button type="button" className="ev-btn" data-kind="primary" data-testid="ev-connect-done" onClick={() => onDone(`${source.label} connected. Add a rule to wake an agent.`, null)}>Done</button></div>
      </div>
    );
  }
  return (
    <form className="ev-form" onSubmit={(e) => void submit(e)} noValidate data-testid="ev-connect-form">
      <FormGrid label={`Connect ${source.label}`}>
        <Field label="Name" tip="How this connection is listed"><input value={name} onChange={(e) => setName(e.target.value)} data-testid="ev-connect-name" /></Field>
        {plain.map((f) => (
          <Field key={f.name} label={`${f.label}${f.required ? "" : " (optional)"}`} tip={f.help}>
            <input value={values[f.name] || ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} data-testid={`ev-connect-${f.name}`} />
          </Field>
        ))}
        {secret.map((f) => (
          <Field key={f.name} label={<>{f.label} <span className="ev-tag-ro" title="BOSS stores it in the vault. Nobody, including you, can read it back.">Write-only</span></>}
                 tip={f.name === "signing_secret" ? "Leave empty: BOSS makes one and shows it once" : f.help}>
            <input type="password" autoComplete="off" value={values[f.name] || ""} placeholder={f.name === "signing_secret" ? "BOSS makes one" : ""}
                   onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} data-testid={`ev-connect-${f.name}`} />
          </Field>
        ))}
        <FormError>{error}</FormError>
      </FormGrid>
      {minted ? <span className="ev-faint">Leave the signing secret empty: BOSS makes one and shows it once.</span> : null}
      <div className="ev-row-line">
        <button type="button" className="ev-btn" data-kind="ghost" onClick={onBack}>Back</button>
        <span className="ev-spacer" />
        <GatedButton permission="integration:manage" projectId={projectId} type="submit" className="ev-btn" data-kind="primary"
                     disabled={busy || missing.length > 0} title={missing.length ? `Fill in ${missing.map((f) => f.label).join(", ")}` : undefined}
                     data-testid="ev-connect-save">{busy ? <ArrowsClockwise size={14} aria-hidden /> : null}Connect</GatedButton>
      </div>
    </form>
  );
}
