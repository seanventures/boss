"use client";

/**
 * The Passport header (spec §3.4): who this agent is, in one glance, as a certificate.
 *
 * Left, the 128px face (PASSPORT_FACE_PX), breathing and blinking when idle. Centre, the name, the
 * role, the `AI agent` badge, its lifecycle and its own two lines about itself. Right, the
 * certificate: an ID that never changes, when and by whom it was born, where it came from, the
 * human accountable for it, the version of its definition, and an honest `Local · tokens coming`.
 * Every value here is the API's (`/v1/agents/{role}/passport`) — nothing is computed to look
 * plausible. Below, the vitals strip and the actions; then the section nav.
 *
 * Five quick clicks on the face make it wave and say its first phrase. Local, no model call.
 */
import React, { useEffect, useRef, useState } from "react";
import { Copy, DotsThree, ShareNetwork } from "@phosphor-icons/react";

import type { AvatarState, Passport } from "../../lib/agent-world-types";
import { useIdentity } from "../../lib/use-agent-identities";
import {
  bornDate, bornFromLabel, getAgentCard, humanise, identityOf, money, resolveAvatar, setLifecycle, toast,
  versionLine,
} from "../../lib/agent-world";
import { AgFace, AiBadge } from "./agent-face";

/** Loadout first (agent-loadout spec §3.1): what it can do comes before what it has done. */
export const SECTIONS = [
  { key: "loadout", label: "Loadout" },
  { key: "work", label: "Work" },
  { key: "profile", label: "Profile" },
  { key: "used", label: "Used in" },
  { key: "lineage", label: "Lineage" },
  { key: "settings", label: "Settings" },
] as const;
export type SectionKey = (typeof SECTIONS)[number]["key"];

/** `Born by`, in words: you, a person, BOSS's own pack, or the agent that made it. */
export function bornByLabel(by: string, actor: string): string {
  if (!by || by === "spec-pack") return "BOSS";
  if (by.startsWith("agent:")) return "An agent";
  return by === actor ? "You" : by;
}

/** The vitals strip, as the API counted them. A zero that means "nothing yet" is left out. */
export function vitalsLine(v: Passport["vitals"]): string[] {
  const out = [`Trust ${v.trust}`, `${v.runs} run${v.runs === 1 ? "" : "s"}`];
  if (v.runs > 0) out.push(`${Math.round(v.cleanRate * (v.cleanRate <= 1 ? 100 : 1))}% clean`);
  // Metered money only; subscription use is its own count, never dollars.
  if (v.cost7d > 0) out.push(`${money(v.cost7d)} this week`);
  if (v.planCalls7d > 0) out.push(`${v.planCalls7d} on plan`);
  if (v.panels > 0) out.push(`${v.panels} panel${v.panels === 1 ? "" : "s"}`);
  if (v.apps > 0) out.push(`${v.apps} app${v.apps === 1 ? "" : "s"}`);
  return out;
}

/** What the agent page itself knows (`/api/agents/{role}`), for what a passport does not carry —
 *  violations, the reporting line — and for the whole strip on an API with no passport. */
export type DetailFacts = {
  violations: number; reportsTo: string | null;
  apps: number; spend: number; panels: number;
  /** null for an unregistered agent: nothing governs it, so there is no trust to show. */
  trust: number | null;
};

/** The strip from the agent page's own figures, when there is no passport to count them. */
export function detailVitalsLine(f: DetailFacts): string[] {
  const out: string[] = [];
  if (f.trust != null) out.push(`Trust ${f.trust}`);
  out.push(`${f.apps} app${f.apps === 1 ? "" : "s"}`);
  if (f.spend > 0) out.push(`${money(f.spend)} spent`);
  if (f.panels > 0) out.push(`${f.panels} panel${f.panels === 1 ? "" : "s"}`);
  return out;
}

/** The Passport's face: the hero of the page. */
export const PASSPORT_FACE_PX = 128;

/** Tooltips for the certificate, one short line each (spec §9). */
export const CERT_TIPS = {
  id: "Never changes. Future tokens and policies attach here.",
  born: "When this agent was created.",
  by: "Who created it.",
  from: "What it was made from: a BOSS pack, a template or an archetype.",
  sponsor: "The person accountable for this agent.",
  version: "Changes when its voice, remit, skills or instructions change.",
  reports: "Who it answers to.",
  identity: "Per-agent tokens and policies come in a later release.",
} as const;

