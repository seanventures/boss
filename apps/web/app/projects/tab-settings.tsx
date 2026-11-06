"use client";

/**
 * SETTINGS (screen 13, contract §11): the project's own settings in one place, moved from where they
 * were scattered — demo logins (user names + reveal, step-up), deploy values (names only), targets &
 * hosts, captain & squad, autonomy & budget, repos, notifications. Each write is the existing one
 * (the section's `actions`); the old homes say "Moved to Project › Settings" and link here.
 *
 * Reveal: owner/admin, through the step-up sheet ("Confirm it's you"); the password shows for 30
 * seconds and Copy copies it without ever printing it.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  ArrowSquareOut, Bell, Copy, Eye, EyeSlash, Gauge, GearSix, GitBranch, HardDrives, Key, LockKey, Plus, SteeringWheel, UserCircle, Crown, Headset, User,
  type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError, idGet, refusalSentence } from "../../lib/identity/api";
import {
  PX_PATHS, a, isPxDemo, usePageSearch, o, parseSettings, pxHref, s, setCaptain, usePxRead, writePxUrl, type Settings, type SettingsSection,
} from "../../lib/project-experience";
import { FX_SETTINGS } from "../../lib/project-experience-fixtures";
import { Face, Ico, PxLink, ReadGate, Said, Tag, type Tone } from "./kit";
import type { PxCtx } from "./project-page";

const SECTION_ICON: Record<string, { icon: Icon; c: Tone }> = {
  demo_logins: { icon: Key, c: "green" }, deploy_values: { icon: LockKey, c: "violet" }, targets: { icon: HardDrives, c: "teal" },
  captain: { icon: SteeringWheel, c: "blue" }, autonomy: { icon: Gauge, c: "amber" }, repos: { icon: GitBranch, c: "teal" }, notifications: { icon: Bell, c: "red" },
};
const ROLE_ICON: Record<string, Icon> = { admin: Crown, agent: Headset, customer: User };

/** Sections from what the console already knows, for an engine without §11's read. */
function fallbackSettings(ctx: PxCtx): Settings {
  const p = ctx.overview.project;
  const c = ctx.overview.captain;
  return { sections: [
    { key: "demo_logins", label: "Demo logins", icon: "key", moved_from: "", items: [],
      actions: [{ label: "Open the app's setup", href: "?area=code_tasks&tab=apps", requires: "" }] },
    { key: "deploy_values", label: "Deploy values", icon: "lock", moved_from: "", items: [],
      actions: [{ label: "Deploy values in Infra", href: "?area=infra&view=deploys", requires: "" }] },
    { key: "targets", label: "Targets & hosts", icon: "server", moved_from: "", items: [],
      actions: [{ label: "Targets in Infra", href: "?area=infra&view=fleet", requires: "" }] },
    { key: "captain", label: "Captain & crew", icon: "robot", moved_from: "",
      items: c ? [{ label: "Captain", value: c.name, sub: "", reveal: null, href: "" }] : [], actions: [{ label: "Agent Studio", href: "?area=workforce", requires: "" }] },
    { key: "autonomy", label: "Autonomy & budget", icon: "gauge", moved_from: "", items: [],
      actions: [{ label: "Autonomy", href: "?area=agent_ops", requires: "" }, { label: "Budget in Govern", href: "?area=budget", requires: "" }] },
    { key: "repos", label: "Repositories", icon: "git", moved_from: "",
      items: p.repos.map((r) => ({ label: r.full_name, value: r.branch, sub: "", reveal: null, href: r.external_url || `https://github.com/${r.full_name}` })),
      actions: [{ label: "Add repo", href: "?area=setup&focus=repository&open=add", requires: "" }] },
    { key: "notifications", label: "Notifications", icon: "bell", moved_from: "", items: [],
      actions: [{ label: "Channels in Skills & MCP", href: "?area=skills", requires: "" }] },
  ] };
}

