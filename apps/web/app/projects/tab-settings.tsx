"use client";

/**
 * SETTINGS (screen 13, contract §11): the project's own settings in one place, moved from where they
 * were scattered — demo logins per environment (user names, Reveal with step-up, Rotate), deploy
 * values (one row per name, in words; write-only Set / Rotate / Remove), targets & hosts, captain &
 * squad, autonomy level and the week's ambient budget, repos, and who is told about what. Each
 * write is an existing route (or the §11 one built for it); the old homes show "Moved to Project ›
 * Settings · Open →" and keep working.
 *
 * Reveal: owner/admin, through the step-up sheet ("Confirm it's you"); the password shows for 30
 * seconds and Copy copies it without ever printing it.
 */
import React, { useEffect, useRef, useState } from "react";
import {
  ArrowSquareOut, ArrowsClockwise, Bell, Copy, Crown, Eye, EyeSlash, GearSix, GitBranch, GitMerge, GithubLogo, HandWaving, HardDrives, Headset, Info,
  Key, LockKey, PencilSimple, Plus, Siren, SlidersHorizontal, SteeringWheel, User, UserCircle,
  type Icon,
} from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError, idDelete, idGet, idPost, idPut, refusalFix, refusalSentence, type RefusalFix } from "../../lib/identity/api";
import {
  PX_PATHS, a, isPxDemo, n, usePageSearch, o, parseSettings, pxHref, s, setCaptain, usePxRead, writePxUrl,
  type Agent, type Settings, type SettingsAction, type SettingsSection,
} from "../../lib/project-experience";
import { notifyChanged } from "../changed";
import { apiWrite } from "../../lib/api-write";
import { LEVEL_MEANINGS, levelLabel } from "../../lib/autonomy-levels";
import { FX_SETTINGS } from "../../lib/project-experience-fixtures";
import { Face, Ico, PxLink, ReadGate, Said, Tag, type Tone } from "./kit";
import type { PxCtx } from "./project-page";
import { CaptainPicker, captainChoices } from "./captain-picker";
import { ConfirmSheet } from "./confirm-sheet";

/** The section nav and card icons, in the wireframe's order and colours (13-01). */
const SECTION_ICON: Record<string, { icon: Icon; c: Tone | "grey" }> = {
  demo_logins: { icon: Key, c: "green" }, deploy_values: { icon: LockKey, c: "violet" }, targets: { icon: HardDrives, c: "teal" },
  captain: { icon: SteeringWheel, c: "blue" }, autonomy: { icon: SlidersHorizontal, c: "amber" }, repos: { icon: GithubLogo, c: "grey" },
  notifications: { icon: Bell, c: "red" },
};
const ROLE_ICON: Record<string, Icon> = { admin: Crown, agent: Headset, customer: User };
const EVENT_ICON: Record<string, Icon> = { needs_you: HandWaving, incidents: Siren, pr_ready: GitMerge };

/** A link to where a setting lives today (a console href is its own `open`). */
const go = (label: string, href: string): SettingsAction => ({ label, href, method: "GET", requires: "", open: href });
const section = (key: string, label: string, icon: string, items: SettingsSection["items"], actions: SettingsAction[]): SettingsSection =>
  ({ key, label, icon, moved_from: "", hint: "", items, actions, candidates: [], raw: {} });

/** Sections from what the console already knows, when §11's read does not answer (the project is
 *  out of reach, or an older engine): each links to where the setting lives. */
function fallbackSettings(ctx: PxCtx): Settings {
  const p = ctx.overview.project;
  const c = ctx.overview.captain;
  return { sections: [
    section("demo_logins", "Demo logins", "key", [], [go("Open the app's setup", "?area=code_tasks&view=apps")]),
    section("deploy_values", "Deploy values", "lock", [], [go("Open the app's setup", "?area=code_tasks&view=apps")]),
    section("targets", "Targets & hosts", "server", [], [go("Targets in Infra", "?area=infra&view=fleet")]),
    section("captain", "Captain & squad", "robot", c ? [{ label: "Captain", value: c.name, sub: "", reveal: null, href: "" }] : [], [go("Agent Studio", "?area=workforce")]),
    section("autonomy", "Autonomy & budget", "gauge", [], [go("Autonomy", "?area=budget&tab=autonomy"), go("Budget in Govern", "?area=budget&tab=limits")]),
    section("repos", "Repos", "git",
            p.repos.map((r) => ({ label: r.full_name, value: r.branch, sub: "", reveal: null, href: r.external_url || `https://github.com/${r.full_name}` })),
            [go("Add", "?area=setup&focus=repository&open=add")]),
    section("notifications", "Notifications", "bell", [], [go("Notifications", "?area=observability&view=notify")]),
  ] };
}

