"use client";

/**
 * SETUP › CONFLUENCE (BOSSAPP-1384): the org's one Confluence connection, under Setup › Documents
 * (a document repository, not a ticket system).
 *
 * Connected ONCE for the org and shared to every project, like the ticket systems' list: the row
 * says where it was added, whose token BOSS uses (as who), and which spaces agents search by
 * default and which are kept in knowledge. Sign-in is the Atlassian account email + an API token
 * (kept in the vault, never read back), or the org's Jira credential when the operator chooses it.
 * "Test connection" is one cheap call to the site. Self-loading; the connect panel is a form, not
 * a drawer, so a first connection is one screen.
 */
import React, { useCallback, useEffect, useState } from "react";
import { BookOpenText, ArrowsClockwise, Plus } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { SectionLabel } from "../section-label";
import {
  connectDocs, disconnectDocs, docsSummary, readDocsConnections, readDocsSpaces, saveDocsSpaces, siteHost,
  readDocsSync, syncDocsKnowledge, syncLine, testDocs, type DocsConnection, type DocsRead, type DocsSpace,
} from "../../lib/docs-connections";
import { RowMenu } from "./parts";

const STATUS: Record<DocsConnection["status"], { word: string; tone: string }> = {
  connected: { word: "Connected", tone: "green" }, needs_attention: { word: "Needs a look", tone: "amber" }, disconnected: { word: "Not connected", tone: "red" },
};
const csv = (v: string) => [...new Set(v.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean))];

