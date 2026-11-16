"use client";

/**
 * SETUP › EVENTS › CONNECT SLACK (P9a-7, wireframe E2). One Slack app for this BOSS; every agent
 * answers through it (boss-eventing-lld.md §15 D1). Three steps:
 *
 *   1 Create the app from the BOSS manifest (a link that opens Slack with every scope and event
 *     filled in; Socket Mode when BOSS has no public address);
 *   2 Paste two secrets (write-only: the vault keeps them, nobody reads them back) and the channel
 *     BOSS posts in. This creates the install's Slack chat endpoint (routers/chat_channels.py),
 *     then the Slack intake connection riding on it. An endpoint that already exists is reused;
 *   3 Test: the secrets resolve, BOSS posts in the channel you picked, then waits for your
 *     @BOSS and draws Signed → Saved → Replied as each happens.
 *
 * Beside the steps: who answers a DM that names no agent (the connection's default DM agent),
 * and, for an identity-provider admin only, the SSO attestation that lets a Slack email count as
 * a BOSS sign-in (PUT …/sso-attestation; hidden without identity_provider:manage).
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { ArrowSquareOut, Check, Copy, PaperPlaneTilt, ShieldCheck } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { CheckRow, Field, FormError, FormGrid } from "../../components/form-kit";
import { formatDate } from "../../lib/datetime";
import {
  attestConnection, clearAttestation, createConnection, createSlackEndpoint, parseConnection, parseEndpoint, parseTestMessage,
  chipOf, readChatEndpoints, readEvents, readIdps, readTestMessage, postTestMessage, updateConnection, checkWords, type ChatEndpoint, type Connection, type FlowNode,
} from "../../lib/intake";
import { EvFrame, useEv } from "./frame";
import { EventFlowView } from "./event-flow";
import { AgentName, Said, SourceMark, goEvents, useRead } from "./parts";

const SCOPES = ["app_mentions:read", "channels:history", "groups:history", "im:history", "im:read", "im:write", "chat:write",
                "users:read", "users:read.email", "reactions:write"];
const EVENTS = ["app_mention", "message.im", "message.channels", "message.groups"];

/** The Slack app manifest for this BOSS, as Slack's "create from manifest" link. */
export function manifestLink(opts: { socket: boolean; requestUrl: string }): string {
  const settings: Record<string, unknown> = {
    event_subscriptions: { bot_events: EVENTS, ...(opts.socket || !opts.requestUrl ? {} : { request_url: opts.requestUrl }) },
    interactivity: { is_enabled: true, ...(opts.socket || !opts.requestUrl ? {} : { request_url: opts.requestUrl }) },
    org_deploy_enabled: false, socket_mode_enabled: opts.socket, token_rotation_enabled: false,
  };
  const manifest = {
    display_information: { name: "BOSS", description: "Talk to your BOSS agents" },
    features: { bot_user: { display_name: "BOSS", always_online: true }, app_home: { messages_tab_enabled: true, messages_tab_read_only_enabled: false } },
    oauth_config: { scopes: { bot: SCOPES } },
    settings,
  };
  return `https://api.slack.com/apps?new_app=1&manifest_json=${encodeURIComponent(JSON.stringify(manifest))}`;
}

const copy = (t: string) => { try { void navigator.clipboard?.writeText(t); } catch { /* no clipboard */ } };
const POLL_MS = 3000;
const WAIT_MS = 120_000;

