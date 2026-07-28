"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useTransition,
  type ReactNode,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowsClockwise,
  Buildings,
  ChartBar,
  ChatsCircle,
  CheckCircle,
  Code,
  Database,
  Gauge,
  GitBranch,
  Graph,
  House,
  List,
  MagnifyingGlass,
  Heartbeat,
  PaintBrush,
  SquaresFour,
  Pulse,
  Robot,
  Rows,
  SlidersHorizontal,
  TerminalWindow,
  UserCircle,
  Users,
  UsersThree,
  Wallet,
  Wrench,
  type Icon,
} from "@phosphor-icons/react";

type AreaId =
  | "agent"
  | "dashboard"
  | "overview"
  | "decisions"
  | "work"
  | "portfolio"
  | "discovery"
  | "meetings"
  | "code_tasks"
  | "reliability"
  | "reports"
  | "budget"
  | "pipeline"
  | "comms"
  | "github"
  | "infra"
  | "capabilities"
  | "studio"
  | "sandboxes"
  | "foundations"
  | "components"
  | "ops"
  | "org"
  | "enterprise";

type AreaDef = {
  id: AreaId;
  label: string;
  sub: string;
  group: "Run" | "Govern" | "Platform";
  icon: Icon;
};

const ICONS = {
  agent: Robot,
  dashboard: Gauge,
  overview: House,
  decisions: CheckCircle,
  work: Rows,
  portfolio: SlidersHorizontal,
  budget: Wallet,
  pipeline: ArrowsClockwise,
  comms: ChatsCircle,
  github: GitBranch,
  infra: Database,
  ops: Pulse,
  org: Users,
  enterprise: Buildings,
  reports: ChartBar,
  capabilities: Wrench,
  meetings: UsersThree,
  code_tasks: Code,
  reliability: Heartbeat,
  discovery: MagnifyingGlass,
  sandboxes: TerminalWindow,
  studio: Graph,
  foundations: PaintBrush,
  components: SquaresFour,
} as const;

const AREAS: AreaDef[] = [
  { id: "foundations", label: "Foundations", sub: "Design system — tokens, type, motion, status language", group: "Platform", icon: ICONS.foundations },
  { id: "components", label: "Components", sub: "Component library — buttons, composer, orbs, overlays", group: "Platform", icon: ICONS.components },
  { id: "overview", label: "Command Center", sub: "What changed · top risks · next actions", group: "Platform", icon: ICONS.overview },
  { id: "agent", label: "BOSS Agent", sub: "One conversation routes into every governed workflow", group: "Run", icon: ICONS.agent },
  { id: "dashboard", label: "Mission Dashboard", sub: "Live cross-phase snapshot — budget, queue, proof, health", group: "Run", icon: ICONS.dashboard },
  { id: "decisions", label: "Approvals", sub: "Explicit approvals before real effects", group: "Run", icon: ICONS.decisions },
  { id: "work", label: "Task Board", sub: "Tasks, artifacts, and the activity feed", group: "Run", icon: ICONS.work },
  { id: "portfolio", label: "Work Allocation", sub: "Ranked priorities, quotas, leases, tickets, and delivery status", group: "Run", icon: ICONS.portfolio },
  { id: "discovery", label: "Discovery", sub: "Research a signal — cited dossier & candidate outcomes", group: "Run", icon: ICONS.discovery },
  { id: "meetings", label: "Meetings", sub: "Cross-functional agent panels — debate to a PRD", group: "Run", icon: ICONS.meetings },
  { id: "code_tasks", label: "Code Tasks", sub: "Agent edits code in a sandbox — tested, gated PRs", group: "Run", icon: ICONS.code_tasks },
  { id: "reliability", label: "Reliability", sub: "AI SRE — incidents, root cause, governed fixes, learning", group: "Run", icon: ICONS.reliability },
  { id: "reports", label: "Evidence", sub: "Throughput, receipts, cost, and agent scorecards", group: "Run", icon: ICONS.reports },
  { id: "budget", label: "Budget", sub: "Live spend, reservations, and cap pressure", group: "Govern", icon: ICONS.budget },
  { id: "pipeline", label: "Coding Pipeline", sub: "VAG — cheap models build, frontier models verify · profiles, tiers & run costs", group: "Govern", icon: ICONS.pipeline },
  { id: "comms", label: "Communications", sub: "Outbox discipline before external sends", group: "Govern", icon: ICONS.comms },
  { id: "ops", label: "Operations", sub: "Health, audit, replay, and self-improvement", group: "Govern", icon: ICONS.ops },
  { id: "org", label: "Agent Roster", sub: "Actors, access, packs, and agent modules", group: "Govern", icon: ICONS.org },
  { id: "github", label: "GitHub", sub: "Repositories, branch truth, sync, conflict resolution, and merges", group: "Platform", icon: ICONS.github },
  { id: "infra", label: "System Configuration", sub: "Connections, providers, resources, health, and FinOps", group: "Platform", icon: ICONS.infra },
  { id: "capabilities", label: "Capabilities", sub: "Agent tools & MCP servers — discover, audit, extend", group: "Platform", icon: ICONS.capabilities },
  { id: "studio", label: "Agent Studio", sub: "Build, test, compose, and observe agents working together", group: "Platform", icon: ICONS.studio },
  { id: "sandboxes", label: "Sandboxes", sub: "Execution plane — inspect sandboxes & governed console", group: "Platform", icon: ICONS.sandboxes },
  { id: "enterprise", label: "Enterprise", sub: "Editions, identity, and account connect", group: "Platform", icon: ICONS.enterprise },
];