export type CertRow = {
  key: keyof typeof CERT_TIPS; label: string; tip: string; value: string;
  testId: string; mono?: boolean; copy?: string; full?: string;
};

/** The certificate, row by row. Every value is the API's; a missing one is a dash. */
export function certRows(passport: Passport | null, actor: string, reportsTo: string | null): CertRow[] {
  const id = passport?.agentId || "";
  const sponsor = passport?.sponsor;
  const fp = (passport?.fingerprint || "").replace(/^sha256:/, "");
  const rows: CertRow[] = [
    { key: "id", label: "ID", tip: CERT_TIPS.id, value: id || "—", testId: "ag-cert-id", mono: true, copy: id || undefined },
    { key: "born", label: "Born", tip: CERT_TIPS.born, value: passport ? bornDate(passport.born.at) : "—", testId: "ag-cert-born" },
    { key: "by", label: "Born by", tip: CERT_TIPS.by, value: passport ? bornByLabel(passport.born.by, actor) : "—", testId: "ag-cert-by" },
    { key: "from", label: "From", tip: CERT_TIPS.from, value: passport ? bornFromLabel(passport.born.from) : "—", testId: "ag-cert-from" },
    { key: "sponsor", label: "Sponsor", tip: CERT_TIPS.sponsor, testId: "ag-cert-sponsor",
      value: sponsor ? (sponsor === actor ? "You" : sponsor) : "—" },
    { key: "version", label: "Version", tip: CERT_TIPS.version, testId: "ag-cert-version", mono: true,
      value: passport ? versionLine(passport.version, passport.fingerprint) : "—",
      copy: passport ? (fp ? `v${passport.version} ${fp}` : `v${passport.version}`) : undefined,
      full: passport && fp ? `v${passport.version} · ${fp}` : undefined },
    { key: "reports", label: "Reports to", tip: CERT_TIPS.reports, testId: "ag-cert-reports",
      value: reportsTo ? identityOf(reportsTo).name : "—" },
    { key: "identity", label: "Identity", tip: CERT_TIPS.identity, value: "Local · tokens coming", testId: "ag-cert-identity" },
  ];
  return rows;
}

async function copyText(text: string, label: string): Promise<void> {
  try { await navigator.clipboard.writeText(text); toast("success", `Copied ${label}`); }
  catch { toast("error", "Clipboard unavailable"); }
}

const LIFECYCLE_WORD = { active: "Active", paused: "Paused", retired: "Retired" } as const;