export function ConnectSlack() {
  const { projectId, conns, reloadConns, agents } = useEv();
  const manage = useGate("integration:manage", projectId);
  const admin = useGate("identity_provider:manage", projectId);
  const endpoints = useRead(() => readChatEndpoints(projectId), [projectId]);
  const idps = useRead(() => (admin.allowed ? readIdps() : Promise.resolve({ ok: true as const, data: [] })), [admin.allowed]);
  const slackConn = (conns?.ok ? conns.data : []).find((c) => c.kind === "slack") ?? null;
  const endpoint: ChatEndpoint | null = (endpoints.read?.ok ? endpoints.read.data : []).find((e) => e.kind === "slack" && e.status === "active") ?? null;

  const [socket, setSocket] = useState(false);
  const [signing, setSigning] = useState("");
  const [bot, setBot] = useState("");
  const [appToken, setAppToken] = useState("");
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
    if (slackConn) { setSocket(slackConn.mode === "socket"); setDmAgent(slackConn.default_dm_agent); }
  }, [slackConn?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
  const idpList = idps.read?.ok ? idps.read.data.filter((p) => p.status === "active") : [];
  const chosenIdp = idpList.find((p) => p.id === idp) ?? idpList[0] ?? null;

  const stepNow = slackConn ? 3 : endpoint ? 2 : 1;
  const needSecrets = !endpoint;
  const missing = [
    ...(needSecrets && !signing.trim() ? ["the signing secret"] : []),
    ...(needSecrets && !bot.trim() ? ["the bot token"] : []),
    ...(needSecrets && !channel.trim() ? ["the channel"] : []),
    ...(socket && !slackConn && !appToken.trim() ? ["the app-level token"] : []),
  ];
  const requestUrl = endpoint?.url || "";

  const poll = (connId: string, started: number) => {
    const tick = async () => {
      const [r, probe] = await Promise.all([
        readEvents(projectId, { connection_id: connId, hours: 1, limit: 20 }),
        readTestMessage(connId),
      ]);
      const fresh = r.ok ? r.data.events.filter((e) => Date.parse(e.received_at) >= started - 2000) : [];
      const saved = (probe.ok && probe.data.round_trip) || fresh.length > 0;
      const replied = fresh.some((e) => e.status === "routed" && e.runs.length > 0);
      // Stopped short: a guard said no (an unlinked person, no grant yet), or no rule matched.
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
      const made = await createSlackEndpoint(projectId, { name: "Slack", channel: channel.trim().replace(/^#/, ""), signing_secret: signing.trim(), bot_token: bot.trim() });
      if (!made.ok) { setBusy(false); setError(made.error || "Slack was not saved."); return; }
      ep = parseEndpoint(made.body);
      endpoints.reload();
    }
    let c = slackConn;
    if (!c) {
      const r = await createConnection(projectId, {
        kind: "slack", name: "Slack", mode: socket ? "socket" : "push",
        config: { chat_endpoint_id: ep.id, ...(dmAgent ? { default_dm_agent: dmAgent } : {}) },
        secrets: socket && appToken.trim() ? { app_token: appToken.trim() } : {},
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
    setSigning(""); setBot(""); setAppToken("");
    setBusy(false);
    reloadConns();
    await runTest(c);
  };

  const turnOffAttestation = async () => {
    if (!slackConn) return;
    const r = await clearAttestation(slackConn.id);
    setSaid(r.ok ? { text: "SSO auto-link is off. New people link with a one-time code.", tone: "green" } : { text: r.error || "Not changed.", tone: "red" });
    if (r.ok) reloadConns();
  };

  const flow: FlowNode[] = useMemo(() => test ? [
    { key: "signed", label: "Signed", detail: test.signed === null ? "checking" : test.signed ? "secrets ✓" : "failed", tip: test.lines.join(" · ") || "The secrets resolve",
      state: test.signed === null ? "active" : test.signed ? "done" : "failed" },
    { key: "saved", label: "Saved", detail: test.saved ? "your @BOSS" : test.waiting ? "waiting" : "nothing came", tip: "BOSS stored your message once",
      state: test.saved ? "done" : test.signed && test.waiting ? "active" : "skipped" },
    test.stopped ? { key: "replied", label: chipOf(test.stopped.status).word, detail: "see the stream", tip: test.stopped.why, state: "failed" as const }
      : { key: "replied", label: "Replied", detail: test.replied ? "in thread" : "", tip: "A rule woke an agent and it answered",
          state: test.replied ? "done" : test.saved && test.waiting ? "active" : test.saved ? "waiting" : "skipped" },
  ] : [], [test]);

  const agentList = agents?.ok ? agents.data : [];
  const att = slackConn?.attestation ?? null;

  return (
    <EvFrame tab="connect" crumb="Connect Slack" testId="ev-connect-slack"
             title={<><SourceMark kind="slack" />Connect Slack</>} sub="One Slack app for this BOSS. Every agent answers through it.">
      <ol className="ev-steps" aria-label="Steps">
        {["Create the app", "Paste secrets", "Test"].map((w, i) => (
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
          <section className="ev-card" aria-labelledby="ev-s1">
            <h2 id="ev-s1" className="ev-h2">{stepNow > 1 ? "App created from the BOSS manifest" : "1 · Create the app from the BOSS manifest"}</h2>
            <span className="ev-faint">Slack opens with every scope and event filled in. Pick your workspace, then Create.</span>
            <div className="ev-row-line">
              <a className="ev-btn" href={manifestLink({ socket, requestUrl })} target="_blank" rel="noreferrer noopener" data-testid="ev-slack-manifest"
                 title="Opens Slack with every scope and event pre-filled"><ArrowSquareOut size={15} aria-hidden />Open manifest link</a>
              {requestUrl && !socket ? <span className="ev-faint">Request URL already in it</span> : null}
            </div>
            {endpoint && !socket ? (
              <div className="ev-copy" title="Slack › Event Subscriptions and Interactivity › Request URL">
                <code data-testid="ev-slack-url">{requestUrl}</code>
                <button type="button" className="ev-btn" data-size="sm" onClick={() => copy(requestUrl)}><Copy size={13} aria-hidden /> Copy</button>
              </div>
            ) : null}
          </section>

          <section className="ev-card" aria-labelledby="ev-s2">
            <h2 id="ev-s2" className="ev-h2">{endpoint ? "Secrets saved in BOSS's vault" : "2 · Paste two secrets from the Slack app page"}</h2>
            {endpoint ? (
              <span className="ev-faint" data-testid="ev-slack-endpoint">Uses “{endpoint.name}” · posts in #{endpoint.channel} · {endpoint.secrets_set.length} secrets stored, never shown</span>
            ) : (
              <FormGrid label="Slack secrets">
                <Field label={<>Signing secret <span className="ev-tag-ro" title="BOSS stores it in the vault. Nobody, including you, can read it back. Paste a new one to replace it.">Write-only</span></>}
                       tip="Basic Information › App Credentials">
                  <input type="password" autoComplete="off" value={signing} onChange={(e) => setSigning(e.target.value)} data-testid="ev-slack-signing" aria-describedby="ev-sig-h" />
                </Field>
                <Field label={<>Bot token <span className="ev-tag-ro" title="BOSS stores it in the vault. Nobody, including you, can read it back. Paste a new one to replace it.">Write-only</span></>}
                       tip="xoxb-… · OAuth & Permissions">
                  <input type="password" autoComplete="off" value={bot} onChange={(e) => setBot(e.target.value)} placeholder="xoxb-…" data-testid="ev-slack-bot" />
                </Field>
                <Field label="Channel BOSS posts in" tip="The channel id (C…): decision cards and the test go here. Invite BOSS to it first.">
                  <input value={channel} onChange={(e) => setChannel(e.target.value)} placeholder="C0123ABCD" data-testid="ev-slack-channel" />
                </Field>
              </FormGrid>
            )}
            {endpoint ? null : <span id="ev-sig-h" className="ev-faint">Basic Information › App Credentials, and OAuth &amp; Permissions for the bot token.</span>}
            <div className="ev-toggle-row">
              <button type="button" role="switch" aria-checked={socket} aria-label="Socket Mode" className="ev-switch" data-testid="ev-slack-socket"
                      {...(slackConn ? { "aria-disabled": true, title: "Set when the connection was made" } : {})}
                      onClick={() => { if (!slackConn) setSocket((v) => !v); }} />
              <div><b>Socket Mode</b><span className="ev-faint" title="Turn on to paste an app-level token (xapp-…). Webhooks are then not needed.">No public address, like a laptop? Slack connects out to BOSS instead.</span></div>
            </div>
            {socket && !slackConn ? (
              <Field label={<>App-level token <span className="ev-tag-ro" title="Stored in the vault; read only when the socket opens.">Write-only</span></>} tip="Basic Information › App-Level Tokens, with connections:write">
                <input type="password" autoComplete="off" value={appToken} onChange={(e) => setAppToken(e.target.value)} placeholder="xapp-…" data-testid="ev-slack-apptoken" />
              </Field>
            ) : null}
          </section>

          {admin.allowed ? (
            <section className="ev-card" aria-labelledby="ev-sso" data-testid="ev-slack-sso">
              <div className="ev-row-line"><h2 id="ev-sso" className="ev-h2">Link people automatically</h2><span className="ev-tag-ro">Optional</span></div>
              {att ? (
                <>
                  <span className="ev-row-line"><ShieldCheck size={16} aria-hidden /> SSO auto-link on · {att.idp_name} · {att.domains.join(", ")}</span>
                  <span className="ev-faint">Confirmed by {att.attested_by} · {formatDate(att.attested_at)}</span>
                  <div><GatedButton permission="identity_provider:manage" projectId={projectId} className="ev-btn" data-size="sm" onClick={() => void turnOffAttestation()}
                                    data-testid="ev-slack-sso-off">Turn off</GatedButton></div>
                </>
              ) : idpList.length ? (
                <>
                  {idpList.length > 1 ? (
                    <Field label="Sign-in provider"><select value={chosenIdp?.id || ""} onChange={(e) => setIdp(e.target.value)} data-testid="ev-slack-idp">
                      {idpList.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
                  ) : null}
                  <CheckRow checked={attest} onChange={() => setAttest((v) => !v)} testId="ev-slack-attest"
                            label={`This workspace signs in with ${chosenIdp?.name || "your provider"}, the same as BOSS, for ${chosenIdp?.domains.join(", ") || "its domains"}`} />
                  <span className="ev-faint">A Slack email then counts as a BOSS sign-in. Confirm only if Slack enforces {chosenIdp?.name || "it"}.</span>
                  <span className="ev-faint">Guests and Slack Connect users are never linked.</span>
                  {attest ? <span className="ev-faint">Confirmed by you · logged with your name and time</span> : null}
                </>
              ) : <span className="ev-faint">Add a sign-in provider with verified domains first (Identity › Sign-in).</span>}
            </section>
          ) : null}
        </div>

        <aside className="ev-form" aria-label="Who answers and the test">
          <section className="ev-card" aria-labelledby="ev-dm">
            <h2 id="ev-dm" className="ev-h2">Who answers</h2>
            <Field label="Default agent" tip="For DMs and “@BOSS” with no agent named. Say “@BOSS triage: …” to pick another.">
              <select value={dmAgent} onChange={(e) => setDmAgent(e.target.value)} data-testid="ev-slack-dm-agent">
                <option value="">The rules decide</option>
                {agentList.map((g) => <option key={g.key} value={g.key}>{g.name}</option>)}
              </select>
            </Field>
            {dmAgent ? <AgentName agentKey={dmAgent} name={agentList.find((g) => g.key === dmAgent)?.name} /> : null}
            <span className="ev-faint">For DMs and “@BOSS” with no agent named. Say “@BOSS triage: …” to pick another.</span>
          </section>
          <section className="ev-card" aria-labelledby="ev-s3" data-testid="ev-slack-test">
            <h2 id="ev-s3" className="ev-h2">3 · Test</h2>
            <span className="ev-faint">{endpoint?.channel || channel ? `BOSS posts in #${(endpoint?.channel || channel).replace(/^#/, "")} and waits for your @BOSS.` : "BOSS posts in the channel you pick and waits for your @BOSS."} BOSS shows each step here.</span>
            {test ? <EventFlowView nodes={flow} size="sm" testId="ev-slack-flow" /> : null}
            {test && !test.waiting && !test.replied && test.signed ? <span className="ev-faint">Nothing arrived yet. Check the Request URL in Slack, then Test again.</span> : null}
            {test?.stopped ? <button type="button" className="ev-link" onClick={() => goEvents({ events: "stream", ev: test.stopped?.id || null, kind: null })} data-testid="ev-slack-stopped">
              Slack reached BOSS; {chipOf(test.stopped.status).word.toLowerCase()} there. Open it in the stream</button> : null}
            {test?.replied ? <span className="ev-calm"><Check size={16} aria-hidden /> Slack works end to end.</span> : null}
          </section>
          <FormError>{error}</FormError>
          <Said said={said} testId="ev-slack-said" />
          <div className="ev-row-line">
            <button type="button" className="ev-btn" data-kind="ghost" onClick={() => goEvents({ events: "connections", kind: null })} data-testid="ev-slack-back">Back</button>
            <span className="ev-spacer" />
            {slackConn ? (
              <>
                {dmAgent !== slackConn.default_dm_agent ? (
                  <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" onClick={() => void save()} data-testid="ev-slack-save-agent">Save</GatedButton>
                ) : null}
                <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-kind="primary" disabled={busy || Boolean(test?.waiting)}
                             onClick={() => void runTest(slackConn)} data-testid="ev-slack-test-btn"><PaperPlaneTilt size={15} aria-hidden />{test?.waiting ? (test.saved ? "Waiting for the reply…" : "Waiting for @BOSS…") : "Test"}</GatedButton>
              </>
            ) : (
              <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-kind="primary" disabled={busy || missing.length > 0}
                           title={missing.length ? `Paste ${missing.join(", ")}` : undefined} onClick={() => void save()} data-testid="ev-slack-save">
                {busy ? "Saving…" : "Save and test"}</GatedButton>
            )}
          </div>
          {!manage.allowed ? <span className="ev-faint">{manage.reason}</span> : null}
        </aside>
      </div>
    </EvFrame>
  );
}