/** A section's console links (its actions' `open`), once — an API route is never a link. */
function openLinks(sec: SettingsSection, skip: string[] = []): SettingsAction[] {
  const seen = new Set<string>();
  return sec.actions.filter((ac) => {
    if (!ac.open || skip.includes(ac.label)) return false;
    const k = `${ac.label}|${ac.open}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

/** One write the section's own form sends, step-up aware; sample mode sends nothing. */
function useWrite() {
  const guarded = useGuardedCall();
  return async (label: string, call: () => Promise<unknown>): Promise<{ ok: boolean; body: unknown; said: string; fix: RefusalFix | null }> => {
    if (isPxDemo()) return { ok: true, body: {}, said: "Sample mode — nothing was sent.", fix: null };
    try {
      const body = await guarded(label, call);
      return { ok: true, body, said: "", fix: null };
    } catch (e) {
      return { ok: false, body: null, fix: refusalFix(e),
               said: e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not changed: you cancelled the check." : refusalSentence(e) };
    }
  };
}

/** What a write said: green when it worked, red (an alert) when it was refused — from the result,
 *  never guessed from the words — with the refusal's fix action beside it (13-03 / X-73). */
export type Line = { text: string; ok: boolean; fix?: RefusalFix | null };
export const NO_LINE: Line = { text: "", ok: true };
export const lineOf = (r: { ok: boolean; said: string; fix?: RefusalFix | null }, okText: string): Line =>
  (r.ok ? { text: r.said || okText, ok: true } : { text: r.said, ok: false, fix: r.fix ?? null });

export function SaidLine({ line }: { line: Line }) {
  if (!line.text) return null;
  return (
    <span className="px-row px-wrap" style={{ gap: 6 }} data-testid="px-said-line" data-ok={line.ok ? "" : undefined}>
      <Said text={line.text} tone={line.ok ? "green" : "red"} />
      {!line.ok && line.fix?.href ? <PxLink href={line.fix.href} className="px-more" testid="px-said-fix">{line.fix.label} ›</PxLink> : null}
      {!line.ok && line.fix?.command && !line.fix.href ? <code className="px-code" title={line.fix.label} data-testid="px-said-fix">{line.fix.command}</code> : null}
    </span>
  );
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
  const goTo = (key: string) => {
    setActive(key);
    writePxUrl({ section: key }, "replace");
    refs.current[key]?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  };
  const changed = () => { read.refresh(); notifyChanged("project.settings"); };
  return (
    <div data-testid="px-settings">
      {read.state === "missing" && !read.data ? <div className="px-note" role="note" style={{ marginBottom: 12 }} data-testid="px-settings-note">BOSS didn&apos;t answer for this project&apos;s settings; each one links to where it lives.</div> : null}
      <ReadGate read={{ ...read, data }} what="Settings">
        {(st) => {
          const by = Object.fromEntries(st.sections.map((x) => [x.key, x]));
          const card = (key: string) => (by[key] ? (
            <SectionCard key={key} sec={by[key]} ctx={ctx} onChanged={changed} setRef={(el) => { refs.current[key] = el; }} />
          ) : null);
          return (
            <div className="px-set">
              <nav className="px-set-nav" aria-label="Settings sections">
                {st.sections.map((sec) => {
                  const I = SECTION_ICON[sec.key]?.icon ?? GearSix;
                  return <button key={sec.key} type="button" aria-current={active === sec.key} onClick={() => goTo(sec.key)} data-testid={`px-set-nav-${sec.key}`}><I size={14} aria-hidden />{sec.label}</button>;
                })}
              </nav>
              <div className="px-stack">
                {card("demo_logins")}
                {card("deploy_values")}
                {by.targets || by.captain ? <div className="px-grid px-g2">{card("targets")}{card("captain")}</div> : null}
                {by.autonomy || by.repos ? <div className="px-grid px-g2">{card("autonomy")}{card("repos")}</div> : null}
                {card("notifications")}
                {st.sections.filter((x) => !["demo_logins", "deploy_values", "targets", "captain", "autonomy", "repos", "notifications"].includes(x.key)).map((x) => card(x.key))}
              </div>
            </div>
          );
        }}
      </ReadGate>
    </div>
  );
}

function SectionCard({ sec, ctx, onChanged, setRef }: { sec: SettingsSection; ctx: PxCtx; onChanged: () => void; setRef: (el: HTMLElement | null) => void }) {
  const meta = SECTION_ICON[sec.key];
  const [adding, setAdding] = useState(false);
  const [env, setEnv] = useState<string | null>(null);
  const raw = o(sec.raw);
  const head: React.ReactNode[] = [];
  if (sec.key === "deploy_values") {
    head.push(<small key="n" className="px-faint px-hide-phone" style={{ fontSize: ".56rem" }}>names only · values write-only</small>);
    head.push(<span key="sp" className="px-spacer" />);
    if (o(raw.add).href) head.push(<GatedButton key="add" permission="app_secret:write" projectId={ctx.projectId} className="px-more px-linkbtn" data-testid="px-set-add-value" onClick={() => setAdding((v) => !v)}><Plus size={11} aria-hidden /> Add value</GatedButton>);
  }
  if (sec.key === "repos") openLinks(sec).filter((x) => x.label === "Add").forEach((x) => head.push(<PxLink key="add" href={x.open} className="px-more" testid="px-set-repos-add"><Plus size={11} aria-hidden /> Add</PxLink>));
  if (sec.key === "notifications" && s(raw.href)) head.push(<PxLink key="cp" href={s(raw.href)} className="px-more" testid="px-set-notify-cps">Contact points</PxLink>);
  // A section the engine does not shape here (an older engine): its console links.
  const known = ["demo_logins", "deploy_values", "targets", "captain", "autonomy", "repos", "notifications"].includes(sec.key);
  if (!known || (!Object.keys(raw).length && sec.key !== "captain")) openLinks(sec).forEach((x) => head.push(<PxLink key={`${x.label}-${x.open}`} href={x.open} className="px-more" testid={`px-set-act-${sec.key}`}>{x.label}</PxLink>));
  return (
    <section ref={setRef} className="px-card" aria-labelledby={`px-set-${sec.key}`} data-testid={`px-set-${sec.key}`} style={{ scrollMarginTop: 80 }}>
      <div className="px-card-h">
        {meta?.c === "grey" ? <span className="px-ico" aria-hidden><GithubLogo size={16} weight="duotone" /></span>
          : <Ico icon={meta?.icon ?? GearSix} c={(meta?.c as Tone) ?? "blue"} />}
        <h2 id={`px-set-${sec.key}`}>{sec.label}</h2>
        {sec.key === "demo_logins" ? <DemoEnvSwitch sec={sec} picked={env} onPick={setEnv} /> : null}
        {head.length && sec.key !== "deploy_values" ? <span className="px-spacer" /> : null}
        {head}
      </div>
      <SectionBody ctx={ctx} sec={sec} onChanged={onChanged} adding={adding} setAdding={setAdding} env={env} />
    </section>
  );
}

// ── demo logins (13-02 … 13-07) ─────────────────────────────────────────────────────────────

type DemoEnv = { key: string; label: string; logins: { user: string | null; label: string; reveal: { href: string; requires: string } | null }[]; empty: string; rotate: { href: string } | null };
function demoEnvs(sec: SettingsSection): DemoEnv[] {
  const envs = a(o(sec.raw).environments).map((e) => {
    const y = o(e); const rot = o(y.rotate);
    return { key: s(y.key), label: s(y.label), empty: s(y.empty), rotate: s(rot.href) ? { href: s(rot.href) } : null,
             logins: a(y.logins).map((l) => {
               const z = o(l); const r = o(z.reveal);
               return { user: s(z.user) || null, label: s(z.label, "Demo login"), reveal: s(r.href) ? { href: s(r.href), requires: s(r.requires, "step_up") } : null };
             }) };
  });
  if (envs.length) return envs;
  // An older engine (or sample data): the flat rows, grouped by their environment word.
  const groups = new Map<string, DemoEnv>();
  for (const it of sec.items) {
    const key = (it.sub || "Staging").toLowerCase();
    const g = groups.get(key) ?? { key, label: it.sub || "Staging", logins: [], empty: "", rotate: null };
    g.logins.push({ user: it.value || null, label: it.label, reveal: it.reveal });
    groups.set(key, g);
  }
  return [...groups.values()];
}

/** Which environment's logins show: the one picked, else the URL's `env=`, else the first with
 *  logins. The pick is the card's own state (shared by the switch and the rows) and is written to
 *  the URL so a reload keeps it. */
function useDemoEnv(sec: SettingsSection, picked: string | null): [DemoEnv | null, DemoEnv[]] {
  const envs = demoEnvs(sec);
  const asked = usePageSearch().get("env");
  const first = envs.find((e) => e.logins.length) ?? envs[0] ?? null;
  const cur = envs.find((e) => e.key === (picked ?? asked)) ?? first;
  return [cur, envs];
}

function DemoEnvSwitch({ sec, picked, onPick }: { sec: SettingsSection; picked: string | null; onPick: (k: string) => void }) {
  const [cur, envs] = useDemoEnv(sec, picked);
  const pick = (k: string) => { onPick(k); writePxUrl({ env: k }, "replace"); };
  if (envs.length < 2) return null;
  return (
    <div className="px-seg px-set-envs" role="tablist" aria-label="Environment" data-testid="px-set-demo-envs">
      {envs.map((e) => (
        <button key={e.key} type="button" role="tab" aria-selected={cur?.key === e.key} data-testid={`px-set-demo-env-${e.key}`} onClick={() => pick(e.key)}>{e.label}</button>
      ))}
    </div>
  );
}

/** Admin first, then Agent, then Customer, then the rest (screen 13's order). */
const roleRank = (label: string) => { const i = ["admin", "agent", "customer"].indexOf(label.toLowerCase()); return i < 0 ? 3 : i; };

function DemoLogins({ ctx, sec, picked }: { ctx: PxCtx; sec: SettingsSection; picked: string | null }) {
  const [cur] = useDemoEnv(sec, picked);
  const write = useWrite();
  const [said, setSaid] = useState<Line>(NO_LINE);
  const raw = o(sec.raw);
  const from = s(raw.users_from);
  const fromWords = s(raw.users_from_words) || (from === "manifest" ? "boss.deploy.yaml" : from === "repository" ? "the app's seed" : "");
  const rotate = async () => {
    if (!cur?.rotate) return;
    const r = await write("Rotate the demo login password.", () => idPost<unknown>(cur.rotate!.href, {}));
    setSaid(lineOf(r, s(o(r.body).note) || "Rotated."));
  };
  if (!cur) return <p className="px-faint" data-testid="px-demo-logins-none">{sec.hint || "No demo users found · add demo_logins: to boss.deploy.yaml"}</p>;
  return (
    <div data-testid="px-demo-logins">
      {cur.logins.length ? [...cur.logins].sort((x, y) => roleRank(x.label) - roleRank(y.label)).map((l) => (
        <DemoLoginRow key={`${cur.key}-${l.user ?? l.label}`} projectId={ctx.projectId} login={{ label: l.label, value: l.user ?? "", sub: "", reveal: l.reveal, href: "" }} />
      )) : <p className="px-faint" style={{ margin: "6px 0" }} data-testid="px-demo-env-empty">{cur.empty || `No demo login for ${cur.label} yet.`}</p>}
      {sec.hint && cur.logins.length ? <p className="px-faint" style={{ margin: "6px 0 0", fontSize: ".56rem" }} data-testid="px-demo-logins-hint">{sec.hint}</p> : null}
      <div className="px-row px-wrap px-set-foot">
        <Info size={12} aria-hidden />
        {fromWords ? <span>Users from <code className="px-code">{fromWords}</code> · one password per environment (DEMO_PASSWORD)</span>
          : <span>One password per environment (DEMO_PASSWORD)</span>}
        <span className="px-spacer" />
        {cur.rotate ? (
          <GatedButton permission="secret:manage" projectId={ctx.projectId} className="px-linkbtn" data-testid="px-set-demo-rotate"
                       title={`A new ${cur.label} password (asks you to confirm it's you; the app picks it up on its next deploy)`} onClick={() => void rotate()}>
            <ArrowsClockwise size={11} aria-hidden /> Rotate</GatedButton>
        ) : null}
      </div>
      <SaidLine line={said} />
    </div>
  );
}

