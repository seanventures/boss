"use client";

/**
 * SETUP › EVENTS › CONNECT MICROSOFT TEAMS (P9b-1, BOSSAPP-1100; wireframe E3).
 *
 * Single-tenant Azure Bot (Decision D5):
 *   1 Download the BOSS Teams app manifest.json & copy the messaging endpoint URL;
 *   2 Paste Entra App ID, Tenant ID, and write-only App Password (creates the Teams
 *     chat endpoint + Teams intake connection riding on it);
 *   3 Verify Entra aad_oid matching / SSO attestation & run the live round-trip test.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy, DownloadSimple, PaperPlaneTilt, ShieldCheck } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { CheckRow, Field, FormError, FormGrid } from "../../components/form-kit";
import { formatDate } from "../../lib/datetime";
import {
  attestConnection, clearAttestation, createConnection, createTeamsEndpoint, parseConnection, parseEndpoint,
  parseTestMessage, chipOf, readChatEndpoints, readEvents, readIdps, readTestMessage, postTestMessage,
  updateConnection, checkWords, type ChatEndpoint, type Connection, type FlowNode,
} from "../../lib/intake";
import { EvFrame, useEv } from "./frame";
import { EventFlowView } from "./event-flow";
import { AgentName, Said, SourceMark, goEvents, useRead } from "./parts";

/** Teams app manifest v1.17 JSON for sideloading or Teams Developer Portal. */
export function teamsManifestJson(opts: { appId: string; endpointUrl?: string }): string {
  const id = opts.appId.trim() || "00000000-0000-0000-0000-000000000000";
  const manifest = {
    $schema: "https://developer.microsoft.com/en-us/json-schemas/teams/v1.17/MicrosoftTeams.schema.json",
    manifestVersion: "1.17",
    version: "1.0.0",
    id,
    developer: { name: "BOSS", websiteUrl: "https://boss.local", privacyUrl: "https://boss.local/privacy", termsOfUseUrl: "https://boss.local/terms" },
    name: { short: "BOSS", full: "BOSS Agent OS" },
    description: { short: "Talk to BOSS agents and answer decisions in Teams", full: "Connect Microsoft Teams channels and direct messages to BOSS agents with Entra ID identity linking." },
    bots: [{
      botId: id,
      scopes: ["personal", "team", "groupChat"],
      supportsFiles: false,
      isNotificationOnly: false,
      ...(opts.endpointUrl ? { configurationUrl: opts.endpointUrl } : {}),
    }],
    permissions: ["identity", "messageTeamMembers"],
    validDomains: opts.endpointUrl ? (() => { try { return [new URL(opts.endpointUrl).hostname]; } catch { return []; } })() : [],
  };
  return JSON.stringify(manifest, null, 2);
}

const copy = (t: string) => { try { void navigator.clipboard?.writeText(t); } catch { /* no clipboard */ } };
const POLL_MS = 3000;
const WAIT_MS = 120_000;

