"use client";

/**
 * Observability › NOTIFY (BOSSAPP-598, PRD §8): who is told, how, and when. Four zones:
 *
 *   CONTACT POINTS   Console · On-call email ✓ verified · #ops Slack (Unverified [code][Verify])  Test  [+ Add]
 *                    Recent sends ▸ (the outbox, with receipts)
 *   POLICIES         Every alert → Console                         wait 30 s · repeat 4 h      [+ Child] Edit
 *                      └ service="checkout" → On-call email         quiet 22:00–07:00          Edit  Delete
 *   ROUTING PREVIEW  [ service=checkout\nseverity=critical ]  [Preview] → where it goes, after how long, skipped
 *   SILENCES & MAINTENANCE   active silences (End) · [+ Silence] · windows (Delete) · [+ Window] · inhibit rules
 *
 * The router and the preview share one server function: what the preview says is what happens.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import {
  CONTACT_KINDS, KIND_WORD, PREVIEW_PATH, contactPath, matchersWord, parseLabelLines, parsePreview, policyPath, policyTree,
  rruleWord, secondsWord, useContactPoints, useNotifications, usePolicies, useSilences,
  type ContactPoint, type Matcher, type Policy, type Preview,
} from "../../lib/monitoring";
import { useLocalTime } from "../../lib/local-time";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { CopyButton } from "../infra/infra-util";
import { Empty, Sheet, Skeleton, ZoneHead } from "../infra/kit";
import { whenWord } from "./alerts-tab";
import { obsWrite } from "./obs-write";
import type { ObsTabProps } from "./obs-home";
import { SevFlag } from "./severity";

const VERIFY_TIP = "Unverified: high-urgency alerts skip it until it is verified with the code BOSS sent";
const localZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } };

/** `service="checkout", severity=~"crit.*"` → matchers. Unquoted values are accepted. */
export function parseMatcherText(text: string): Matcher[] {
  const out: Matcher[] = [];
  for (const part of text.replace(/^\s*\{|\}\s*$/g, "").split(",")) {
    const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"?(.*?)"?\s*$/.exec(part);
    if (m) out.push({ name: m[1], op: m[2], value: m[3] });
  }
  return out;
}
const matcherText = (ms: Matcher[]) => ms.map((m) => `${m.name}${m.op}"${m.value}"`).join(", ");

type Zone = { projectId: string; actor: string };
const write = (path: string, body: Record<string, unknown>, label: string, method: "POST" | "PUT" | "DELETE" = "POST") =>
  obsWrite<Record<string, unknown>>(path, body, { scope: `obs:notify:${label}:${path}`, label }, method);
const said = (body: Record<string, unknown> | null, fallback: string) => (typeof body?.word === "string" && body.word ? body.word : fallback);

// ── CONTACT POINTS ──────────────────────────────────────────────────────────────────────────