// ── deploy values (13-08 … 13-13) ───────────────────────────────────────────────────────────

type ValueRow = { name: string; words: string; state: string; tag: string; app_key: string; env: string; set: boolean; managed: string;
                  put: SettingsActionLite | null; remove: SettingsActionLite | null };
type SettingsActionLite = { label: string; href: string; method: string; body: Record<string, unknown> };
const actionOf = (v: unknown): SettingsActionLite => { const y = o(v); return { label: s(y.label), href: s(y.href), method: s(y.method, "GET"), body: o(y.body) }; };

function valueRows(sec: SettingsSection): ValueRow[] {
  const rows = a(o(sec.raw).items);
  if (!rows.length || !s(o(rows[0]).words)) {
    return sec.items.map((it) => ({ name: it.label, words: [it.value, it.sub].filter(Boolean).join(" · "), state: it.value === "not set" ? "you" : "done",
                                    tag: it.value === "generated" ? "generated" : "", app_key: "", env: "deploy", set: it.value !== "not set", managed: "",
                                    put: null, remove: null }));
  }
  return rows.map((r) => {
    const y = o(r);
    const acts = a(y.actions).map(actionOf);
    return { name: s(y.name), words: s(y.words), state: s(y.state, "done"), tag: s(y.tag), app_key: s(y.app_key), env: s(y.env, "deploy"),
             set: y.set !== false, managed: s(y.managed_by),
             put: acts.find((x) => x.method === "PUT") ?? null, remove: acts.find((x) => x.method === "DELETE") ?? null };
  });
}