export function ConnectTeams() {
  const { projectId, conns, reloadConns, agents } = useEv();
  const manage = useGate("integration:manage", projectId);
  const admin = useGate("identity_provider:manage", projectId);
  const endpoints = useRead(() => readChatEndpoints(projectId), [projectId]);
  const idps = useRead(() => (admin.allowed ? readIdps() : Promise.resolve({ ok: true as const, data: [] })), [admin.allowed]);
  const teamsConn = (conns?.ok ? conns.data : []).find((c) => c.kind === "teams") ?? null;
  const endpoint: ChatEndpoint | null = (endpoints.read?.ok ? endpoints.read.data : []).find((e) => e.kind === "teams" && e.status === "active") ?? null;

  const [appId, setAppId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [appPassword, setAppPassword] = useState("");
  const [channel, setChannel] = useState("");
  const [dmAgent, setDmAgent] = useState("");
  const [attest, setAttest] = useState(false);
  const [idp, setIdp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" | "amber" } | null>(null);
  const [test, setTest] = useState<{ started: number; signed: boolean | null; saved: boolean; replied: boolean; waiting: boolean; lines: string[];
    stopped?: { status: string; why: string; id: string } | null } | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (teamsConn) setDmAgent(teamsConn.default_dm_agent);
  }, [teamsConn?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
  const idpList = idps.read?.ok ? idps.read.data.filter((p) => p.status === "active") : [];
  const chosenIdp = idpList.find((p) => p.id === idp) ?? idpList[0] ?? null;

  const stepNow = teamsConn ? 3 : endpoint ? 2 : 1;
  const needSecrets = !endpoint;
  const missing = [
    ...(needSecrets && !appId.trim() ? ["the Microsoft App ID"] : []),
    ...(needSecrets && !tenantId.trim() ? ["the Entra Tenant ID"] : []),
    ...(needSecrets && !appPassword.trim() ? ["the App Password"] : []),
  ];
  const messagingUrl = teamsConn?.url || teamsConn?.url_path || endpoint?.url || "";
  const manifestHref = useMemo(() => {
    const jsonText = teamsManifestJson({ appId: appId || endpoint?.app_id || "", endpointUrl: messagingUrl });
    return `data:application/json;charset=utf-8,${encodeURIComponent(jsonText)}`;
  }, [appId, endpoint?.app_id, messagingUrl]);

  const poll = (connId: string, started: number) => {
    const tick = async () => {
      const [r, probe] = await Promise.all([
        readEvents(projectId, { connection_id: connId, hours: 1, limit: 20 }),
        readTestMessage(connId),
      ]);
      const fresh = r.ok ? r.data.events.filter((e) => Date.parse(e.received_at) >= started - 2000) : [];
      const saved = (probe.ok && probe.data.round_trip) || fresh.length > 0;
      const replied = fresh.some((e) => e.status === "routed" && e.runs.length > 0);
      const stopped = replied ? null : fresh.find((e) => ["refused", "filtered", "ignored", "dead", "failed"].includes(e.status)) ?? null;
      const more = !replied && !stopped && Date.now() - started < WAIT_MS;
      setTest((t) => (t ? { ...t, saved: t.saved || saved, replied: t.replied || replied, waiting: more,
                            stopped: stopped ? { status: stopped.status, why: stopped.last_error || chipOf(stopped.status).tip, id: stopped.id } : null } : t));
      if (more) timer.current = window.setTimeout(() => void tick(), POLL_MS);
    };
    timer.current = window.setTimeout(() => void tick(), POLL_MS);
  };

  const runTest = async (c: Connection) => {
    const started = Date.now();
    setTest({ started, signed: null, saved: false, replied: false, waiting: true, lines: [] });
    const r = await postTestMessage(c.id);
    if (!r.ok) { setTest({ started, signed: false, saved: false, replied: false, waiting: false, lines: [r.error || "The test failed."] }); return; }
    const t = parseTestMessage(r.body);
    const signed = t.ok && t.posted;
    setTest({ started, signed, saved: t.round_trip, replied: false, waiting: signed && !t.round_trip,
              lines: checkWords(t).map((l) => `${l.label}${l.ok ? " ✓" : ""}`) });
    if (signed && !t.round_trip) poll(c.id, started);
  };

  const save = async () => {
    setBusy(true); setError(""); setSaid(null);
    let ep = endpoint;
    if (!ep) {
      const made = await createTeamsEndpoint(projectId, {
        name: "Microsoft Teams", app_id: appId.trim(), tenant_id: tenantId.trim(),
        app_password: appPassword.trim(), channel: channel.trim() || undefined,
      });
      if (!made.ok) { setBusy(false); setError(made.error || "Teams was not saved."); return; }
      ep = parseEndpoint(made.body);
      endpoints.reload();
    }
    let c = teamsConn;
    if (!c) {
      const r = await createConnection(projectId, {
        kind: "teams", name: "Microsoft Teams", mode: "push",
        config: { chat_endpoint_id: ep.id, ...(dmAgent ? { default_dm_agent: dmAgent } : {}) },
        secrets: {},
      });
      if (!r.ok) { setBusy(false); setError(r.error || "The connection was not saved."); return; }
      c = parseConnection(r.body);
    } else if (dmAgent !== c.default_dm_agent) {
      const r = await updateConnection(c.id, { config: { default_dm_agent: dmAgent } });
      if (!r.ok) { setBusy(false); setError(r.error || "Not saved."); return; }
    }
    if (attest && admin.allowed && chosenIdp && !c.attestation) {
      const r = await attestConnection(c.id, chosenIdp.id, chosenIdp.domains);
      if (!r.ok) setSaid({ text: `Saved, but the SSO link was not confirmed: ${r.error}`, tone: "amber" });
    }
    setAppPassword("");
    setBusy(false);
    reloadConns();
    await runTest(c);
  };

  const turnOffAttestation = async () => {
    if (!teamsConn) return;
    const r = await clearAttestation(teamsConn.id);
    setSaid(r.ok ? { text: "SSO auto-link is off. Home-tenant Entra ID or one-time codes link users.", tone: "green" } : { text: r.error || "Not changed.", tone: "red" });
    if (r.ok) reloadConns();
  };

  const flow: FlowNode[] = useMemo(() => test ? [
    { key: "signed", label: "Signed", detail: test.signed === null ? "checking" : test.signed ? "token ✓" : "failed", tip: test.lines.join(" · ") || "Entra client credentials resolve",
      state: test.signed === null ? "active" : test.signed ? "done" : "failed" },
    { key: "saved", label: "Saved", detail: test.saved ? "your @BOSS" : test.waiting ? "waiting" : "nothing came", tip: "BOSS verified the Bot Framework JWT",
      state: test.saved ? "done" : test.signed && test.waiting ? "active" : "skipped" },
    test.stopped ? { key: "replied", label: chipOf(test.stopped.status).word, detail: "see the stream", tip: test.stopped.why, state: "failed" as const }
      : { key: "replied", label: "Replied", detail: test.replied ? "in thread" : "", tip: "A rule woke an agent and it answered",
          state: test.replied ? "done" : test.saved && test.waiting ? "active" : test.saved ? "waiting" : "skipped" },
  ] : [], [test]);

  const agentList = agents?.ok ? agents.data : [];
  const att = teamsConn?.attestation ?? null;

  return (
    <EvFrame tab="connect" crumb="Connect Microsoft Teams" testId="ev-connect-teams"
             title={<><SourceMark kind="teams" />Connect Microsoft Teams</>} sub="Single-tenant Azure Bot for this BOSS. Replies and decision cards go back to Teams.">
      <ol className="ev-steps" aria-label="Steps">
        {["Register Azure Bot", "App credentials", "Entra & test"].map((w, i) => (
          <React.Fragment key={w}>
            {i ? <li className="ev-step-wire" aria-hidden /> : null}
            <li className="ev-step" data-state={i + 1 < stepNow || (i + 1 === 3 && test?.replied) ? "done" : i + 1 === stepNow ? "now" : undefined}>
              <span className="ev-step-n">{i + 1 < stepNow ? <Check size={12} weight="bold" aria-hidden /> : i + 1}</span><span className="ev-step-label">{w}</span>
            </li>
          </React.Fragment>
        ))}
      </ol>
      <div className="ev-split">
        <div className="ev-form">
          <section className="ev-card" aria-labelledby="ev-t1">
            <h2 id="ev-t1" className="ev-h2">{stepNow > 1 ? "Azure Bot & Teams manifest" : "1 · Register a single-tenant Azure Bot"}</h2>
            <span className="ev-faint">Create a single-tenant Azure Bot in your Entra tenant and upload the BOSS Teams app manifest.</span>
            <div className="ev-row-line">
              <a className="ev-btn" href={manifestHref} download="boss-teams-manifest.json" data-testid="ev-teams-manifest">
                <DownloadSimple size={15} aria-hidden />Download manifest.json
              </a>
            </div>
            {messagingUrl ? (
              <div className="ev-copy" title="Azure Bot › Configuration › Messaging endpoint">
                <code data-testid="ev-teams-url">{messagingUrl}</code>
                <button type="button" className="ev-btn" data-size="sm" onClick={() => copy(messagingUrl)}><Copy size={13} aria-hidden /> Copy</button>
              </div>
            ) : null}
          </section>

          <section className="ev-card" aria-labelledby="ev-t2">
            <h2 id="ev-t2" className="ev-h2">{endpoint ? "Credentials saved in BOSS's vault" : "2 · Paste Entra App ID, Tenant ID & App Password"}</h2>
            {endpoint ? (
              <span className="ev-faint" data-testid="ev-teams-endpoint">Uses “{endpoint.name}” · app {endpoint.app_id || "configured"} · {endpoint.secrets_set.length} secret stored, never shown</span>
            ) : (
              <FormGrid label="Azure Bot credentials">
                <Field label="Microsoft App ID" tip="Azure Bot › Configuration › Microsoft App ID (client UUID)">
                  <input value={appId} onChange={(e) => setAppId(e.target.value)} placeholder="11111111-2222-3333-4444-555555555555" data-testid="ev-teams-appid" />
                </Field>
                <Field label="Entra Tenant ID" tip="Home Entra tenant UUID (Decision D5: single-tenant)">
                  <input value={tenantId} onChange={(e) => setTenantId(e.target.value)} placeholder="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" data-testid="ev-teams-tenantid" />
                </Field>
                <Field label={<>App Password <span className="ev-tag-ro" title="Stored in the vault; never logged or exposed to VMs.">Write-only</span></>}
                       tip="Entra App registration › Certificates & secrets">
                  <input type="password" autoComplete="off" value={appPassword} onChange={(e) => setAppPassword(e.target.value)} data-testid="ev-teams-secret" />
                </Field>
                <Field label="Default conversation id (optional)" tip="Teams channel or conversation id for decision cards & test message">
                  <input value={channel} onChange={(e) => setChannel(e.target.value)} placeholder="19:general@thread.tacv2" data-testid="ev-teams-channel" />
                </Field>
              </FormGrid>
            )}
          </section>

          <section className="ev-card" aria-labelledby="ev-t-sso" data-testid="ev-teams-sso">
            <div className="ev-row-line"><h2 id="ev-t-sso" className="ev-h2">Entra ID matching &amp; SSO</h2><span className="ev-tag-ro">Home tenant</span></div>
            <span className="ev-faint">Non-guest home-tenant members matching an Entra <code className="ev-code">aad_oid</code> sign-in link automatically. Guests are never auto-linked.</span>
            {admin.allowed ? (
              att ? (
                <>
                  <span className="ev-row-line"><ShieldCheck size={16} aria-hidden /> SSO auto-link on · {att.idp_name} · {att.domains.join(", ")}</span>
                  <span className="ev-faint">Confirmed by {att.attested_by} · {formatDate(att.attested_at)}</span>
                  <div><GatedButton permission="identity_provider:manage" projectId={projectId} className="ev-btn" data-size="sm" onClick={() => void turnOffAttestation()}
                                    data-testid="ev-teams-sso-off">Turn off</GatedButton></div>
                </>
              ) : idpList.length ? (
                <>
                  {idpList.length > 1 ? (
                    <Field label="Sign-in provider"><select value={chosenIdp?.id || ""} onChange={(e) => setIdp(e.target.value)} data-testid="ev-teams-idp">
                      {idpList.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
                  ) : null}
                  <CheckRow checked={attest} onChange={() => setAttest((v) => !v)} testId="ev-teams-attest"
                            label={`Allow email matching with ${chosenIdp?.name || "Entra ID"} for ${chosenIdp?.domains.join(", ") || "verified domains"}`} />
                </>
              ) : null
            ) : null}
          </section>
        </div>

        <aside className="ev-form" aria-label="Who answers and the test">
          <section className="ev-card" aria-labelledby="ev-t-dm">
            <h2 id="ev-t-dm" className="ev-h2">Who answers</h2>
            <Field label="Default agent" tip="Answers personal Teams messages and @mentions with no agent named.">
              <select value={dmAgent} onChange={(e) => setDmAgent(e.target.value)} data-testid="ev-teams-dm-agent">
                <option value="">The rules decide</option>
                {agentList.map((g) => <option key={g.key} value={g.key}>{g.name}</option>)}
              </select>
            </Field>
            {dmAgent ? <AgentName agentKey={dmAgent} name={agentList.find((g) => g.key === dmAgent)?.name} /> : null}
          </section>

          <section className="ev-card" aria-labelledby="ev-t3" data-testid="ev-teams-test">
            <h2 id="ev-t3" className="ev-h2">3 · Test</h2>
            <span className="ev-faint">Verifies Bot Framework token exchange, posts to the configured conversation, and watches for your @BOSS mention.</span>
            {test ? <EventFlowView nodes={flow} size="sm" testId="ev-teams-flow" /> : null}
            {test?.stopped ? <button type="button" className="ev-link" onClick={() => goEvents({ events: "stream", ev: test.stopped?.id || null, kind: null })} data-testid="ev-teams-stopped">
              Teams reached BOSS; {chipOf(test.stopped.status).word.toLowerCase()} there. Open it in the stream</button> : null}
            {test?.replied ? <span className="ev-calm"><Check size={16} aria-hidden /> Microsoft Teams works end to end.</span> : null}
          </section>
          <FormError>{error}</FormError>
          <Said said={said} testId="ev-teams-said" />
          <div className="ev-row-line">
            <button type="button" className="ev-btn" data-kind="ghost" onClick={() => goEvents({ events: "connections", kind: null })} data-testid="ev-teams-back">Back</button>
            <span className="ev-spacer" />
            {teamsConn ? (
              <>
                {dmAgent !== teamsConn.default_dm_agent ? (
                  <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" onClick={() => void save()} data-testid="ev-teams-save-agent">Save</GatedButton>
                ) : null}
                <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-kind="primary" disabled={busy || Boolean(test?.waiting)}
                             onClick={() => void runTest(teamsConn)} data-testid="ev-teams-test-btn"><PaperPlaneTilt size={15} aria-hidden />{test?.waiting ? "Waiting…" : "Test"}</GatedButton>
              </>
            ) : (
              <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-kind="primary" disabled={busy || missing.length > 0}
                           title={missing.length ? `Fill in ${missing.join(", ")}` : undefined} onClick={() => void save()} data-testid="ev-teams-save">
                {busy ? "Saving…" : "Save and test"}
              </GatedButton>
            )}
          </div>
          {!manage.allowed ? <span className="ev-faint">{manage.reason}</span> : null}
        </aside>
      </div>
    </EvFrame>
  );
}
