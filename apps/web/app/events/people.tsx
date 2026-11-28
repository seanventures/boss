"use client";

/**
 * SETUP › EVENTS › PEOPLE (P9a-7, wireframe E7): who each Slack user is in BOSS, per chat
 * connection (GET /v1/intake/connections/{id}/people). The SSO attestation on top (on: which
 * provider, which domains, who confirmed it and when, Turn off for an identity-provider admin;
 * off: people link with a one-time code). Then the counts, a filter and one row per chat user:
 * linked (the BOSS person, a method chip: Code / SSO email / Entra ID, when, Revoke), not linked
 * (when a link prompt last went), a guest (never auto-linked), a link revoked in the last 30 days.
 */
import React, { useEffect, useState } from "react";
import { ShieldCheck, UsersThree } from "@phosphor-icons/react";

import { GatedButton, useGate } from "../../components/gated";
import { formatDate, formatRelativeTime } from "../../lib/datetime";
import { clearAttestation, methodWords, promptPeople, readPeople, revokePerson, type Person, type PersonState } from "../../lib/intake";
import { useNow } from "../agents/live-util";
import { EvFrame, useConnPick, useEv } from "./frame";
import { Chip, Empty, Face, ReadState, Said, SourceMark, goEvents, useRead } from "./parts";

const FILTERS: { key: "all" | PersonState; word: string }[] = [
  { key: "all", word: "All" }, { key: "linked", word: "Linked" }, { key: "not_linked", word: "Not linked" }, { key: "guest", word: "Guests" },
  { key: "revoked", word: "Revoked" },
];
const METHOD_TIPS: Record<string, string> = {
  sso_email: "Matched by the sign-in provider's email on an attested workspace",
  code: "The person signed in once with a one-time code",
  aad_oid: "Matched by their Entra ID object id (no email involved)",
};