/** The write-only entry: a value goes in, nothing ever comes back out. */
function ValueEntry({ ctx, name, put, onDone, onCancel }: { ctx: PxCtx; name: string | null; put: SettingsActionLite; onDone: (said: string) => void; onCancel: () => void }) {
  const write = useWrite();
  const [key, setKey] = useState(name ?? "");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [why, setWhy] = useState<Line>(NO_LINE);
  const valid = /^[A-Z][A-Z0-9_]{1,63}$/.test(key) && value.length > 0;
  const save = async () => {
    setBusy(true);
    const href = put.href.replace("{name}", encodeURIComponent(key));
    const r = await write(`Set ${key}.`, () => idPut<unknown>(href, { ...put.body, value, mode: put.body.mode === "generated" ? "test" : (put.body.mode ?? "test") }));
    setBusy(false);
    setValue("");
    if (r.ok) onDone(r.said || `${key} is set. The next deploy uses it.`); else setWhy(lineOf(r, ""));
  };
  return (
    <form className="px-set-entry" data-testid="px-set-value-entry" onSubmit={(e) => { e.preventDefault(); if (valid) void save(); }}>
      {name === null ? <input className="px-input px-mono" aria-label="Name" placeholder="NAME" value={key} autoComplete="off" spellCheck={false}
                              onChange={(e) => setKey(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))} data-testid="px-set-value-name" /> : null}
      <input className="px-input px-mono" type="password" aria-label={`Value for ${key || "the new value"}`} placeholder="Value (write-only)" value={value}
             autoComplete="new-password" onChange={(e) => setValue(e.target.value)} data-testid="px-set-value-input" />
      <GatedButton permission="app_secret:write" projectId={ctx.projectId} type="submit" className="px-btn" data-kind="primary" data-size="s" disabled={!valid || busy} data-testid="px-set-value-save">{busy ? "Saving…" : "Save"}</GatedButton>
      <button type="button" className="px-btn" data-size="s" onClick={onCancel}>Cancel</button>
      {why.text ? <span style={{ flexBasis: "100%" }}><SaidLine line={why} /></span> : null}
    </form>
  );
}

function DeployValues({ ctx, sec, onChanged, adding, setAdding }: { ctx: PxCtx; sec: SettingsSection; onChanged: () => void; adding: boolean; setAdding: (v: boolean) => void }) {
  const rows = valueRows(sec);
  const write = useWrite();
  const [open, setOpen] = useState<string | null>(null);
  const [said, setSaid] = useState<Line>(NO_LINE);
  const [asking, setAsking] = useState<ValueRow | null>(null);
  const add = o(o(sec.raw).add);
  // Remove asks first in the console's own sheet (13-12), never the browser's confirm().
  /** Resolves to null when removed (the sheet closes), else the refusal in words (shown red in the sheet). */
  const remove = async (r: ValueRow): Promise<string | null> => {
    if (!r.remove) return null;
    const res = await write(`Remove ${r.name}.`, () => idDelete<unknown>(r.remove!.href));
    setSaid(lineOf(res, `${r.name} was removed.`));
    if (res.ok) { onChanged(); return null; }
    return lineOf(res, "").text || "It was not removed.";
  };
  const done = (text: string) => { setSaid({ text, ok: true }); setOpen(null); setAdding(false); onChanged(); };
  return (
    <div data-testid="px-set-values">
      {adding && s(add.href) ? <ValueEntry ctx={ctx} name={null} put={actionOf(add)} onDone={done} onCancel={() => setAdding(false)} /> : null}
      {rows.length ? rows.map((r) => (
        <React.Fragment key={r.name}>
          <div className="px-ck-i" data-s={r.state === "you" ? "you" : "done"} data-testid={`px-set-value-${r.name}`}>
            <span className="px-ck-b" aria-hidden>{r.state === "you" ? "!" : "✓"}</span>
            <span className="px-ck-t px-mono" style={{ minWidth: 0 }}><span className="px-ell" style={{ display: "block" }} title={r.name}>{r.name}</span><small>{r.words}</small></span>
            <span className="px-row" style={{ gap: 6 }}>
              {r.tag ? <Tag>{r.tag}</Tag> : null}
              {r.state === "you" && r.put ? (
                <GatedButton permission="app_secret:write" projectId={ctx.projectId} className="px-btn" data-kind="amber" data-size="s" data-testid={`px-set-value-set-${r.name}`}
                             title="Set it (write-only: the value is never shown again)" onClick={() => setOpen(open === r.name ? null : r.name)}>Set</GatedButton>
              ) : null}
              {r.state !== "you" && r.managed === "you" && r.put ? (
                <GatedButton permission="app_secret:write" projectId={ctx.projectId} className="px-chip px-linkbtn" data-testid={`px-set-value-rotate-${r.name}`}
                             title="Replace the value (write-only)" onClick={() => setOpen(open === r.name ? null : r.name)}>Rotate</GatedButton>
              ) : null}
              {r.state !== "you" && r.managed === "you" && r.remove ? (
                <GatedButton permission="app_secret:write" projectId={ctx.projectId} className="px-chip px-linkbtn" data-testid={`px-set-value-remove-${r.name}`}
                             title="Remove it (asks first)" onClick={() => setAsking(r)}>Remove</GatedButton>
              ) : null}
            </span>
          </div>
          {open === r.name && r.put ? <ValueEntry ctx={ctx} name={r.name} put={r.put} onDone={done} onCancel={() => setOpen(null)} /> : null}
        </React.Fragment>
      )) : <p className="px-faint" style={{ margin: 0 }}>No deploy values yet. BOSS generates the ones the app asks for on its first deploy.</p>}
      <SaidLine line={said} />
      {asking ? (
        <ConfirmSheet title={`Remove ${asking.name}?`} press="Remove" pressKind="danger" testid="px-set-value-remove-sheet"
                      lede="Deploys stop receiving this value. It can't be brought back; set it again to restore it."
                      rows={[{ label: "Value", value: asking.name, mono: true }]}
                      onConfirm={() => remove(asking)} onClose={() => setAsking(null)} />
      ) : null}
    </div>
  );
}

// ── targets & hosts (13-14 … 13-16) ─────────────────────────────────────────────────────────