export function SettingsTab({ ctx }: { ctx: PxCtx }) {
  const read = usePxRead<Settings>(PX_PATHS.settings(ctx.projectId), parseSettings, { everyMs: 60_000, demo: () => parseSettings(FX_SETTINGS) });
  const data = read.data ?? (read.state === "missing" ? fallbackSettings(ctx) : null);
  const asked = usePageSearch().get("section");
  const [active, setActive] = useState<string>(asked ?? "demo_logins");
  const refs = useRef<Record<string, HTMLElement | null>>({});
  useEffect(() => {
    if (asked && refs.current[asked]) { refs.current[asked]!.scrollIntoView?.({ block: "start", behavior: "smooth" }); setActive(asked); }
  }, [asked, data]);
  const go = (key: string) => {
    setActive(key);
    writePxUrl({ section: key }, "replace");
    refs.current[key]?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  };
  return (
    <div data-testid="px-settings">
      {read.state === "missing" && !read.data ? <div className="px-note" role="note" style={{ marginBottom: 12 }}>These settings gather here with the next BOSS update; meanwhile each links to where it lives today.</div> : null}
      <ReadGate read={{ ...read, data }} what="Settings">
        {(st) => (
          <div className="px-set">
            <nav className="px-set-nav" aria-label="Settings sections">
              {st.sections.map((sec) => {
                const I = SECTION_ICON[sec.key]?.icon ?? GearSix;
                return <button key={sec.key} type="button" aria-current={active === sec.key} onClick={() => go(sec.key)} data-testid={`px-set-nav-${sec.key}`}><I size={14} aria-hidden />{sec.label}</button>;
              })}
            </nav>
            <div className="px-stack">
              {st.sections.map((sec) => (
                <section key={sec.key} ref={(el) => { refs.current[sec.key] = el; }} className="px-card" aria-labelledby={`px-set-${sec.key}`}
                         data-testid={`px-set-${sec.key}`} style={{ scrollMarginTop: 80 }}>
                  <div className="px-card-h">
                    <Ico icon={SECTION_ICON[sec.key]?.icon ?? GearSix} c={SECTION_ICON[sec.key]?.c ?? "blue"} />
                    <h2 id={`px-set-${sec.key}`}>{sec.label}</h2>
                    {sec.key === "deploy_values" ? <small className="px-faint">names only · values write-only</small> : null}
                    {sec.moved_from ? <small className="px-faint px-hide-phone">moved from {sec.moved_from}</small> : null}
                    <span className="px-spacer" />
                    {sec.actions.map((ac) => <PxLink key={ac.label} href={ac.href} className="px-more" testid={`px-set-act-${sec.key}`}>{/^add/i.test(ac.label) ? <Plus size={11} /> : null}{ac.label}</PxLink>)}
                  </div>
                  <SectionBody ctx={ctx} sec={sec} />
                </section>
              ))}
            </div>
          </div>
        )}
      </ReadGate>
    </div>
  );
}

function SectionBody({ ctx, sec }: { ctx: PxCtx; sec: SettingsSection }) {
  if (sec.key === "demo_logins") {
    return sec.items.length ? (
      <div data-testid="px-demo-logins">
        {sec.items.map((it) => <DemoLoginRow key={`${it.label}-${it.value}`} projectId={ctx.projectId} login={it} />)}
        <small className="px-faint" style={{ display: "block", marginTop: 8 }}>Users from the app (boss.deploy.yaml or its seed) · one password per environment (DEMO_PASSWORD)</small>
      </div>
    ) : <p className="px-faint" data-testid="px-demo-logins-none">No demo users found for this project yet. They appear once the app declares them (boss.deploy.yaml or its seed) and deploys.</p>;
  }
  if (sec.key === "captain") return <CaptainSection ctx={ctx} sec={sec} />;
  if (!sec.items.length) return <p className="px-faint" style={{ margin: 0 }}>Nothing set here yet.</p>;
  return (
    <div>
      {sec.items.map((it) => (
        <div key={`${it.label}-${it.value}`} className="px-li">
          <span className="px-ck-b" aria-hidden style={it.value === "not set" || it.value === "not set up" ? { borderColor: "var(--amber)", color: "var(--amber)" } : { borderColor: "var(--green)", background: "var(--green)", color: "#032015" }}>
            {it.value === "not set" || it.value === "not set up" ? "!" : "✓"}</span>
          <span style={{ minWidth: 0 }}><b className={sec.key === "deploy_values" ? "px-mono" : undefined}>{it.label}</b><small>{[it.sub].filter(Boolean).join(" · ")}</small></span>
          <span className="px-row">
            {it.value ? <Tag tone={it.value === "not set" || it.value === "not set up" ? "amber" : undefined}>{it.value}</Tag> : null}
            {it.href ? <PxLink href={it.href} testid={`px-set-item-${sec.key}`}>{sec.key === "deploy_values" ? (it.value === "not set" ? "Set" : "Rotate") : "Open"}</PxLink> : null}
          </span>
        </div>
      ))}
    </div>
  );
}