export function ConfluenceRow({ projectId, onSummary }: { projectId: string; onSummary?: (words: string) => void }) {
  const [read, setRead] = useState<DocsRead<DocsConnection[]> | null>(null);
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" } | null>(null);
  const [adding, setAdding] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState("");
  const [spaces, setSpaces] = useState<DocsSpace[] | null>(null);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [form, setForm] = useState({ base_url: "", email: "", api_token: "", spaces: "", jira: false, bearer: false });
  const manage = useGate("integration:manage", projectId);
  const knowledge = useGate("knowledge:manage", projectId);

  const load = useCallback(async () => {
    const r = await readDocsConnections(projectId);
    setRead(r);
    onSummary?.(r.ok ? docsSummary(r.data[0] ?? null) : "…");
  }, [projectId, onSummary]);
  useEffect(() => { setRead(null); void load(); }, [load]);

  const c = read?.ok ? read.data[0] ?? null : null;
  const live = c !== null && c.status !== "disconnected";
  const can = manage.allowed && Boolean(c?.can_manage);

  const connect = async () => {
    setSaid(null); setBusy("connect");
    const r = await connectDocs(projectId, {
      base_url: form.base_url.trim(), auth_method: form.bearer ? "bearer" : "basic", email: form.email.trim(), api_token: form.api_token,
      default_spaces: csv(form.spaces), use_jira_credential: form.jira,
    });
    setBusy("");
    if (r.ok) {
      const acct = (r.body?.test as { account?: string } | undefined)?.account;
      setForm({ base_url: "", email: "", api_token: "", spaces: "", jira: false, bearer: false }); // the token never stays in the page
      setAdding(false);
      setSaid({ text: `Confluence is connected${acct ? ` · signed in as ${acct}` : ""}.`, tone: "green" });
      void load();
    } else setSaid({ text: r.error ?? "Could not connect.", tone: "red" });
  };
  const test = async () => {
    setSaid(null); setBusy("test");
    const r = await testDocs(projectId);
    setBusy("");
    const acct = (r.body?.test as { account?: string } | undefined)?.account;
    setSaid(r.ok ? { text: `Confluence works${acct ? ` · signed in as ${acct}` : ""}.`, tone: "green" } : { text: r.error ?? "The test failed.", tone: "red" });
    void load();
  };
  const disconnect = async () => {
    setConfirm(false); setSaid(null);
    const r = await disconnectDocs(projectId);
    setSaid(r.ok ? { text: "Confluence is disconnected. Pages already in knowledge stay.", tone: "green" } : { text: r.error ?? "Not disconnected.", tone: "red" });
    void load();
  };
  const loadSpaces = async () => {
    const r = await readDocsSpaces(projectId);
    if (r.ok) { setSpaces(r.data); setPicked(c?.knowledge_spaces ?? []); } else setSaid({ text: r.error, tone: "red" });
  };
  const saveSpaces = async (patch: { default_spaces?: string[]; knowledge_spaces?: string[] }) => {
    setSaid(null);
    const r = await saveDocsSpaces(projectId, patch);
    setSaid(r.ok ? { text: "Saved.", tone: "green" } : { text: r.error ?? "Not saved.", tone: "red" });
    if (r.ok) { setPicked(null); void load(); }
  };
  const sync = async () => {
    setSaid(null); setBusy("sync");
    const r = await syncDocsKnowledge(projectId);
    if (!r.ok) { setBusy(""); setSaid({ text: r.error ?? "The sync did not start.", tone: "red" }); return; }
    // ONE background job: poll its state until it is no longer running.
    for (let i = 0; i < 400; i += 1) {
      const j = await readDocsSync(projectId);
      if (!j.ok) { setSaid({ text: j.error, tone: "red" }); break; }
      const job = j.data;
      if (job.status === "running") { setSaid({ text: `Syncing… ${syncLine(job)}`, tone: "green" }); await new Promise((res) => setTimeout(res, 1500)); continue; }
      const rows = Object.values(job.spaces);
      const sum = (k: "indexed" | "updated" | "unchanged") => rows.reduce((n, x) => n + x[k], 0);
      setSaid(job.status === "done" ? { text: `Synced: ${sum("indexed")} new, ${sum("updated")} updated, ${sum("unchanged")} unchanged.`, tone: "green" }
        : { text: `The sync ${job.status === "stalled" ? "stalled" : "failed"}${job.error ? `: ${job.error}` : ""}. Press Sync now to resume.`, tone: "red" });
      break;
    }
    setBusy("");
    void load();
  };

  const st = c ? STATUS[c.status] : STATUS.disconnected;
  const ready = form.base_url.trim() && form.api_token.trim() && (form.bearer || form.email.trim());
  const jiraReady = form.jira;
  const canSubmit = form.jira ? true : Boolean(ready);

  return (
    <section className="su-row tk-setup" data-testid="su-row-confluence" aria-label="Confluence" id="su-confluence">
      <div className="su-row-label">
        <SectionLabel as="span" tip="Connect Confluence once for the org. Agents read pages; publishing waits for approval.">Confluence</SectionLabel>
      </div>
      <div className="su-row-body su-stack">
        <div className="tk-row">
          <span className="tk-system-name">
            <span className="tk-mono" data-provider="confluence" style={{ width: 26, height: 26, fontSize: 12 }} aria-hidden><BookOpenText size={15} /></span>
            <span className="tk-stack" style={{ gap: 0 }}>
              <b>Confluence</b>
              <span className="tk-faint" data-testid="cf-site">{live && c ? siteHost(c.base_url) : "Pages, spaces and runbooks"}</span>
            </span>
          </span>
          <span className="tk-spacer" />
          {read?.ok ? <span className="tk-status" data-tone={st.tone} data-testid="cf-status"><i aria-hidden /> {st.word}</span> : null}
          {!live ? (
            <GatedButton permission="integration:manage" projectId={projectId} className="su-btn su-btn-sm" data-testid="cf-add"
                         title="Connect Confluence Cloud for the whole org" onClick={() => setAdding((v) => !v)}>
              <Plus size={13} aria-hidden /> Connect Confluence</GatedButton>
          ) : (
            <RowMenu label="Confluence actions" testid="cf-menu" items={[
              { label: "Test connection", testid: "cf-test", disabled: !can || busy === "test", onSelect: () => void test() },
              { label: "Reconnect", testid: "cf-reconnect", disabled: !can, onSelect: () => setAdding(true) },
              { label: "Disconnect", testid: "cf-off", danger: true, disabled: !can, onSelect: () => setConfirm(true) },
            ]} />
          )}
        </div>

        {read && !read.ok ? <span className="tk-faint" role="status" data-testid="cf-error">{read.error}</span> : null}
        {read?.ok && !live && !adding ? (
          <span className="tk-faint" data-testid="cf-empty"
                title="Connect it once and every project can search and read your pages.">Not connected.</span>
        ) : null}

        {live && c ? (
          <div className="tk-stack" data-testid="cf-detail">
            <span className="tk-faint" data-testid="cf-account">
              {c.acts_as ? `BOSS acts as ${c.acts_as}` : "Connected"}{c.last4 && c.can_manage ? ` · token …${c.last4}` : ""}
              {c.credential_from === "jira" ? " · same credential as Jira" : ""}
              {c.reach === "org" ? ` · Shared · added in ${c.home_project.name}` : " · Shared with the whole org"}
            </span>
            <span className="tk-faint" data-testid="cf-tested">
              {c.last_test_ok === false ? "The last test failed. Reconnect Confluence." : c.last_tested_at ? `Last tested ${new Date(c.last_tested_at).toLocaleString()}` : "Not tested yet"}
            </span>
            <label className="tk-faint" style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              Default spaces
              <input className="su-input" data-testid="cf-default-spaces" defaultValue={c.default_spaces.join(", ")} key={c.default_spaces.join(",")}
                     placeholder="ENG, OPS" aria-label="Default spaces" disabled={!can} style={{ width: 200 }}
                     onBlur={(e) => { const next = csv(e.currentTarget.value); if (next.join(",") !== c.default_spaces.join(",")) void saveSpaces({ default_spaces: next }); }} />
            </label>
            <div className="tk-row" style={{ flexWrap: "wrap" }} data-testid="cf-knowledge">
              <span className="tk-faint">In knowledge:</span>
              {c.knowledge_spaces.length ? c.knowledge_spaces.map((k) => (
                <span key={k} className="tk-mono-text" title={c.sync_state[k]?.synced_at ? `Synced ${c.sync_state[k].synced_at}` : "Not synced yet"}>{k}</span>
              )) : <span className="tk-faint">no spaces yet</span>}
              <button type="button" className="su-btn su-btn-sm" data-testid="cf-pick" disabled={!can} onClick={() => void loadSpaces()}>Choose spaces</button>
              <GatedButton permission="knowledge:manage" projectId={projectId} className="su-btn su-btn-sm" data-testid="cf-sync"
                           title="Index the chosen spaces' pages into this project's knowledge"
                           disabled={!c.knowledge_spaces.length || busy === "sync" || !knowledge.allowed} onClick={() => void sync()}>
                <ArrowsClockwise size={13} aria-hidden /> {busy === "sync" ? "Syncing…" : "Sync now"}</GatedButton>
            </div>
            {spaces && picked ? (
              <div className="tk-confirm" data-kind="spaces" role="group" aria-label="Spaces kept in knowledge" data-testid="cf-space-picker">
                <span>Pick the spaces BOSS keeps in knowledge (pages are re-synced when they change).</span>
                {spaces.map((sp) => (
                  <label key={sp.key} style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                    <input type="checkbox" checked={picked.includes(sp.key)} data-testid={`cf-space-${sp.key}`}
                           onChange={(e) => setPicked(e.currentTarget.checked ? [...picked, sp.key] : picked.filter((k) => k !== sp.key))} />
                    {sp.name || sp.key} <span className="tk-faint">{sp.key}</span>
                  </label>
                ))}
                <button type="button" className="su-btn su-btn-sm su-btn-primary" data-testid="cf-space-save" onClick={() => void saveSpaces({ knowledge_spaces: picked })}>Save spaces</button>
                <button type="button" className="su-btn su-btn-sm" onClick={() => { setSpaces(null); setPicked(null); }}>Cancel</button>
              </div>
            ) : null}
          </div>
        ) : null}

        {adding ? (
          <form className="tk-stack" data-testid="cf-form" onSubmit={(e) => { e.preventDefault(); if (canSubmit) void connect(); }}>
            <label className="tk-stack">Site URL
              <input className="su-input" data-testid="cf-url" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                     placeholder="https://your-team.atlassian.net/wiki" autoComplete="off" />
            </label>
            <label style={{ display: "flex", gap: 8, alignItems: "center" }} className="tk-faint">
              <input type="checkbox" data-testid="cf-jira" checked={form.jira} onChange={(e) => setForm({ ...form, jira: e.target.checked })} />
              Use the org's Jira credential (same Atlassian site; copied into its own vault entry)
            </label>
            {!form.jira ? (
              <>
                <label style={{ display: "flex", gap: 8, alignItems: "center" }} className="tk-faint">
                  <input type="checkbox" data-testid="cf-bearer" checked={form.bearer} onChange={(e) => setForm({ ...form, bearer: e.target.checked })} />
                  Sign in with a bearer token instead of an email + API token
                </label>
                {!form.bearer ? (
                  <label className="tk-stack">Atlassian account email
                    <input className="su-input" data-testid="cf-email" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} autoComplete="off" />
                  </label>
                ) : null}
                <label className="tk-stack">{form.bearer ? "Bearer token" : "API token"}
                  <input className="su-input" data-testid="cf-token" type="password" value={form.api_token} onChange={(e) => setForm({ ...form, api_token: e.target.value })}
                         autoComplete="new-password" aria-describedby="cf-token-help" />
                  <span id="cf-token-help" className="tk-faint">Create at id.atlassian.com › Security › API tokens. It goes to the vault and is never shown again.</span>
                </label>
              </>
            ) : null}
            <label className="tk-stack">Default spaces (optional)
              <input className="su-input" data-testid="cf-spaces" value={form.spaces} onChange={(e) => setForm({ ...form, spaces: e.target.value })} placeholder="ENG, OPS" />
            </label>
            <span className="tk-row">
              <button type="submit" className="su-btn su-btn-sm su-btn-primary" data-testid="cf-connect" disabled={!canSubmit || busy === "connect" || !manage.allowed}>
                {busy === "connect" ? "Testing…" : jiraReady ? "Test and connect with Jira's credential" : "Test and connect"}</button>
              <button type="button" className="su-btn su-btn-sm" onClick={() => setAdding(false)}>Cancel</button>
            </span>
          </form>
        ) : null}

        {confirm ? (
          <div className="tk-confirm" role="alertdialog" aria-label="Disconnect Confluence?" data-testid="cf-confirm">
            <span>Agents lose access to Confluence for every project. Pages already in knowledge stay.</span>
            <button type="button" className="su-btn su-btn-sm su-btn-primary" data-testid="cf-confirm-yes" onClick={() => void disconnect()}>Disconnect</button>
            <button type="button" className="su-btn su-btn-sm" onClick={() => setConfirm(false)}>Cancel</button>
          </div>
        ) : null}
        {said ? <span role="status" className="tk-faint" data-tone={said.tone} data-testid="cf-said">{said.text}</span> : null}
      </div>
    </section>
  );
}