function Targets({ ctx, sec }: { ctx: PxCtx; sec: SettingsSection }) {
  const rows = a(o(sec.raw).items).map(o).filter((r) => r.kind === "target" || r.kind === "missing");
  if (!rows.length && sec.items.length) {
    return <div>{sec.items.map((it) => <div key={`${it.label}-${it.value}`} className="px-ck-i" data-s="done"><span className="px-ck-b" aria-hidden>✓</span><span className="px-ck-t">{it.label}<small>{it.sub}</small></span>{it.href ? <PxLink href={it.href}>Open</PxLink> : <span />}</div>)}</div>;
  }
  return (
    <div data-testid="px-set-targets">
      {rows.map((r) => {
        const act = o(r.action);
        return (
          <div key={s(r.id)} className="px-ck-i" data-s={s(r.state, "done")} data-testid={`px-set-target-${s(r.env)}`}>
            <span className="px-ck-b" aria-hidden>{r.state === "you" ? "!" : "✓"}</span>
            <span className="px-ck-t" style={{ minWidth: 0 }}>
              {s(r.env_label)}
              <small>{s(r.words)}</small></span>
            {r.kind === "missing" ? (
              <a className="px-btn" data-kind="amber" data-size="s" href={pxHref(s(act.open))} data-testid="px-set-pick-host">{s(act.label, "Pick host")}</a>
            ) : s(r.host) ? (
              <PxLink href={s(r.host_href) || `?area=infra&view=fleet&q=${encodeURIComponent(s(r.host))}`} testid={`px-set-host-${s(r.env)}`}
                      title={`${s(r.host_name) || "The host"} — opens the host in Infra`}>{s(r.host)} ↗</PxLink>
            ) : <span />}
          </div>
        );
      })}
      {!rows.length ? <p className="px-faint" style={{ margin: 0 }}>No targets yet. The first deploy sets staging up.</p> : null}
      <span hidden>{ctx.projectId}</span>
    </div>
  );
}

// ── autonomy & budget (13-19 … 13-22) ───────────────────────────────────────────────────────

function Autonomy({ ctx, sec, onChanged }: { ctx: PxCtx; sec: SettingsSection; onChanged: () => void }) {
  const raw = o(sec.raw);
  const write = useWrite();
  const levels = a(raw.levels).map(o);
  const [level, setLevel] = useState(s(raw.level));
  const [said, setSaid] = useState<Line>(NO_LINE);
  const [editing, setEditing] = useState(false);
  const amb = o(raw.ambient);
  const cap = n(amb.week_cap_usd), spent = n(amb.week_spent_usd);
  const [capIn, setCapIn] = useState(String(Math.round(cap)));
  useEffect(() => { setLevel(s(raw.level)); }, [raw.level]);
  const pick = async (key: string) => {
    if (key === level) return;
    const before = level;
    setLevel(key);
    const r = await write(`Set this project's autonomy to ${key}.`, () => idPut<unknown>(PX_PATHS.settings(ctx.projectId).replace(/\/settings$/, "/autonomy"), { level: key }));
    if (!r.ok) setLevel(before);
    setSaid(lineOf(r, `Autonomy is ${levelLabel(key)} for this project.`));
    if (r.ok) onChanged();
  };
  const saveCap = async () => {
    const week = Number(capIn);
    if (!Number.isFinite(week) || week < 0) { setSaid({ text: "Give the week's budget in dollars.", ok: false }); return; }
    const r = await write("Change the ambient budget.", () => idPut<unknown>("/v1/ambient/settings", { project_id: ctx.projectId, daily_cap_usd_micros: Math.round((week / 7) * 1_000_000) }));
    setSaid(lineOf(r, `Ambient budget is $${Math.round(week)} a week.`));
    if (r.ok) { setEditing(false); onChanged(); }
  };
  if (!levels.length) {
    return <div>{sec.items.map((it) => <div key={it.label} className="px-li"><span /><span><b>{it.label}</b><small>{[it.value, it.sub].filter(Boolean).join(" · ")}</small></span><span /></div>)}</div>;
  }
  const pct = cap > 0 ? Math.min(100, (100 * spent) / cap) : 0;
  return (
    <div data-testid="px-set-autonomy">
      <span className="px-lbl" style={{ display: "block" }}>Level</span>
      <div className="px-seg px-set-level" role="radiogroup" aria-label="Autonomy level" style={{ margin: "6px 0 12px" }}>
        {levels.map((l) => (
          <GatedButton key={s(l.key)} permission="autonomy:manage" projectId={ctx.projectId} type="button" role="radio" aria-checked={level === s(l.key)}
                       aria-selected={level === s(l.key)} title={`${levelLabel(s(l.key))} — ${s(l.meaning) || LEVEL_MEANINGS[s(l.key)] || ""}`} data-testid={`px-set-level-${s(l.key)}`} onClick={() => void pick(s(l.key))}>
            {s(l.label)}</GatedButton>
        ))}
      </div>
      <span className="px-lbl" style={{ display: "block" }}>Ambient budget · week</span>
      <div className="px-row" style={{ marginTop: 6 }}>
        <span className="px-bar" style={{ flex: 1, height: 8 }} role="img" aria-label={`$${Math.round(spent)} of $${Math.round(cap)} this week`}>
          <span style={{ width: `${pct}%`, background: "var(--px-violet, #8b7cf6)" }} /></span>
        {editing ? (
          <form className="px-row" style={{ gap: 6 }} onSubmit={(e) => { e.preventDefault(); void saveCap(); }}>
            <span className="px-mono" style={{ fontSize: ".6rem" }}>${Math.round(spent)} /</span>
            <input className="px-input px-mono" style={{ width: 70 }} inputMode="decimal" aria-label="Week's ambient budget, dollars" value={capIn} onChange={(e) => setCapIn(e.target.value)} data-testid="px-set-budget-input" />
            <GatedButton permission="autonomy:manage" projectId={ctx.projectId} type="submit" className="px-btn" data-size="s" data-testid="px-set-budget-save">Save</GatedButton>
          </form>
        ) : (
          <>
            <span className="px-mono" style={{ fontSize: ".6rem" }} data-testid="px-set-budget">${Math.round(spent)} / ${Math.round(cap)}</span>
            <GatedButton permission="autonomy:manage" projectId={ctx.projectId} className="px-icon-btn" aria-label="Change the week's ambient budget" data-testid="px-set-budget-edit"
                         onClick={() => { setCapIn(String(Math.round(cap))); setEditing(true); }}><PencilSimple size={12} /></GatedButton>
          </>
        )}
      </div>
      <span className="px-faint" style={{ fontSize: ".54rem", display: "block", marginTop: 6 }}>
        Org caps still apply (<a href={pxHref(s(raw.govern_href) || "?area=budget&tab=limits")} data-testid="px-set-govern">Govern</a>)</span>
      <SaidLine line={said} />
    </div>
  );
}