function ContactRow({ c, projectId, actor, onDone }: Zone & { c: ContactPoint; onDone: () => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const test = async () => {
    setBusy("test");
    const r = await write(contactPath(c.id, "test"), { project_id: projectId, actor }, "Test");
    setBusy(null);
    if (r.ok) showToast(r.body?.sent === false ? "error" : "success", said(r.body, "Test sent")); else showToast("error", r.error ?? "Not sent");
    onDone();
  };
  const verify = async () => {
    setBusy("verify");
    const r = await write(contactPath(c.id, "verify"), { project_id: projectId, actor, code: code.trim() }, "Verify");
    setBusy(null);
    if (r.ok) { showToast("success", `${c.name} verified: high-urgency alerts use it now`); setCode(""); onDone(); }
    else showToast("error", r.error ?? "Not verified");
  };
  return (
    <li className="obs-line" data-testid={`obs-cp-${c.id}`}>
      <span className="obs-adot" data-tone={c.verified || c.kind === "console" ? "green" : "amber"} aria-hidden />
      <span className="obs-strong" data-user-text>{c.name}</span>
      <span className="obs-chip">{KIND_WORD[c.kind] ?? c.kind}</span>
      {c.shows ? <span className="inf-muted inf-trunc obs-url" title={c.shows} data-copy="data">{c.shows}</span> : null}
      {c.kind === "console" ? null : c.verified ? (
        <span className="obs-chip" data-tone="green" data-testid="obs-cp-verified" title="Verified: every alert may use it">Verified</span>
      ) : (
        <span className="obs-chip" data-tone="amber" data-testid="obs-cp-unverified" title={VERIFY_TIP}>Unverified</span>
      )}
      <span className="ag-spacer" />
      {!c.verified && c.kind !== "console" ? (
        <span className="obs-rfield">
          <input className="obs-rnum" value={code} onChange={(e) => setCode(e.target.value)} aria-label={`Code for ${c.name}`}
                 data-testid="obs-cp-code" placeholder="Code" autoComplete="one-time-code" inputMode="numeric"
                 title="The code in BOSS's test message" />
          <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-cp-verify"
                       disabled={!code.trim() || busy !== null} title={VERIFY_TIP} onClick={verify}>Verify</GatedButton>
        </span>
      ) : null}
      {c.kind !== "console" ? (
        <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-cp-test"
                     disabled={busy !== null} title={c.verified ? "Send a test message" : "Send a test message with the verification code"}
                     onClick={test}>{busy === "test" ? "Sending…" : "Test"}</GatedButton>
      ) : null}
    </li>
  );
}

function AddContactSheet({ projectId, actor, onClose, onSaved }: Zone & { onClose: () => void; onSaved: () => void }) {
  const [kind, setKind] = useState<(typeof CONTACT_KINDS)[number]["key"]>("email");
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const spec = CONTACT_KINDS.find((k) => k.key === kind)!;
  const save = async () => {
    setBusy(true);
    setError(null);
    const r = await write(contactPath(), { project_id: projectId, actor, kind, name: name.trim(), [spec.field]: target.trim() }, "Add contact point");
    setBusy(false);
    if (!r.ok) { setError(r.error ?? "Not added"); return; }
    showToast("success", `${name.trim()} added: send it a test to verify it`);
    onSaved();
    const t = typeof r.body?.signing_token === "string" ? r.body.signing_token : null;
    if (t) setToken(t); else onClose();
  };
  return (
    <Sheet title="Add a contact point" testId="obs-cp-sheet" onClose={onClose}>
      {token ? (
        <div className="obs-rform" data-testid="obs-cp-token">
          <span className="obs-chip" data-tone="amber" title="BOSS keeps only a hash">Shown once: copy it now</span>
          <span className="inf-muted">Your endpoint checks X-BOSS-Signature with this signing token.</span>
          <code className="obs-promql" data-copy="data">{token}</code>
          <div className="inf-actions"><span className="ag-spacer" /><CopyButton text={token} label="Copy token" testId="obs-cp-token-copy" />
            <button type="button" className="ag-btn ag-btn-primary" onClick={onClose}>Done</button></div>
        </div>
      ) : (
        <div className="obs-rform">
          <label className="inf-field">
            <span>Kind</span>
            <select value={kind} onChange={(e) => { setKind(e.target.value as typeof kind); setTarget(""); }} data-testid="obs-cp-kind">
              {CONTACT_KINDS.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
            </select>
          </label>
          <label className="inf-field">
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} data-testid="obs-cp-name" placeholder="On-call" autoComplete="off" />
          </label>
          <label className="inf-field">
            <span>{spec.field === "address" ? "Address" : "Webhook URL"}</span>
            <input value={target} onChange={(e) => setTarget(e.target.value)} data-testid="obs-cp-target" placeholder={spec.placeholder}
                   autoComplete="off" spellCheck={false} inputMode={spec.field === "address" ? "email" : "url"}
                   title={spec.field === "url" ? "Kept secret: shown back only by its host" : undefined} />
          </label>
          <span className="inf-faint" title={VERIFY_TIP}>It starts unverified; a test message carries the code.</span>
          {error ? <span className="inf-error" data-testid="obs-cp-error" data-copy="data">{error}</span> : null}
          <div className="inf-actions">
            <span className="ag-spacer" />
            <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
            <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-cp-save"
                         disabled={busy || !name.trim() || !target.trim()} onClick={save}>{busy ? "Adding…" : "Add"}</GatedButton>
          </div>
        </div>
      )}
    </Sheet>
  );
}

function ContactPoints({ projectId, actor, cps, refresh }: Zone & { cps: ReturnType<typeof useContactPoints>; refresh: () => void }) {
  const lt = useLocalTime();
  const [adding, setAdding] = useState(false);
  const [sends, setSends] = useState(false);
  const outbox = useNotifications(sends ? projectId : "");
  return (
    <section className="obs-zone" aria-labelledby="obs-cp-head" data-testid="obs-cps">
      <ZoneHead id="obs-cp-head" label="CONTACT POINTS" count={cps.data?.length} tip="Where BOSS sends what fired">
        <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-cp-add"
                     title="Email, Slack, Mattermost or a webhook" onClick={() => setAdding(true)}>+ Add</GatedButton>
      </ZoneHead>
      {!cps.data ? (cps.error ? <Empty>Can&apos;t load contact points</Empty> : <Skeleton testId="obs-cps-loading" />)
        : cps.data.length === 0 ? <Empty>Only the console, for now</Empty> : (
          <ul className="obs-list">{cps.data.map((c) => <ContactRow key={c.id} c={c} projectId={projectId} actor={actor} onDone={refresh} />)}</ul>
        )}
      <button type="button" className="inf-link" aria-expanded={sends} data-testid="obs-sends-toggle"
              title="What BOSS sent, with each channel's receipt" onClick={() => setSends((v) => !v)}>
        {sends ? "Hide recent sends" : "Recent sends"}
      </button>
      {sends ? (
        !outbox.data ? <Skeleton testId="obs-sends-loading" /> : outbox.data.length === 0 ? <Empty>Nothing sent yet</Empty> : (
          <ul className="obs-list" data-testid="obs-sends">
            {outbox.data.slice(0, 15).map((n) => (
              <li key={n.id} className="obs-line" data-testid="obs-send">
                <span className="obs-adot" data-tone={n.status === "sent" ? "green" : n.status === "failed" || n.status === "dead" ? "red" : "grey"} aria-hidden />
                <span className="inf-num inf-faint" title={lt.full(n.sentAt ?? n.createdAt)}>{whenWord(n.sentAt ?? n.createdAt, lt)}</span>
                {n.sev ? <SevFlag sev={n.sev} /> : null}
                <span className="obs-strong obs-alert-what" data-user-text>{n.subject || n.kind}</span>
                {n.contactPoint ? <span className="inf-muted">{n.contactPoint}</span> : null}
                <span className="ag-spacer" />
                <span className="obs-chip" data-tone={n.status === "sent" ? "green" : n.status === "failed" || n.status === "dead" ? "red" : undefined}
                      title={n.errorWord ?? `${n.attempts} attempt${n.attempts === 1 ? "" : "s"}`}>{n.status}</span>
              </li>
            ))}
          </ul>
        )
      ) : null}
      {adding ? <AddContactSheet projectId={projectId} actor={actor} onClose={() => setAdding(false)} onSaved={refresh} /> : null}
    </section>
  );
}

// ── POLICIES ────────────────────────────────────────────────────────────────────────────────

function PolicySheet({ projectId, actor, policy, parent, cps, onClose, onSaved }: Zone & {
  policy: Policy | null; parent: Policy | null; cps: ContactPoint[]; onClose: () => void; onSaved: () => void;
}) {
  const root = policy ? policy.parentId === null : false;
  const [name, setName] = useState(policy?.name ?? "");
  const [matchers, setMatchers] = useState(policy ? matcherText(policy.matchers) : "");
  const [picked, setPicked] = useState<Set<string>>(new Set(policy?.contactPointIds ?? []));
  const [groupBy, setGroupBy] = useState((policy?.groupBy ?? []).join(", "));
  const [wait, setWait] = useState(policy?.groupWaitS != null ? String(policy.groupWaitS) : "");
  const [interval, setInterval_] = useState(policy?.groupIntervalS != null ? String(policy.groupIntervalS) : "");
  const [repeat, setRepeat] = useState(policy?.repeatIntervalS != null ? String(policy.repeatIntervalS) : "");
  const [cont, setCont] = useState(policy?.continueMatching ?? false);
  const [quiet, setQuiet] = useState(Boolean(policy?.quietHours));
  const [qStart, setQStart] = useState(policy?.quietHours?.start ?? "22:00");
  const [qEnd, setQEnd] = useState(policy?.quietHours?.end ?? "07:00");
  const [qZone, setQZone] = useState(policy?.quietHours?.timezone ?? localZone());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const n = (v: string) => (v.trim() === "" ? undefined : Number(v));
  const save = async () => {
    setBusy(true);
    setError(null);
    const body: Record<string, unknown> = {
      project_id: projectId, actor, name: name.trim(), matchers: parseMatcherText(matchers), continue_matching: cont,
      group_by: groupBy.split(",").map((g) => g.trim()).filter(Boolean), contact_point_ids: [...picked],
      group_wait_s: n(wait), group_interval_s: n(interval), repeat_interval_s: n(repeat),
      quiet_hours: quiet ? { start: qStart, end: qEnd, timezone: qZone } : null,
    };
    if (!policy && parent) body.parent_id = parent.id;
    if (policy?.parentId) body.parent_id = policy.parentId;
    const r = await write(policy ? policyPath(policy.id) : policyPath(), body, policy ? "Save policy" : "Add policy", policy ? "PUT" : "POST");
    setBusy(false);
    if (r.ok) { showToast("success", policy ? `${name.trim()} saved` : `${name.trim()} added under ${parent?.name ?? "the root"}`); onSaved(); }
    else setError(r.error ?? "Not saved");
  };
  return (
    <Sheet title={policy ? `Edit ${policy.name}` : `Add a policy under ${parent?.name ?? "the root"}`} testId="obs-policy-sheet" onClose={onClose}>
      <div className="obs-rform">
        <label className="inf-field"><span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} data-testid="obs-policy-name" placeholder="Checkout on-call" autoComplete="off" /></label>
        {!root ? (
          <label className="inf-field"><span>Matches</span>
            <input value={matchers} onChange={(e) => setMatchers(e.target.value)} data-testid="obs-policy-matchers" spellCheck={false}
                   placeholder='service="checkout", severity="critical"' autoComplete="off"
                   title='Labels to match: name="value", != , =~ (regex), !~; comma-separated' /></label>
        ) : <span className="inf-faint" title="The root policy takes every alert no child policy takes">Matches every alert</span>}
        <fieldset className="obs-zone" data-testid="obs-policy-cps">
          <legend className="obs-rlabel">Tell</legend>
          {cps.length === 0 ? <span className="inf-faint">Add a contact point first</span> : cps.map((c) => (
            <label key={c.id} className="obs-rcheck" title={!c.verified && c.kind !== "console" ? VERIFY_TIP : undefined}>
              <input type="checkbox" aria-label={`Tell ${c.name}`} checked={picked.has(c.id)}
                     onChange={(e) => setPicked((s) => { const x = new Set(s); if (e.target.checked) x.add(c.id); else x.delete(c.id); return x; })} />
              <span>{c.name}{!c.verified && c.kind !== "console" ? " (unverified)" : ""}</span>
            </label>
          ))}
        </fieldset>
        <div className="obs-rrow"><span className="obs-rlabel" title="Alerts with the same values of these labels go in one message">Group by</span>
          <input aria-label="Group by" value={groupBy} onChange={(e) => setGroupBy(e.target.value)} data-testid="obs-policy-groupby" placeholder="alertname, service" spellCheck={false} /></div>
        <div className="obs-rrow"><span className="obs-rlabel">Timing (s)</span>
          <span className="obs-rbody">
            <input type="number" min={0} className="obs-rnum" value={wait} onChange={(e) => setWait(e.target.value)} aria-label="Group wait"
                   placeholder="30" title="Wait this long for more alerts before the first message" data-testid="obs-policy-wait" />
            <input type="number" min={0} className="obs-rnum" value={interval} onChange={(e) => setInterval_(e.target.value)} aria-label="Group interval"
                   placeholder="300" title="At most one update to a group this often" />
            <input type="number" min={0} className="obs-rnum" value={repeat} onChange={(e) => setRepeat(e.target.value)} aria-label="Repeat interval"
                   placeholder="14400" title="Repeat a still-firing, un-acked alert this often" data-testid="obs-policy-repeat" />
            <span className="inf-faint">wait · interval · repeat (blank: inherit)</span>
          </span></div>
        <label className="obs-rcheck" title="Policy order matters: keep looking at the next sibling after a match">
          <input type="checkbox" checked={cont} onChange={(e) => setCont(e.target.checked)} data-testid="obs-policy-continue" />
          <span>Keep matching after this one</span></label>
        <label className="obs-rcheck" title="Low-urgency alerts wait until quiet hours end; high urgency still goes out">
          <input type="checkbox" checked={quiet} onChange={(e) => setQuiet(e.target.checked)} data-testid="obs-policy-quiet" />
          <span>Quiet hours</span></label>
        {quiet ? (
          <span className="obs-rbody">
            <input type="time" value={qStart} onChange={(e) => setQStart(e.target.value)} aria-label="Quiet from" />
            <span className="inf-muted">to</span>
            <input type="time" value={qEnd} onChange={(e) => setQEnd(e.target.value)} aria-label="Quiet until" />
            <input value={qZone} onChange={(e) => setQZone(e.target.value)} aria-label="Time zone" spellCheck={false} title="IANA time zone, e.g. Europe/London" />
          </span>
        ) : null}
        {error ? <span className="inf-error" data-testid="obs-policy-error" data-copy="data">{error}</span> : null}
        <div className="inf-actions"><span className="ag-spacer" />
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-policy-save"
                       disabled={busy || !name.trim()} onClick={save}>{busy ? "Saving…" : policy ? "Save" : "Add"}</GatedButton></div>
      </div>
    </Sheet>
  );
}