const GROUP_ORDER: AreaDef["group"][] = ["Platform", "Run", "Govern"];

function budgetTone(state?: string): "green" | "amber" | "red" {
  const s = (state ?? "").toLowerCase();
  if (s === "red") return "red";
  if (s === "orange" || s === "yellow") return "amber";
  return "green";
}

export type ConsoleShellProps = {
  status: {
    apiReachable: boolean;
    budgetState: string;
    deadletters: number;
    pendingDecisions: number;
  };
  scope: { project: string; actor: string };
  org?: { id: string; name: string };
  initialArea?: string;
  children: ReactNode;
};

export default function ConsoleShell({ status, scope, org, initialArea, children }: ConsoleShellProps) {
  const switchOrg = () => {
    try {
      window.localStorage.removeItem("boss.area");
      window.localStorage.removeItem("boss.org");
    } catch {}
    window.location.href = "/?pick=1";
  };
  const deepLinkArea = (AREAS.some((a) => a.id === initialArea) ? initialArea : undefined) as AreaId | undefined;
  const [active, setActive] = useState<AreaId>(deepLinkArea ?? "overview");
  const [navOpen, setNavOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [live, setLive] = useState(true);
  const paletteInput = useRef<HTMLInputElement>(null);
  const router = useRouter();
  const searchParams = useSearchParams();

  // Live auto-refresh: re-fetch server data on an interval so async worker
  // changes (new decisions, budget moves, events) appear without manual reload.
  // Skips while the user is typing or the palette is open so nothing is interrupted.
  // Guardrails (the console SSR fans out ~55 API calls per refresh and takes
  // multiple seconds — an unguarded 7s loop stacked refreshes and starved the
  // boss-api healthcheck, 502ing the console mid-session):
  //  - never stack refreshes: skip while the previous one is still rendering
  //  - never refresh a hidden tab: a console left open in a background tab
  //    must not keep hammering boss-api all day
  const [refreshPending, startRefresh] = useTransition();
  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => {
      if (paletteOpen || refreshPending || document.hidden) return;
      const tag = document.activeElement?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      startRefresh(() => router.refresh());
    }, 15000);
    return () => clearInterval(id);
  }, [live, paletteOpen, refreshPending, router]);

  // Depend on the query STRING (stable across router.refresh(), changes on real
  // navigation) so the area syncs from the URL on deep links like "View in Work →"
  // without the 7s auto-refresh snapping the user back.
  const queryString = searchParams.toString();
  useEffect(() => {
    const area = new URLSearchParams(queryString).get("area");
    if (area && AREAS.some((a) => a.id === area)) {
      setActive(area as AreaId);
      return;
    }
    const stored = window.localStorage.getItem("boss.area");
    if (stored && AREAS.some((a) => a.id === stored)) {
      setActive(stored as AreaId);
    }
  }, [queryString]);

  const go = useCallback((id: AreaId) => {
    setActive(id);
    setNavOpen(false);
    setPaletteOpen(false);
    setQuery("");
    window.localStorage.setItem("boss.area", id);
    // Reflect the selected tab in the URL as a new history entry (so Back
    // returns to the previous tab) WITHOUT a server round-trip / data refetch.
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("area", id);
      if (url.toString() !== window.location.href) {
        window.history.pushState(null, "", url.toString());
      }
    } catch {
      /* non-browser / unsupported — ignore */
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  // Quick-action links (AreaLink in the Overview command center) switch areas
  // client-side via this event — instant, no full-page navigation/refetch.
  useEffect(() => {
    const onNav = (e: Event) => {
      const id = (e as CustomEvent).detail as AreaId;
      if (AREAS.some((a) => a.id === id)) go(id);
    };
    window.addEventListener("boss:navigate", onNav as EventListener);
    return () => window.removeEventListener("boss:navigate", onNav as EventListener);
  }, [go]);

  // Back/forward: restore the active area from the URL (without pushing again).
  useEffect(() => {
    const onPop = () => {
      const area = new URLSearchParams(window.location.search).get("area");
      if (area && AREAS.some((a) => a.id === area)) setActive(area as AreaId);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      } else if (e.key === "Escape") {
        setPaletteOpen(false);
        setNavOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (paletteOpen) {
      const t = setTimeout(() => paletteInput.current?.focus(), 20);
      return () => clearTimeout(t);
    }
  }, [paletteOpen]);

  const activeDef = AREAS.find((a) => a.id === active) ?? AREAS[0];
  const tone = budgetTone(status.budgetState);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return AREAS;
    return AREAS.filter(
      (a) => a.label.toLowerCase().includes(q) || a.sub.toLowerCase().includes(q),
    );
  }, [query]);

  const badgeFor = (id: AreaId): number | undefined => {
    if (id === "decisions" && status.pendingDecisions > 0) return status.pendingDecisions;
    if (id === "ops" && status.deadletters > 0) return status.deadletters;
    return undefined;
  };

  return (
    <div className={`bx-shell${navOpen ? " nav-open" : ""}`}>
      <a className="skip-link" href="#decision-inbox">Skip to decision inbox</a>

      <aside className="bx-sidebar">
        <div className="bx-brand">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            className="bx-brand-logo"
            src="/boss-mark.png"
            alt=""
          />
          <span className="bx-brand-copy">
            <span className="bx-brand-name">BOSS</span>
            <span className="bx-brand-tag">Orchestration OS</span>
          </span>
        </div>

        <nav className="bx-nav" aria-label="Console areas">
          {GROUP_ORDER.map((group) => (
            <div key={group}>
              <div className="bx-nav-label">{group}</div>
              {AREAS.filter((a) => a.group === group).map((a) => {
                const badge = badgeFor(a.id);
                const Icon = a.icon;
                return (
                  <button
                    key={a.id}
                    type="button"
                    className={`bx-nav-item${a.id === active ? " active" : ""}`}
                    aria-current={a.id === active ? "page" : undefined}
                    onClick={() => go(a.id)}
                  >
                    <Icon size={17} weight={a.id === active ? "fill" : "regular"} aria-hidden />
                    <span>{a.label}</span>
                    {badge ? <span className="bx-nav-badge">{badge}</span> : null}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="bx-sidebar-foot">
          <div className="bx-operator">
            <span className="bx-operator-avatar" aria-hidden>{scope.actor.slice(0, 1).toUpperCase()}</span>
            <span>
              <strong>{scope.actor}</strong>
              <small>Operator · in control</small>
            </span>
          </div>
          <div className="bx-control-status">
            <span className={`dot ${status.apiReachable ? "green" : "red"}`} />
            <span>{status.apiReachable ? "Control plane online" : "Control plane offline"}</span>
          </div>
        </div>
      </aside>

      {navOpen ? <div className="bx-scrim" onClick={() => setNavOpen(false)} /> : null}

      <div className="bx-main">
        <header className="bx-topbar">
          <button
            type="button"
            className="bx-hamburger"
            aria-label="Toggle navigation"
            onClick={() => setNavOpen((v) => !v)}
          >
            <List size={18} aria-hidden />
          </button>

          <div className="bx-topbar-title" id="boss-console-title">
            {activeDef.label}
            <small>{activeDef.sub}</small>
          </div>

          <div className="bx-topbar-spacer" />

          {org ? (
            <button type="button" className="bx-org-chip" onClick={switchOrg} title="Switch organization">
              <span className="bx-org-mark">{org.name.slice(0, 2).toUpperCase()}</span>
              <span className="bx-org-name">{org.name}</span>
              <span className="bx-org-switch">switch</span>
            </button>
          ) : null}

          <button
            type="button"
            className="bx-chip hide-sm"
            title={live ? "Live: auto-refreshing every 7s — click to pause" : "Paused — click to resume live updates"}
            onClick={() => setLive((v) => !v)}
            style={{ cursor: "pointer" }}
          >
            <span className={live ? "dot green bx-pulse" : "dot"} />
            {live ? "Live" : "Paused"}
          </button>

          <button type="button" className="bx-kbd" onClick={() => setPaletteOpen(true)}>
            <span>Jump to</span>
            <kbd>⌘K</kbd>
          </button>

          <span className="bx-chip hide-sm" title={`Budget state: ${status.budgetState}`}>
            <span className={`dot ${tone}`} />
            <b style={{ textTransform: "capitalize" }}>{status.budgetState || "unknown"}</b>
          </span>

          <span className="bx-chip" title="Active actor">
            <UserCircle size={15} aria-hidden />
            <b>{scope.actor}</b>
          </span>
        </header>

        <main className="bx-content" data-active={active} aria-label={activeDef.label}>
          {children}
        </main>
      </div>

      {paletteOpen ? (
        <div className="bx-palette-backdrop" onClick={() => setPaletteOpen(false)}>
          <div
            className="bx-palette"
            role="dialog"
            aria-modal="true"
            aria-label="Jump to area"
            onClick={(e) => e.stopPropagation()}
          >
            <input
              ref={paletteInput}
              value={query}
              placeholder="Jump to an area…"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && filtered[0]) go(filtered[0].id);
              }}
            />
            <div className="bx-palette-list">
              {filtered.map((a, i) => {
                const Icon = a.icon;
                return (
                  <button
                    key={a.id}
                    type="button"
                    className={`bx-palette-item${i === 0 ? " active" : ""}`}
                    onClick={() => go(a.id)}
                  >
                    <Icon size={17} weight={i === 0 ? "fill" : "regular"} aria-hidden />
                    <span>
                      <span style={{ fontWeight: 600 }}>{a.label}</span>
                      <span style={{ display: "block", fontSize: "0.78rem", color: "var(--muted)" }}>{a.sub}</span>
                    </span>
                  </button>
                );
              })}
              {filtered.length === 0 ? (
                <div style={{ padding: "14px 12px", color: "var(--muted)" }}>No matching area.</div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