// ── repos (13-23 / 13-24) ───────────────────────────────────────────────────────────────────

function Repos({ sec }: { sec: SettingsSection }) {
  const rows = a(o(sec.raw).items).map(o);
  if (!rows.length) return <p className="px-faint" style={{ margin: 0 }}>No repo yet. Add the one this project builds in.</p>;
  return (
    <div data-testid="px-set-repos">
      {rows.map((r) => (
        <div key={s(r.full_name)} className="px-row px-set-repo">
          <GitBranch size={13} aria-hidden />
          <a className="px-mono px-ell" href={s(r.external_url) || `https://github.com/${s(r.full_name)}`} target="_blank" rel="noreferrer noopener" title={`${s(r.full_name)} — open on GitHub`}>{s(r.full_name)}</a>
          <Tag>{s(r.branch, "main")}</Tag>
          <span className="px-spacer" />
          <Tag tone={r.access_ok === true ? "green" : r.access_ok === false ? "red" : undefined}>{s(r.access_words, "access ✓")}</Tag>
        </div>
      ))}
    </div>
  );
}

// ── notifications (13-25 … 13-28) ───────────────────────────────────────────────────────────

function Notifications({ ctx, sec, onChanged }: { ctx: PxCtx; sec: SettingsSection; onChanged: () => void }) {
  const raw = o(sec.raw);
  const routes = a(raw.routes).map(o);
  const cps = a(raw.contact_points).map(o);
  const write = useWrite();
  const [open, setOpen] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const [said, setSaid] = useState<Line>(NO_LINE);
  if (!routes.length) {
    return sec.items.length ? <div>{sec.items.map((it) => <div key={it.label} className="px-li"><span /><span><b>{it.label}</b><small>{it.value}</small></span>{it.href ? <PxLink href={it.href}>Open</PxLink> : <span />}</div>)}</div>
      : <p className="px-faint" style={{ margin: 0 }}>Nobody is told yet.</p>;
  }
  const save = async (event: string, href: string) => {
    const r = await write("Change who is told.", () => idPut<unknown>(href, { contact_point_ids: picked }));
    setSaid(lineOf(r, "Saved."));
    if (r.ok) { setOpen(null); onChanged(); }
  };
  return (
    <div data-testid="px-set-notify">
      <div className="px-grid px-g3 px-set-routes">
        {routes.map((r) => {
          const ev = s(r.event); const I = EVENT_ICON[ev] ?? Bell;
          return (
            <div key={ev} className="px-row" data-testid={`px-set-route-${ev}`}>
              <I size={14} aria-hidden />{s(r.label)}<span className="px-spacer" />
              <GatedButton permission="observability:manage" projectId={ctx.projectId} className="px-tag px-linkbtn" data-tone="blue" data-testid={`px-set-route-edit-${ev}`}
                           title="Change who is told" onClick={() => { setPicked(a(r.contact_point_ids).map((x) => s(x))); setOpen(open === ev ? null : ev); }}>{s(r.words)}</GatedButton>
            </div>
          );
        })}
      </div>
      {open ? (() => {
        const r = routes.find((x) => s(x.event) === open)!;
        return (
          <div className="px-set-entry" role="group" aria-label={`Who is told: ${s(r.label)}`} data-testid="px-set-route-picker">
            {cps.length ? cps.map((c) => (
              <label key={s(c.id)} className="px-row" style={{ gap: 6, fontSize: ".6rem" }}>
                <input type="checkbox" checked={picked.includes(s(c.id))} onChange={(e) => setPicked((p) => (e.target.checked ? [...p, s(c.id)] : p.filter((x) => x !== s(c.id))))} />
                {s(c.word)} · {s(c.name)}
              </label>
            )) : <span className="px-faint">No contact points yet.</span>}
            <span className="px-spacer" />
            <GatedButton permission="observability:manage" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-size="s" data-testid="px-set-route-save"
                         onClick={() => void save(open, s(o(r.set).href))}>Save</GatedButton>
            <button type="button" className="px-btn" data-size="s" onClick={() => setOpen(null)}>Cancel</button>
          </div>
        );
      })() : null}
      <SaidLine line={said} />
    </div>
  );
}

function SectionBody({ ctx, sec, onChanged, adding, setAdding, env }: { ctx: PxCtx; sec: SettingsSection; onChanged: () => void; adding: boolean; setAdding: (v: boolean) => void; env: string | null }) {
  if (sec.key === "demo_logins") return <DemoLogins ctx={ctx} sec={sec} picked={env} />;
  if (sec.key === "deploy_values") return <DeployValues ctx={ctx} sec={sec} onChanged={onChanged} adding={adding} setAdding={setAdding} />;
  if (sec.key === "targets") return <Targets ctx={ctx} sec={sec} />;
  if (sec.key === "captain") return <CaptainSection ctx={ctx} sec={sec} />;
  if (sec.key === "autonomy") return <Autonomy ctx={ctx} sec={sec} onChanged={onChanged} />;
  if (sec.key === "repos") return <Repos sec={sec} />;
  if (sec.key === "notifications") return <Notifications ctx={ctx} sec={sec} onChanged={onChanged} />;
  if (!sec.items.length) return <p className="px-faint" style={{ margin: 0 }}>Nothing set here yet.</p>;
  return (
    <div>
      {sec.items.map((it) => (
        <div key={`${it.label}-${it.value}`} className="px-li">
          <span className="px-ck-b" aria-hidden style={{ borderColor: "var(--green)", background: "var(--green)", color: "#032015" }}>✓</span>
          <span style={{ minWidth: 0 }}><b>{it.label}</b><small>{it.sub}</small></span>
          <span className="px-row">{it.value ? <Tag>{it.value}</Tag> : null}{it.href ? <PxLink href={it.href}>Open</PxLink> : null}</span>
        </div>
      ))}
    </div>
  );
}