export function PeopleTab() {
  const { projectId, conns } = useEv();
  const { list, picked } = useConnPick((c) => c.kind === "slack" || c.kind === "teams");
  const admin = useGate("identity_provider:manage", projectId);
  const manage = useGate("integration:manage", projectId);
  const now = useNow(60_000);
  const { read, reload } = useRead(() => (picked ? readPeople(picked.id) : Promise.resolve(null as never)), [picked?.id]);
  const [filter, setFilter] = useState<"all" | PersonState>("all");
  const [said, setSaid] = useState<{ text: string; tone: "green" | "red" | "amber" } | null>(null);
  const [confirm, setConfirm] = useState<Person | null>(null);
  const [extra, setExtra] = useState<Person[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const data = read?.ok ? read.data : null;
  const att = data?.attestation ?? null;

  useEffect(() => {
    setExtra([]);
    setNextOffset(data ? data.next_offset : null);
  }, [picked?.id, data]);

  const allPeople = (() => {
    const base = data?.people ?? [];
    if (!extra.length) return base;
    const seen = new Set(base.map((p) => `${p.external_user_id}:${p.link_id}`));
    const merged = [...base];
    for (const p of extra) {
      const k = `${p.external_user_id}:${p.link_id}`;
      if (!seen.has(k)) { seen.add(k); merged.push(p); }
    }
    return merged;
  })();
  const rows = allPeople.filter((p) => filter === "all" || p.state === filter);
  const ago = (iso: string) => (iso && now ? formatRelativeTime(iso, now) : "");

  const loadMore = async () => {
    if (!picked || nextOffset === null || loadingMore) return;
    setLoadingMore(true);
    const r = await readPeople(picked.id, { limit: data?.limit || 200, offset: nextOffset });
    setLoadingMore(false);
    if (!r.ok) { setSaid({ text: r.error || "Could not load more people.", tone: "red" }); return; }
    setExtra((cur) => [...cur, ...r.data.people]);
    setNextOffset(r.data.next_offset);
  };

  const promptWords = (body: unknown): string => {
    const x = body && typeof body === "object" ? body as Record<string, unknown> : {};
    const sent = Array.isArray(x.sent) ? x.sent.length : 0;
    const already = Array.isArray(x.already_sent) ? x.already_sent.length : 0;
    if (sent && already) return `Sent ${sent}. ${already} already had a prompt this hour.`;
    if (sent === 1) return "Link prompt sent.";
    if (sent > 1) return `Link prompts sent to ${sent} people.`;
    if (already) return "Already sent this hour.";
    return "Nobody to prompt.";
  };
  const sendPrompts = async (user = "", resend = false) => {
    if (!picked) return;
    const r = await promptPeople(picked.id, user, resend);
    setSaid(r.ok ? { text: promptWords(r.body), tone: "green" } : { text: r.error || "Not sent.", tone: "red" });
    if (r.ok) reload();
  };
  const revoke = async (p: Person) => {
    setConfirm(null);
    if (!picked) return;
    const r = await revokePerson(picked.id, p.link_id);
    setSaid(r.ok ? { text: `${p.display || p.external_user_id} unlinked. Their next message gets a new link prompt.`, tone: "green" } : { text: r.error || "Not revoked.", tone: "red" });
    if (r.ok) reload();
  };
  const attestOff = async () => {
    if (!picked) return;
    const r = await clearAttestation(picked.id);
    setSaid(r.ok ? { text: "SSO auto-link is off. New people link with a one-time code.", tone: "green" } : { text: r.error || "Not changed.", tone: "red" });
    if (r.ok) reload();
  };

  return (
    <EvFrame tab="people" title="People links" sub="Who each Slack or Teams user is in BOSS.">
      {conns?.ok && !list.length ? (
        <Empty icon={<UsersThree size={30} aria-hidden />} title="No chat connection yet" line="Connect Slack or Microsoft Teams first; people link the first time they talk to BOSS." testId="ev-people-none">
          <div className="ev-row-line">
            <button type="button" className="ev-btn" data-kind="primary" onClick={() => goEvents({ events: "connect", kind: "slack" })}>Connect Slack</button>
            <button type="button" className="ev-btn" onClick={() => goEvents({ events: "connect", kind: "teams" })}>Connect Teams</button>
          </div>
        </Empty>
      ) : null}
      {list.length > 1 ? (
        <div className="ev-sources-pick" role="group" aria-label="Connection">
          {list.map((c) => (
            <button key={c.id} type="button" className="ev-pick" aria-pressed={c.id === picked?.id} onClick={() => goEvents({ conn: c.id }, "replace")} data-testid={`ev-people-pick-${c.id}`}>
              <SourceMark kind={c.kind} size="sm" />{c.label}{c.name && c.name !== c.label ? ` · ${c.name}` : ""}</button>
          ))}
        </div>
      ) : picked ? <span className="ev-row-line"><SourceMark kind={picked.kind} size="sm" /><b>{picked.label}</b><span className="ev-faint">{picked.name !== picked.label ? picked.name : ""}</span></span> : null}
      <Said said={said} testId="ev-people-said" />
      {picked ? <ReadState read={read} what="People" onRetry={reload} testId="ev-people" /> : null}
      {data ? (
        <>
          <section className="ev-att" data-off={att ? undefined : ""} aria-labelledby="ev-att-h" data-testid="ev-attestation">
            <ShieldCheck size={22} aria-hidden />
            <div>
              <h2 id="ev-att-h" className="ev-h2">{att ? `SSO auto-link on · ${att.idp_name} · ${att.domains.join(", ")}` : "SSO auto-link off"}</h2>
              <span className="ev-faint">{att ? <>Confirmed by <span className="ev-who"><Face name={att.attested_by} />{att.attested_by}</span> · {formatDate(att.attested_at)}</>
                : "People link with a one-time code the first time they talk to BOSS."}</span>
            </div>
            {att && admin.allowed ? <GatedButton permission="identity_provider:manage" projectId={projectId} className="ev-btn" data-size="sm" onClick={() => void attestOff()} data-testid="ev-att-off">Turn off</GatedButton> : null}
            {!att && admin.allowed && picked ? <button type="button" className="ev-btn" data-size="sm" onClick={() => goEvents({ events: "connect", kind: picked.kind })} data-testid="ev-att-on">Turn on</button> : null}
          </section>
          <div className="ev-tiles">
            <div className="ev-tile" data-tone="green"><span className="ev-tile-label">Linked</span><b className="ev-tile-num" data-testid="ev-count-linked">{data.counts.linked}</b></div>
            <div className="ev-tile" data-tone="amber"><span className="ev-tile-label">Not linked</span><b className="ev-tile-num" data-testid="ev-count-not-linked">{data.counts.not_linked}</b></div>
            <div className="ev-tile"><span className="ev-tile-label">Guests</span><b className="ev-tile-num" data-testid="ev-count-guests">{data.counts.guests}</b></div>
            <div className="ev-tile"><span className="ev-tile-label">Revoked, 30 days</span><b className="ev-tile-num" data-testid="ev-count-revoked">{data.counts.revoked}</b></div>
          </div>
          <section className="ev-card" aria-labelledby="ev-pp-h">
            <div className="ev-row-line">
              <h2 id="ev-pp-h" className="ev-sec-title">People</h2>
              {data.counts.not_linked > 0 ? (
                <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-size="sm"
                             onClick={() => void sendPrompts()} data-testid="ev-send-prompts">Send link prompts · {data.counts.not_linked}</GatedButton>
              ) : null}
              <span className="ev-spacer" />
              <div className="ev-seg" role="radiogroup" aria-label="Show">
                {FILTERS.map((f) => <button key={f.key} type="button" role="radio" aria-checked={filter === f.key} onClick={() => setFilter(f.key)} data-testid={`ev-people-filter-${f.key}`}>{f.word}</button>)}
              </div>
            </div>
            {!rows.length ? <span className="ev-faint" data-testid="ev-people-empty">{allPeople.length ? "Nobody here." : "Nobody has talked to BOSS on this connection yet."}</span> : null}
            <div data-testid="ev-people-list">
              {rows.map((p) => (
                <div key={p.external_user_id + p.link_id} className="ev-person" data-state={p.state} data-testid={`ev-person-${p.external_user_id}`}>
                  <span className="ev-person-id"><Face name={p.display || p.external_user_id} />
                    <span className="ev-row-main"><b>@{p.display || p.external_user_id}</b>{p.display ? <span className="ev-faint">{p.external_user_id}</span> : null}</span></span>
                  <span className="ev-person-boss ev-row-main">
                    {p.person ? <><b>{p.person.name || p.person.principal_id}</b><span className="ev-faint">{p.person.email}</span></>
                      : (p.state === "linked" || p.state === "revoked") && data.people_redacted
                        ? <span className="ev-faint" title="Who each person is needs “View people” (Owner, Admin or Auditor)" data-testid={`ev-person-hidden-${p.external_user_id}`}>A BOSS person · name hidden</span>
                      : p.state === "guest" ? <Chip tone="grey" tip="Guests and people from other companies can only link with a code, if an admin allows it">Guest · never auto-linked</Chip>
                      : <Chip tone="amber" tip="Their messages don't run an agent until they link">Not linked</Chip>}
                  </span>
                  <span className="ev-person-act">
                    {p.state === "linked" || p.state === "revoked" ? <Chip tone={p.state === "revoked" ? "red" : p.method === "sso_email" ? "green" : "blue"} tip={METHOD_TIPS[p.method] || p.method}>
                      {p.state === "revoked" ? "Revoked" : methodWords(p.method)}</Chip> : null}
                    <span className="ev-faint">{p.state === "revoked" ? ago(p.revoked_at) : p.state === "linked" ? ago(p.linked_at) : p.prompted_at ? `Link sent ${ago(p.prompted_at)}` : p.last_seen_at ? `seen ${ago(p.last_seen_at)}` : ""}</span>
                    {p.state === "not_linked" ? (
                      <GatedButton permission="integration:manage" projectId={projectId} className="ev-link"
                                   title="Send the link prompt again, even if one already went this hour."
                                   onClick={() => void sendPrompts(p.external_user_id, true)} data-testid={`ev-resend-${p.external_user_id}`}>Resend</GatedButton>
                    ) : null}
                    {p.state === "linked" ? (
                      confirm?.link_id === p.link_id ? (
                        <>
                          <GatedButton permission="integration:manage" projectId={projectId} className="ev-btn" data-size="sm" data-kind="danger" onClick={() => void revoke(p)} data-testid={`ev-revoke-yes-${p.external_user_id}`}>Revoke</GatedButton>
                          <button type="button" className="ev-btn" data-size="sm" data-kind="ghost" onClick={() => setConfirm(null)}>Keep</button>
                        </>
                      ) : (
                        <GatedButton permission="integration:manage" projectId={projectId} className="ev-link" title="Revoke this link. Their next message gets a new link prompt."
                                     onClick={() => setConfirm(p)} data-testid={`ev-revoke-${p.external_user_id}`}>Revoke</GatedButton>
                      )
                    ) : null}
                  </span>
                </div>
              ))}
            </div>
            {nextOffset !== null ? (
              <div className="ev-row-line ev-people-pager" data-testid="ev-people-pager">
                <span className="ev-faint">Showing {allPeople.length.toLocaleString("en-US")} of {Math.max(data.total, allPeople.length).toLocaleString("en-US")}</span>
                <span className="ev-spacer" />
                <button type="button" className="ev-btn" data-size="sm" disabled={loadingMore} onClick={() => void loadMore()} data-testid="ev-people-more">
                  {loadingMore ? "Loading…" : "Load more"}
                </button>
              </div>
            ) : null}
          </section>
          {!manage.allowed ? <span className="ev-faint">{manage.reason}</span> : null}
        </>
      ) : null}
    </EvFrame>
  );
}