export function PassportHeader({ projectId, actor, roleKey, name, passport, onAsk, onShare, onDelete,
                                 onChanged, presenceState, facts }: {
  projectId: string; actor: string; roleKey: string;
  /** The roster's name, for before the passport has loaded or on an API without one. */
  name: string;
  passport: Passport | null;
  onAsk: () => void;
  onShare: () => void;
  onDelete: () => void;
  onChanged: (next: Passport) => void;
  presenceState?: AvatarState;
  facts?: DetailFacts;
}) {
  const who = useIdentity(roleKey, name); // and repaints when identities load
  const persona = passport?.persona;
  const callsign = persona?.callsign || who.callsign;
  const display = passport?.displayName || name;
  const title = callsign || display;
  const tagline = persona?.tagline || who.tagline;
  const about = persona?.about_me;
  const lifecycle = passport?.lifecycle ?? who.lifecycle;
  // The face through the same resolver as every chip: the cast's (or seeded) face, with the
  // passport's picked parts over it only when it has some.
  const identity = passport
    ? { ...who, name: display, callsign, lifecycle,
        avatar: resolveAvatar(who.roleKey || roleKey, passport.agentId || who.agentId, persona?.avatar) }
    : { ...who, lifecycle };
  const state: AvatarState = presenceState ?? passport?.presence.state ?? "idle";

  const [more, setMore] = useState(false);
  const [menu, setMenu] = useState(false);
  const [busy, setBusy] = useState("");
  const [wave, setWave] = useState("");
  const clicks = useRef<number[]>([]);
  const menuBox = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu) return undefined;
    const onDown = (e: MouseEvent) => { if (menuBox.current && !menuBox.current.contains(e.target as Node)) setMenu(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setMenu(false); } };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey, true);
    return () => { window.removeEventListener("mousedown", onDown); window.removeEventListener("keydown", onKey, true); };
  }, [menu]);

  // THE EASTER EGG: five clicks inside two seconds. Its first phrase, or its tagline.
  const poke = () => {
    const now = Date.now();
    clicks.current = [...clicks.current.filter((t) => now - t < 2000), now];
    if (clicks.current.length < 5) return;
    clicks.current = [];
    setWave(persona?.phrases?.[0] || tagline || "Hello.");
    setTimeout(() => setWave(""), 2400);
  };

  const sponsor = passport?.sponsor;
  const reportsTo = passport?.reportsTo || facts?.reportsTo || null;
  const mayRetire = !sponsor || sponsor === actor;
  const steer = async (next: Passport["lifecycle"]) => {
    setMenu(false);
    setBusy(next);
    const r = await setLifecycle(projectId, actor, roleKey, next);
    setBusy("");
    if (!r.ok) {
      toast("error", r.status === 403 ? "Only the sponsor can retire this agent" : r.error);
      return;
    }
    onChanged(r.data);
    toast("success", next === "paused" ? `${title} paused · skipped by Auto`
      : next === "retired" ? `${title} retired` : `${title} resumed`);
  };

  const copyCard = async () => {
    setMenu(false);
    const r = await getAgentCard(projectId, roleKey);
    if (!r.ok) { toast("error", r.error); return; }
    try {
      await navigator.clipboard.writeText(JSON.stringify(r.data, null, 2));
      toast("success", "Copied card JSON");
    } catch { toast("error", "Clipboard unavailable"); }
  };
  return (
    <header className="ag-passport" data-testid="ag-passport" data-lifecycle={lifecycle}>
      <div className="ag-passport-hero">
        <button type="button" className="ag-hero-face" data-wave={wave ? true : undefined}
                onClick={poke} aria-label={`${title}, ${state === "idle" ? "Idle" : state}`}>
          <AgFace roleKey={roleKey} identity={identity} size={PASSPORT_FACE_PX} state={state} decorative aiBadge={false}
                  motion={wave ? "wave" : undefined} />
        </button>
        {wave ? <span className="ag-hero-bubble" role="status" data-user-text>{wave}</span> : null}
      </div>

      <div className="ag-passport-who">
        <h1 className="ag-passport-name" data-testid="ag-passport-name" data-user-text>{title}</h1>
        <div className="ag-passport-role">
          {callsign ? <span data-user-text>{display}</span> : <span>{humanise(roleKey)}</span>}
          <AiBadge long />
          <span className="ag-life" data-life={lifecycle} data-testid="ag-lifecycle">{LIFECYCLE_WORD[lifecycle]}</span>
        </div>
        {tagline ? <div className="ag-passport-tagline" data-user-text>{tagline}</div> : null}
        {about ? (
          <div className="ag-passport-about" data-open={more || undefined}>
            <span data-user-text data-testid="ag-about">{about}</span>
            {about.length > 120 ? (
              <button type="button" className="ag-link" onClick={() => setMore((v) => !v)}>
                {more ? "less" : "more"}
              </button>
            ) : null}
          </div>
        ) : null}
        <div className="ag-passport-actions">
          <button type="button" className="ag-btn ag-btn-primary" data-testid="ag-ask"
                  title={`Talk to ${title}`} onClick={onAsk}>Ask</button>
          <button type="button" className="ag-btn" data-testid="ag-share" onClick={onShare}
                  disabled={!passport}>
            <ShareNetwork size={13} aria-hidden /> Share card
          </button>
          <div className="ag-menu-wrap" ref={menuBox}>
            <button type="button" className="ag-btn ag-btn-icon" aria-label="More" aria-expanded={menu}
                    data-testid="ag-more" onClick={() => setMenu((v) => !v)}>
              <DotsThree size={16} weight="bold" aria-hidden />
            </button>
            {menu ? (
              <div className="ag-menu" role="menu" data-testid="ag-menu">
                <button type="button" role="menuitem" onClick={() => void copyCard()}
                        disabled={!passport}>Copy card JSON</button>
                {lifecycle === "active" ? (
                  <button type="button" role="menuitem" data-testid="ag-pause" disabled={!passport || !mayRetire || !!busy}
                          title={mayRetire ? "Auto skips it until resumed" : "Only the sponsor can retire this agent"}
                          onClick={() => void steer("paused")}>Pause</button>
                ) : (
                  <button type="button" role="menuitem" data-testid="ag-resume" disabled={!passport || !mayRetire || !!busy}
                          title={mayRetire ? undefined : "Only the sponsor can retire this agent"}
                          onClick={() => void steer("active")}>Resume</button>
                )}
                {lifecycle !== "retired" ? (
                  <button type="button" role="menuitem" data-testid="ag-retire" disabled={!passport || !mayRetire || !!busy}
                          title={mayRetire ? "Keeps its work, hides it from pickers" : "Only the sponsor can retire this agent"}
                          onClick={() => void steer("retired")}>Retire</button>
                ) : null}
                <button type="button" role="menuitem" data-tone="danger"
                        onClick={() => { setMenu(false); onDelete(); }}>Delete</button>
              </div>
            ) : null}
          </div>
        </div>
      </div>

      <dl className="ag-cert" data-testid="ag-cert">
        {certRows(passport, actor, reportsTo).map((row) => (
          <div key={row.label} className="ag-cert-row" data-testid={`ag-cert-row-${row.key}`}>
            <dt title={row.tip}>{row.label}</dt>
            <dd title={row.full ?? row.value}>
              {row.mono
                ? <code className="ag-trunc" data-testid={row.testId}>{row.value}</code>
                : <span className="ag-trunc" data-testid={row.testId}>{row.value}</span>}
              {row.copy ? (
                <button type="button" className="ag-icon-btn ag-cert-copy" aria-label={`Copy ${row.label}`}
                        title={`Copy ${row.label}`} data-testid={`ag-copy-${row.key}`}
                        onClick={() => void copyText(row.copy!, row.label)}>
                  <Copy size={13} aria-hidden />
                </button>
              ) : null}
            </dd>
          </div>
        ))}
      </dl>

      {passport || facts ? (
        <div className="ag-vitals" data-testid="ag-vitals">
          {(passport ? vitalsLine(passport.vitals) : detailVitalsLine(facts!)).map((v) => <span key={v}>{v}</span>)}
          {/* Surfaced, not buried (console_v2_workforce): a policy violation is never folded away. */}
          {facts && facts.violations > 0 ? (
            <span data-tone="bad" data-testid="ag-violations" title="Policy violations on record">
              {facts.violations} violation{facts.violations === 1 ? "" : "s"}
            </span>
          ) : null}
        </div>
      ) : null}
    </header>
  );
}