// ── captain & squad (13-17 / 13-18) ─────────────────────────────────────────────────────────

function CaptainSection({ ctx, sec }: { ctx: PxCtx; sec: SettingsSection }) {
  const c = ctx.overview.captain;
  const [picking, setPicking] = useState(false);
  const [said, setSaid] = useState<Line>(NO_LINE);
  const choose = async (agentId: string, name: string) => {
    const r = await setCaptain(ctx.projectId, agentId);
    setSaid(r.ok ? { text: r.demo ? "Sample mode — nothing was sent." : `${name} is the captain now.`, ok: true } : { text: r.error ?? "Not changed.", ok: false });
    setPicking(false);
    if (r.ok && !r.demo) notifyChanged("project.captain");
  };
  // Who may captain (§14 `candidates`, the engine's list), else the captain and its squad.
  const choices = c ? (sec.candidates.length ? sec.candidates : [c as Agent, ...c.squad])
    .map((m) => ({ name: m.name, role_key: m.role_key, agent_id: m.agent_id || m.role_key })) : [];
  // Edit squad opens the squad editor right here (13-18 / PX-43); a link that says `edit=squad`
  // (Overview's crew, ⌘K) lands with it open.
  const asked = usePageSearch().get("edit") === "squad";
  const [editing, setEditing] = useState(asked);
  useEffect(() => { if (asked) setEditing(true); }, [asked]);
  return (
    <div data-testid="px-set-captain-body">
      {c ? (
        <div className="px-row px-wrap">
          <Face agent={c} size={34} />
          <div style={{ flex: 1, minWidth: 0 }}><b>{c.name}</b><div className="px-faint" style={{ fontSize: ".56rem" }}>{c.role_key === "engineering_captain" ? "Engineering Captain" : (c as Agent & { role?: string }).role || "Captain"}</div></div>
          <GatedButton permission="project:update" projectId={ctx.projectId} className="px-btn" data-size="s" data-testid="px-set-captain-change"
                       title="Hand the project to another captain" onClick={() => setPicking((v) => !v)}>Change</GatedButton>
        </div>
      ) : <p className="px-faint">No captain yet.</p>}
      {c ? (
        <div className="px-row px-wrap" style={{ marginTop: 10 }}>
          <span className="px-faces">{c.squad.slice(0, 5).map((m) => <Face key={m.name} agent={m} size={22} />)}</span>
          <small className="px-faint" style={{ fontSize: ".56rem" }}>{c.squad.length} in squad</small><span className="px-spacer" />
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-linkbtn" data-testid="px-set-squad" aria-expanded={editing}
                       style={{ fontSize: ".58rem" }} title="Choose who works on this project" onClick={() => setEditing((v) => !v)}>Edit squad</GatedButton>
        </div>
      ) : null}
      {editing && c ? <SquadEditor ctx={ctx} captainKey={c.role_key} onDone={(t) => { setEditing(false); writePxUrl({ edit: null }, "replace"); if (t) setSaid({ text: t, ok: true }); }} /> : null}
      {picking && c ? (
        <CaptainPicker choices={captainChoices(o(sec.raw).candidates, choices)} current={c.role_key}
                       onChoose={(m) => void choose(m.agent_id || m.role_key, m.name)} />
      ) : null}
      <SaidLine line={said} />
    </div>
  );
}

/** THE SQUAD EDITOR (13-18): the project's team — the agents every build here starts with
 *  (project_agents at app "*", through the console's /api/project-agents relay, the same door as
 *  the Command Center's team picker). Remove with ✕, add from the project's agents, Save states
 *  the whole team (`action: set`); the captain is not part of the squad and is never removed. */
function SquadEditor({ ctx, captainKey, onDone }: { ctx: PxCtx; captainKey: string; onDone: (said?: string) => void }) {
  const [team, setTeam] = useState<string[] | null>(null);
  const [roles, setRoles] = useState<{ role_key: string; name: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [line, setLine] = useState<Line>(NO_LINE);
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const [t, r] = await Promise.all([
          fetch(`/api/project-agents?project_id=${encodeURIComponent(ctx.projectId)}&app=*`, { cache: "no-store" }),
          fetch(`/api/agent?view=roles&project_id=${encodeURIComponent(ctx.projectId)}`, { cache: "no-store" }),
        ]);
        const tb = o(t.ok ? await t.json() : {}), rb = o(r.ok ? await r.json() : {});
        if (!live) return;
        setTeam(a(tb.agents).map((x) => s(o(x).role_key)).filter(Boolean));
        setRoles(a(rb.items).map((x) => ({ role_key: s(o(x).role_key), name: s(o(x).display_name) || s(o(x).role_key) })).filter((x) => x.role_key));
      } catch { if (live) { setTeam([]); setLine({ text: "BOSS didn't answer with the project's agents. Try again in a moment.", ok: false }); } }
    })();
    return () => { live = false; };
  }, [ctx.projectId]);
  const nameOf = (k: string) => roles.find((r) => r.role_key === k)?.name || k.replace(/_/g, " ");
  const cur = (team ?? []).filter((k) => k !== captainKey);
  const free = roles.filter((r) => r.role_key !== captainKey && !cur.includes(r.role_key));
  const save = async () => {
    if (isPxDemo()) { setLine({ text: "Sample mode — nothing was sent.", ok: true }); return; }
    if (!cur.length) { setLine({ text: "Keep at least one agent in the squad.", ok: false }); return; }
    setBusy(true);
    try {
      const keep = (team ?? []).includes(captainKey) ? [captainKey, ...cur] : cur;
      const res = await apiWrite("/api/project-agents", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: ctx.projectId, app: "*", action: "set", role_keys: keep }) });
      const body = o(await res.json().catch(() => ({})));
      if (res.ok) { notifyChanged("project.captain"); notifyChanged("agents"); onDone(`The squad is ${cur.map(nameOf).join(", ")}. New work here starts with them.`); return; }
      else setLine({ text: s(body.detail) || s(body.error) || "The squad wasn't changed. Try again.", ok: false });
    } catch { setLine({ text: "BOSS didn't answer. The squad wasn't changed.", ok: false }); }
    setBusy(false);
  };
  return (
    <div className="px-set-entry" role="group" aria-label="Edit squad" data-testid="px-set-squad-editor">
      {team === null ? <span className="px-faint">Loading the squad…</span> : (
        <>
          {cur.map((k) => (
            <span key={k} className="px-chip px-row" style={{ gap: 6 }} data-testid={`px-set-squad-${k}`}>
              <Face agent={{ name: nameOf(k), role_key: k }} size={18} />{nameOf(k)}
              <button type="button" className="px-icon-btn" aria-label={`Take ${nameOf(k)} off the squad`} onClick={() => setTeam((t) => (t ?? []).filter((x) => x !== k))}>✕</button>
            </span>
          ))}
          <select className="px-input" value="" aria-label="Add an agent to the squad" data-testid="px-set-squad-add"
                  onChange={(e) => { const v = e.target.value; if (v) setTeam((t) => [...(t ?? []), v]); }}>
            <option value="">+ Add an agent…</option>
            {free.map((r) => <option key={r.role_key} value={r.role_key}>{r.name}</option>)}
          </select>
          <span className="px-spacer" />
          <GatedButton permission="work_item:manage" projectId={ctx.projectId} className="px-btn" data-kind="primary" data-size="s" disabled={busy} data-testid="px-set-squad-save" onClick={() => void save()}>{busy ? "Saving…" : "Save"}</GatedButton>
          <button type="button" className="px-btn" data-size="s" onClick={() => onDone()}>Cancel</button>
        </>
      )}
      {line.text ? <span style={{ flexBasis: "100%" }}><SaidLine line={line} /></span> : null}
    </div>
  );
}