function CaptainSection({ ctx, sec }: { ctx: PxCtx; sec: SettingsSection }) {
  const c = ctx.overview.captain;
  const [picking, setPicking] = useState(false);
  const [said, setSaid] = useState("");
  const choose = async (agentId: string, name: string) => {
    const r = await setCaptain(ctx.projectId, agentId);
    setSaid(r.ok ? (r.demo ? "Sample mode — nothing was sent." : `${name} is the captain now.`) : (r.error ?? "Not changed."));
    setPicking(false);
  };
  return (
    <div data-testid="px-set-captain-body">
      {c ? (
        <div className="px-row px-wrap">
          <Face agent={c} size={34} />
          <div style={{ flex: 1, minWidth: 0 }}><b>{c.name}</b><div className="px-faint" style={{ fontSize: ".58rem" }}>{c.role_key === "engineering_captain" ? "Engineering Captain" : "Captain"}</div></div>
          <GatedButton permission="project:update" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid="px-set-captain-change"
                       onClick={() => setPicking((v) => !v)}>Change</GatedButton>
        </div>
      ) : <p className="px-faint">No captain yet.</p>}
      {c?.squad.length ? (
        <div className="px-row px-wrap" style={{ marginTop: 10 }}>
          <span className="px-faces">{c.squad.slice(0, 5).map((m) => <Face key={m.name} agent={m} size={22} />)}</span>
          <small className="px-faint">{c.squad.length} in squad</small><span className="px-spacer" />
          <a href={pxHref("?area=workforce")} data-testid="px-set-squad">Edit squad</a>
        </div>
      ) : null}
      {picking && c ? (
        <div role="listbox" aria-label="Choose a captain" className="px-col" style={{ marginTop: 10 }} data-testid="px-set-captain-pick">
          {[{ name: c.name, role_key: c.role_key, agent_id: c.agent_id }, ...c.squad.map((m) => ({ name: m.name, role_key: m.role_key, agent_id: m.role_key }))].map((m) => (
            <button key={m.name} type="button" role="option" aria-selected={m.name === c.name} className="px-li"
                    style={{ width: "100%", background: "none", border: 0, borderTop: "1px solid var(--border)", color: "inherit", font: "inherit", textAlign: "left", cursor: "pointer" }}
                    onClick={() => void choose(m.agent_id || m.role_key, m.name)}>
              <Face agent={m} size={22} /><b>{m.name}</b>{m.name === c.name ? <Tag tone="blue">current</Tag> : <span />}
            </button>
          ))}
        </div>
      ) : null}
      {sec.items.filter((it) => it.label !== "Captain").map((it) => (
        <div key={it.label} className="px-li"><span /><span><b>{it.label}</b><small>{it.value}{it.sub ? ` · ${it.sub}` : ""}</small></span>
          {it.href ? <PxLink href={it.href}>Open</PxLink> : <span />}</div>
      ))}
      <Said text={said} />
    </div>
  );
}