/** The section nav: anchors, synced to `&tab=`, so a link can open the Passport on Lineage. */
export function PassportNav({ active, onPick }: { active: SectionKey; onPick: (k: SectionKey) => void }) {
  return (
    <nav className="ag-pnav" aria-label="Sections" data-testid="ag-pnav">
      {SECTIONS.map((s) => (
        <a key={s.key} href={`#ag-sec-${s.key}`} aria-current={active === s.key ? "true" : undefined}
           data-testid={`ag-tab-${s.key}`}
           onClick={(e) => { e.preventDefault(); onPick(s.key); }}>{s.label}</a>
      ))}
    </nav>
  );
}

export function readTab(search: string): SectionKey {
  const raw = new URLSearchParams(search).get("tab");
  // The Kit's `All abilities ↗` and `Who can` links still say `&tab=skills`: that is the Loadout.
  const t = raw === "skills" ? "loadout" : raw;
  return (SECTIONS.find((s) => s.key === t)?.key ?? "loadout") as SectionKey;
}

/** Scroll to a section and say so in the URL (replace: a tab is not a navigation). */
export function goToSection(k: SectionKey): void {
  try {
    const url = new URL(window.location.href);
    if (k === "loadout") url.searchParams.delete("tab"); else url.searchParams.set("tab", k);
    window.history.replaceState(null, "", url.toString());
    const el = document.getElementById(`ag-sec-${k}`);
    if (el && typeof el.scrollIntoView === "function") {
      const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    }
  } catch { /* non-browser */ }
}