/** What a reveal answers, whatever the engine's spelling: one password, or a list per user. */
export function revealedPassword(body: unknown, user: string): string {
  const x = o(body);
  if (s(x.password)) return s(x.password);
  if (s(x.value)) return s(x.value);
  const list = a(x.demo_logins ?? x.items ?? x.logins);
  const mine = list.find((d) => s(o(d).user) === user || s(o(d).name) === user) ?? list[0];
  return s(o(mine).password) || s(o(mine).value);
}

/** One demo login (04-40, 08-19…08-22, 13-03…13-05): the role with its icon (Admin's crown in
 *  blue, Agent's headset, Customer's user), the user name in mono, the masked password, Reveal
 *  (owner/admin with step-up, shown 30 s with an amber countdown, audited) and Copy. `compact` is
 *  the read-only form (Overview, Deployments): an eye and a copy icon. */
export function DemoLoginRow({ projectId, login, compact }: {
  projectId: string; login: { label: string; value: string; sub: string; reveal: { href: string; requires: string } | null; href: string }; compact?: boolean;
}) {
  const guarded = useGuardedCall();
  const [shown, setShown] = useState<string | null>(null);
  const [left, setLeft] = useState(0);
  const [why, setWhy] = useState<Line>(NO_LINE);
  useEffect(() => {
    if (!shown) return undefined;
    setLeft(30);
    const t = window.setInterval(() => setLeft((k) => { if (k <= 1) { setShown(null); return 0; } return k - 1; }), 1000);
    return () => window.clearInterval(t);
  }, [shown]);
  const fetchIt = async (): Promise<string | null> => {
    if (!login.reveal) return null;
    setWhy(NO_LINE);
    if (isPxDemo()) return "demo-sample-pass";
    try {
      const body = await guarded("Show the demo login password.", () => idGet<unknown>(login.reveal!.href));
      const pw = revealedPassword(body, login.value);
      if (!pw) setWhy({ text: "BOSS has no password for this login yet. Rotate makes a new one.", ok: false });
      return pw || null;
    } catch (e) {
      setWhy({ text: e instanceof IdentityError && e.code === "step_up_cancelled" ? "Not shown: you cancelled the check." : refusalSentence(e), ok: false, fix: refusalFix(e) });
      return null;
    }
  };
  const role = login.label.toLowerCase();
  const Role = ROLE_ICON[role] ?? UserCircle;
  const tid = login.value || login.label;
  return (
    <div className="px-cred" data-compact={compact ? "" : undefined} data-testid={`px-login-${tid}`}>
      <span className="px-tag" data-tone={role === "admin" ? "blue" : undefined}><Role size={11} aria-hidden /> {login.label}</span>
      <span className="px-mono px-ell" title={login.value}>{login.value || <span className="px-faint">user not named yet</span>}</span>
      <span className="px-row">
        {shown ? <><span className="px-mono" data-testid="px-login-password">{shown}</span><Tag tone="amber" title="Revealed for 30s">{left}s</Tag></> : <span className="px-secret" aria-label="password hidden">••••••••••</span>}
        {login.sub ? <Tag>{login.sub}</Tag> : null}
      </span>
      <span className="px-row px-cred-act">
        {login.reveal ? (
          <>
            <GatedButton permission="secret:manage" projectId={projectId} className={compact ? "px-icon-btn" : "px-btn"} data-size="s" data-testid={`px-login-reveal-${tid}`}
                         title={compact ? "Reveal — owners only, audited" : "Owner/admin · asks you to confirm it's you"} aria-label={shown ? "Hide the password" : "Reveal the password"}
                         onClick={() => { if (shown) { setShown(null); return; } void fetchIt().then((pw) => { if (pw) setShown(pw); }); }}>
              {shown ? <EyeSlash size={compact ? 13 : 12} aria-hidden /> : <Eye size={compact ? 13 : 12} aria-hidden />}{compact ? null : (shown ? " Hide" : " Reveal")}</GatedButton>
            <GatedButton permission="secret:manage" projectId={projectId} className="px-icon-btn" data-testid={`px-login-copy-${tid}`}
                         aria-label={`Copy the password for ${login.value || login.label}`} title="Copy"
                         onClick={() => void (async () => { const pw = shown ?? (await fetchIt()); if (pw) { await navigator.clipboard?.writeText(pw); setWhy({ text: "Copied.", ok: true }); } })()}>
              <Copy size={13} /></GatedButton>
          </>
        ) : login.href ? <PxLink href={login.href}>Open <ArrowSquareOut size={10} /></PxLink> : null}
      </span>
      {why.text ? <span style={{ gridColumn: "1 / -1" }}><SaidLine line={why} /></span> : null}
    </div>
  );
}