function Policies({ projectId, actor, cps }: Zone & { cps: ContactPoint[] }) {
  const policies = usePolicies(projectId);
  const [edit, setEdit] = useState<{ policy: Policy | null; parent: Policy | null } | null>(null);
  const [sure, setSure] = useState<string | null>(null);
  const cpName = (id: string) => cps.find((c) => c.id === id)?.name ?? id;
  const remove = async (p: Policy) => {
    if (sure !== p.id) { setSure(p.id); return; }
    setSure(null);
    const r = await write(`${policyPath(p.id)}?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actor)}`,
                          { project_id: projectId, actor }, "Delete policy", "DELETE");
    if (r.ok) { showToast("success", `${p.name} deleted: its alerts follow its parent now`); policies.refresh(); }
    else showToast("error", r.error ?? "Not deleted");
  };
  const tree = policies.data ? policyTree(policies.data) : [];
  return (
    <section className="obs-zone" aria-labelledby="obs-pol-head" data-testid="obs-policies">
      <ZoneHead id="obs-pol-head" label="POLICIES" count={policies.data?.length} tip="Which alerts go to whom; the first matching child wins" />
      {!policies.data ? (policies.error ? <Empty>Can&apos;t load policies</Empty> : <Skeleton testId="obs-policies-loading" />)
        : tree.length === 0 ? <Empty>No policies: alerts show in the console only</Empty> : (
          <ul className="obs-list">
            {tree.map(([p, depth]) => (
              <li key={p.id} className="obs-line obs-policy" data-testid={`obs-policy-${p.id}`} data-depth={Math.min(depth, 4)}>
                {depth ? <span className="inf-faint" aria-hidden>└</span> : null}
                <span className="obs-strong" data-user-text>{p.name}</span>
                <span className="inf-mono inf-muted" data-copy="data" title="Matches">{depth ? matchersWord(p.matchers) : "every alert"}</span>
                <span aria-hidden className="inf-faint">→</span>
                <span className="inf-muted" data-testid="obs-policy-tells">{p.contactPointIds.length ? p.contactPointIds.map(cpName).join(", ") : "inherits"}</span>
                <span className="inf-faint" title="Group wait · repeat interval">
                  {p.groupWaitS != null ? `wait ${secondsWord(p.groupWaitS)}` : ""}{p.repeatIntervalS != null ? ` · repeat ${secondsWord(p.repeatIntervalS)}` : ""}
                </span>
                {p.quietHours ? <span className="obs-chip" title={`Low urgency waits; ${p.quietHours.timezone}`}>quiet {p.quietHours.start}–{p.quietHours.end}</span> : null}
                {p.continueMatching ? <span className="obs-chip" title="Keeps matching the next sibling">continues</span> : null}
                {p.ruleId ? <span className="obs-chip" title="Written by a rule's Notify setting">rule</span> : null}
                <span className="ag-spacer" />
                <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-policy-child"
                             title="A narrower policy under this one" onClick={() => setEdit({ policy: null, parent: p })}>+ Child</GatedButton>
                <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-policy-edit"
                             onClick={() => setEdit({ policy: p, parent: null })}>Edit</GatedButton>
                {p.parentId ? (
                  <GatedButton permission="observability:manage" type="button" className={sure === p.id ? "ag-btn ag-btn-sm" : "inf-link"}
                               data-testid="obs-policy-delete" title="Its alerts follow its parent" onClick={() => void remove(p)}>
                    {sure === p.id ? "Confirm delete" : "Delete"}
                  </GatedButton>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      {edit ? <PolicySheet projectId={projectId} actor={actor} policy={edit.policy} parent={edit.parent} cps={cps}
                           onClose={() => setEdit(null)} onSaved={() => { setEdit(null); policies.refresh(); notifyChanged("observability", { policy: "x" }); }} /> : null}
    </section>
  );
}

// ── ROUTING PREVIEW ─────────────────────────────────────────────────────────────────────────

function RoutingPreview({ projectId }: { projectId: string }) {
  const lt = useLocalTime();
  const [labels, setLabels] = useState('service=checkout\nseverity=critical');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    const r = await obsWrite(PREVIEW_PATH, { project_id: projectId, labels: parseLabelLines(labels) }, { scope: "obs:notify:preview", label: "Preview" });
    setBusy(false);
    if (r.ok) setResult(parsePreview(r.body)); else { setResult(null); setError(r.error ?? "No preview"); }
  };
  return (
    <section className="obs-zone" aria-labelledby="obs-route-head" data-testid="obs-routing">
      <ZoneHead id="obs-route-head" label="ROUTING PREVIEW" tip="Where an alert with these labels would go — the router's own answer" />
      <label className="inf-field">
        <span>Labels (one key=value a line)</span>
        <textarea rows={3} value={labels} onChange={(e) => setLabels(e.target.value)} data-testid="obs-route-labels" spellCheck={false}
                  className="obs-promql-edit" />
      </label>
      <span className="obs-line"><span className="ag-spacer" />
        <button type="button" className="ag-btn ag-btn-sm" data-testid="obs-route-go" disabled={busy} onClick={run}>{busy ? "Asking…" : "Preview"}</button></span>
      {error ? <span className="inf-error" data-copy="data">{error}</span> : null}
      {result ? (
        <div className="obs-zone" data-testid="obs-route-result">
          <span className="obs-strong" data-testid="obs-route-word" data-copy="data">{result.word}</span>
          <ul className="obs-list">
            {result.routes.map((r) => (
              <li key={r.policyId} className="obs-zone" data-testid="obs-route-hit">
                <span className="obs-line">
                  <span className="inf-muted">{r.path.join(" › ")}</span>
                  {r.groupWaitS != null ? <span className="inf-faint">after {secondsWord(r.groupWaitS)}</span> : null}
                  {r.repeatIntervalS != null ? <span className="inf-faint">repeats every {secondsWord(r.repeatIntervalS)}</span> : null}
                  {r.quietUntil ? <span className="obs-chip" title={lt.full(r.quietUntil)}>quiet until {whenWord(r.quietUntil, lt)}</span> : null}
                </span>
                <span className="obs-line">
                  {r.contactPoints.map((c) => (
                    <span key={c.id} className="obs-chip" data-tone={c.skipped ? "amber" : "green"} data-testid="obs-route-cp"
                          title={c.skipped ?? "Will be told"}>{c.name}{c.skipped ? `: skipped, ${c.skipped}` : ""}</span>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

// ── SILENCES & MAINTENANCE ──────────────────────────────────────────────────────────────────

const MINUTES = [{ v: 60, l: "1 hour" }, { v: 240, l: "4 hours" }, { v: 1440, l: "24 hours" }, { v: 10080, l: "7 days" }];
const DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

function SilenceAdd({ projectId, actor, onClose, onSaved }: Zone & { onClose: () => void; onSaved: () => void }) {
  const [matchers, setMatchers] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    const r = await write("/v1/silences", { project_id: projectId, actor, matchers: parseMatcherText(matchers), minutes, reason: reason.trim() }, "Silence");
    setBusy(false);
    if (r.ok) { showToast("success", said(r.body, "Silenced: matching alerts are still recorded, nobody is told")); onSaved(); }
    else setError(r.error ?? "Not silenced");
  };
  return (
    <Sheet title="Add a silence" testId="obs-sil-sheet" onClose={onClose}>
      <div className="obs-rform">
        <label className="inf-field"><span>Matches</span>
          <input value={matchers} onChange={(e) => setMatchers(e.target.value)} data-testid="obs-sil-matchers" placeholder='service="checkout"'
                 spellCheck={false} autoComplete="off" title='name="value", comma-separated' /></label>
        <label className="inf-field"><span>For</span>
          <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} data-testid="obs-sil-for">
            {MINUTES.map((m) => <option key={m.v} value={m.v}>{m.l}</option>)}</select></label>
        <label className="inf-field"><span>Why</span>
          <input value={reason} onChange={(e) => setReason(e.target.value)} data-testid="obs-sil-reason" placeholder="Load test until 9" autoComplete="off" /></label>
        {error ? <span className="inf-error" data-copy="data">{error}</span> : null}
        <div className="inf-actions"><span className="ag-spacer" />
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-sil-save"
                       disabled={busy || !parseMatcherText(matchers).length || reason.trim().length < 3} onClick={save}>{busy ? "Silencing…" : "Silence"}</GatedButton></div>
      </div>
    </Sheet>
  );
}

function WindowAdd({ projectId, actor, onClose, onSaved }: Zone & { onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState("");
  const [matchers, setMatchers] = useState("");
  const [repeat, setRepeat] = useState<"once" | "daily" | "weekly">("weekly");
  const [days, setDays] = useState<Set<string>>(new Set(["SA", "SU"]));
  const [start, setStart] = useState("");
  const [duration, setDuration] = useState("60");
  const [mode, setMode] = useState<"mute" | "hold">("mute");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rrule = repeat === "once" ? "" : repeat === "daily" ? "FREQ=DAILY" : `FREQ=WEEKLY;BYDAY=${DAYS.filter((d) => days.has(d)).join(",")}`;
  const save = async () => {
    setBusy(true);
    const startsAt = start ? new Date(start).toISOString() : new Date().toISOString();
    const r = await write("/v1/maintenance-windows", { project_id: projectId, actor, name: name.trim(), matchers: parseMatcherText(matchers),
      rrule, starts_at: startsAt, duration_min: Number(duration) || 60, timezone: localZone(), mode }, "Add window");
    setBusy(false);
    if (r.ok) { showToast("success", `${name.trim()}: ${rruleWord(rrule)}, ${duration} min, ${mode === "mute" ? "muted" : "held until it ends"}`); onSaved(); }
    else setError(r.error ?? "Not added");
  };
  return (
    <Sheet title="Add a maintenance window" testId="obs-win-sheet" onClose={onClose}>
      <div className="obs-rform">
        <label className="inf-field"><span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} data-testid="obs-win-name" placeholder="Weekend patching" autoComplete="off" /></label>
        <label className="inf-field"><span>Matches</span>
          <input value={matchers} onChange={(e) => setMatchers(e.target.value)} data-testid="obs-win-matchers" placeholder='service="checkout"'
                 spellCheck={false} autoComplete="off" /></label>
        <div className="obs-rrow"><span className="obs-rlabel">Repeats</span>
          <span className="obs-rbody">
            {(["once", "daily", "weekly"] as const).map((r) => (
              <label key={r} className="obs-rcheck"><input type="radio" name="obs-win-repeat" checked={repeat === r} onChange={() => setRepeat(r)}
                                                         data-testid={`obs-win-${r}`} /><span>{r[0].toUpperCase() + r.slice(1)}</span></label>
            ))}
          </span></div>
        {repeat === "weekly" ? (
          <span className="obs-rbody" data-testid="obs-win-days">
            {DAYS.map((d) => (
              <label key={d} className="obs-rcheck"><input type="checkbox" checked={days.has(d)}
                onChange={(e) => setDays((s) => { const x = new Set(s); if (e.target.checked) x.add(d); else x.delete(d); return x; })} />
                <span>{d[0] + d[1].toLowerCase()}</span></label>
            ))}
          </span>
        ) : null}
        <div className="obs-rrow"><span className="obs-rlabel">Starts</span>
          <input type="datetime-local" aria-label="Starts" value={start} onChange={(e) => setStart(e.target.value)} data-testid="obs-win-start"
                 title={`Your time zone: ${localZone()}`} /></div>
        <div className="obs-rrow"><span className="obs-rlabel">For (min)</span>
          <input type="number" aria-label="Lasts, in minutes" min={1} className="obs-rnum" value={duration} onChange={(e) => setDuration(e.target.value)} data-testid="obs-win-duration" /></div>
        <div className="obs-rrow"><span className="obs-rlabel">During it</span>
          <span className="obs-rbody">
            <label className="obs-rcheck" title="Alerts are recorded; nobody is told"><input type="radio" name="obs-win-mode" checked={mode === "mute"}
              onChange={() => setMode("mute")} /><span>Mute</span></label>
            <label className="obs-rcheck" title="Messages wait; anything still firing when it ends is sent then"><input type="radio" name="obs-win-mode"
              checked={mode === "hold"} onChange={() => setMode("hold")} data-testid="obs-win-hold" /><span>Hold</span></label>
          </span></div>
        {error ? <span className="inf-error" data-copy="data">{error}</span> : null}
        <div className="inf-actions"><span className="ag-spacer" />
          <button type="button" className="ag-btn" onClick={onClose}>Cancel</button>
          <GatedButton permission="observability:manage" type="button" className="ag-btn ag-btn-primary" data-testid="obs-win-save"
                       disabled={busy || !name.trim() || (repeat === "weekly" && days.size === 0)} onClick={save}>{busy ? "Adding…" : "Add"}</GatedButton></div>
      </div>
    </Sheet>
  );
}

function SilencesZone({ projectId, actor }: Zone) {
  const lt = useLocalTime();
  const s = useSilences(projectId);
  const [sheet, setSheet] = useState<"silence" | "window" | null>(null);
  const done = () => { setSheet(null); s.refresh(); notifyChanged("observability", { silence: "x" }); };
  const end = async (id: string) => {
    const r = await write(`/v1/silences/${encodeURIComponent(id)}/end`, { project_id: projectId, actor }, "End silence");
    if (r.ok) { showToast("success", "Silence ended: matching alerts notify again"); s.refresh(); } else showToast("error", r.error ?? "Not ended");
  };
  const drop = async (id: string, name: string) => {
    const r = await write(`/v1/maintenance-windows/${encodeURIComponent(id)}?project_id=${encodeURIComponent(projectId)}&actor=${encodeURIComponent(actor)}`,
                          { project_id: projectId, actor }, "Delete window", "DELETE");
    if (r.ok) { showToast("success", `${name} deleted`); s.refresh(); } else showToast("error", r.error ?? "Not deleted");
  };
  const d = s.data;
  return (
    <section className="obs-zone" aria-labelledby="obs-sil-head" data-testid="obs-silences">
      <ZoneHead id="obs-sil-head" label="SILENCES & MAINTENANCE" tip="Who is not told, for how long, and why">
        <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-sil-add"
                     title="Stop matching alerts notifying for a while" onClick={() => setSheet("silence")}>+ Silence</GatedButton>
        <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-win-add"
                     title="A planned window, once or repeating" onClick={() => setSheet("window")}>+ Window</GatedButton>
      </ZoneHead>
      {!d ? (s.error ? <Empty>Can&apos;t load silences</Empty> : <Skeleton testId="obs-silences-loading" />) : (
        <>
          {d.silences.length === 0 ? <Empty>No active silences</Empty> : (
            <ul className="obs-list">
              {d.silences.map((x) => (
                <li key={x.id} className="obs-line" data-testid={`obs-sil-${x.id}`}>
                  <span className="obs-chip">Silence</span>
                  <span className="inf-mono" data-copy="data">{matchersWord(x.matchers)}</span>
                  <span className="inf-muted" data-user-text>{x.reason}</span>
                  {x.endsAt ? <span className="inf-faint" title={lt.full(x.endsAt)}>until {whenWord(x.endsAt, lt)}</span> : null}
                  {x.createdBy ? <span className="inf-faint">by {x.createdBy}</span> : null}
                  <span className="ag-spacer" />
                  <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-sil-end"
                               onClick={() => void end(x.id)}>End</GatedButton>
                </li>
              ))}
            </ul>
          )}
          {d.windows.length ? (
            <ul className="obs-list">
              {d.windows.map((w) => (
                <li key={w.id} className="obs-line" data-testid={`obs-win-${w.id}`}>
                  <span className="obs-chip" data-tone={w.activeUntil ? "amber" : undefined}
                        title={w.activeUntil ? `Active until ${lt.full(w.activeUntil)}` : "Not active now"}>{w.activeUntil ? "In window" : "Window"}</span>
                  <span className="obs-strong" data-user-text>{w.name}</span>
                  <span className="inf-mono inf-muted" data-copy="data">{matchersWord(w.matchers)}</span>
                  <span className="inf-faint">{rruleWord(w.rrule)} · {w.durationMin} min · {w.mode === "hold" ? "hold" : "mute"} · {w.timezone}</span>
                  <span className="ag-spacer" />
                  <GatedButton permission="observability:manage" type="button" className="inf-link" data-testid="obs-win-delete"
                               onClick={() => void drop(w.id, w.name)}>Delete</GatedButton>
                </li>
              ))}
            </ul>
          ) : null}
          {d.inhibit.length ? (
            <ul className="obs-list" data-testid="obs-inhibits">
              {d.inhibit.map((i) => (
                <li key={i.id} className="obs-line" title="While the first fires, the second is not told">
                  <span className="obs-chip">Inhibit</span>
                  <span className="obs-strong" data-user-text>{i.name}</span>
                  <span className="inf-mono inf-muted" data-copy="data">{matchersWord(i.source)} mutes {matchersWord(i.target)}</span>
                  {i.equal.length ? <span className="inf-faint">same {i.equal.join(", ")}</span> : null}
                  {!i.enabled ? <span className="obs-chip">Off</span> : null}
                  <span className="inf-faint">{i.origin}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
      {sheet === "silence" ? <SilenceAdd projectId={projectId} actor={actor} onClose={() => setSheet(null)} onSaved={done} /> : null}
      {sheet === "window" ? <WindowAdd projectId={projectId} actor={actor} onClose={() => setSheet(null)} onSaved={done} /> : null}
    </section>
  );
}

export function NotifyTab({ projectId, actor }: ObsTabProps) {
  const cps = useContactPoints(projectId);
  return (
    <div className="obs-zone obs-notify" data-testid="obs-notify">
      <ContactPoints projectId={projectId} actor={actor} cps={cps} refresh={cps.refresh} />
      <Policies projectId={projectId} actor={actor} cps={cps.data ?? []} />
      <RoutingPreview projectId={projectId} />
      <SilencesZone projectId={projectId} actor={actor} />
    </div>
  );
}