/** What a reveal answers, whatever the engine's spelling: one password, or a list per user. */
export function revealedPassword(body: unknown, user: string): string {
  const x = o(body);
  if (s(x.password)) return s(x.password);
  if (s(x.value)) return s(x.value);
  const list = a(x.demo_logins ?? x.items);
  const mine = list.find((d) => s(o(d).user) === user || s(o(d).name) === user) ?? list[0];
  return s(o(mine).password) || s(o(mine).value);
}

/** One demo login: role, user name, masked password, Reveal (step-up), Copy. */
export function DemoLoginRow({ projectId, login, compact }: {
  projectId: string; login: { label: string; value: string; sub: string; reveal: { href: string; requires: string } | null; href: string }; compact?: boolean;
}) {
  const guarded = useGuardedCall();
  const [shown, setShown] = useState<string | null>(null);
  const [left, setLeft] = useState(0);
  const [why, setWhy] = useState("");
  useEffect(() => {
    if (!shown) return undefined;
    setLeft(30);
    const t = window.setInterval(() => setLeft((n) => { if (n <= 1) { setShown(null); return 0; } return n - 1; }), 1000);
    return () => window.clearInterval(t);
  }, [shown]);
  const fetchIt = async (): Promise<string | null> => {
    if (!login.reveal) return null;
    setWhy("");
    if (isPxDemo()) return "demo-sample-pass";
    try {
      const body = await guarded("Show the demo login password.", () => idGet<unknown>(login.reveal!.href));
      const pw = revealedPassword(body, login.value);
      if (!pw) setWhy("BOSS has no password for this login yet.");
      return pw || null;
    } catch (e) {
      setWhy(e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not shown: you cancelled the check." : refusalSentence(e));
      return null;
    }
  };
  const Role = ROLE_ICON[login.label.toLowerCase()] ?? UserCircle;
  return (
    <div className="px-cred" data-testid={`px-login-${login.value}`}>
      <span className="px-tag" data-tone={login.label.toLowerCase() === "admin" ? "blue" : undefined}><Role size={11} aria-hidden /> {login.label}</span>
      <span className="px-mono px-ell">{login.value}</span>
      <span className="px-row">
        {shown ? <><span className="px-mono" data-testid="px-login-password">{shown}</span><Tag tone="amber">{left}s</Tag></> : <span className="px-secret" aria-label="password hidden">••••••••••</span>}
        {login.sub && !compact ? <Tag>{login.sub}</Tag> : null}
      </span>
      <span className="px-row">
        {login.reveal ? (
          <>
            <GatedButton permission="app_secret:write" projectId={projectId} className="px-btn" data-size="s" data-testid={`px-login-reveal-${login.value}`}
                         title="Show the password (asks you to confirm it's you; hides after 30s)"
                         onClick={() => { if (shown) { setShown(null); return; } void fetchIt().then((pw) => { if (pw) setShown(pw); }); }}>
              {shown ? <EyeSlash size={12} aria-hidden /> : <Eye size={12} aria-hidden />}{compact ? null : (shown ? " Hide" : " Reveal")}
              {compact ? <span className="px-sr">{shown ? "Hide" : "Reveal"} password</span> : null}</GatedButton>
            <GatedButton permission="app_secret:write" projectId={projectId} className="px-icon-btn" data-testid={`px-login-copy-${login.value}`}
                         aria-label={`Copy the password for ${login.value}`} title="Copy the password (never shown)"
                         onClick={() => void (async () => { const pw = shown ?? (await fetchIt()); if (pw) { await navigator.clipboard?.writeText(pw); setWhy("Copied."); } })()}>
              <Copy size={13} /></GatedButton>
          </>
        ) : login.href ? <PxLink href={login.href}>Open <ArrowSquareOut size={10} /></PxLink> : null}
      </span>
      {why ? <span style={{ gridColumn: "1 / -1" }}><Said text={why} tone={why === "Copied." ? "green" : "red"} /></span> : null}
    </div>
  );
}
