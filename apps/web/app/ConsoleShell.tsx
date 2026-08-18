"use client";

import { isInConsoleLink } from "./console-href";
import { AgentChat } from "./agent-ui";

import React, {
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
  Crown,
  Database,
  Gauge,
  GitBranch,
  Graph,
  ShieldCheck,
  House,
  List,
  MagnifyingGlass,
  Heartbeat,
  Siren,
  PaintBrush,
  SquaresFour,
  Pulse,
  Robot,
  Rocket,
  Rows,
  SlidersHorizontal,
  TerminalWindow,
  TreeStructure,
  UserCircle,
  Users,
  UsersThree,
  Wallet,
  Wrench,
  type Icon,
} from "@phosphor-icons/react";
import {
  communityNavCopy,
  isCommunityEdition,
  isCommunityPrimaryNavVisible,
  normalizeEditionId,
  shouldShowPaidNavBadge,
} from "../lib/edition";
import { EditionChip } from "./edition-ui";

type AreaId =
  | "agent"
  | "metatron"
  | "dashboard"
  | "overview"
  | "decisions"
  | "work"
  | "activity"
  | "portfolio"
  | "discovery"
  | "meetings"
  | "processes"
  | "code_tasks"
  | "shepherd"
  | "delivery"
  | "monitoring"
  | "reliability"
  | "ai-sre"
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
  | "assurance"
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
  metatron: Crown,
  dashboard: Gauge,
  overview: House,
  decisions: CheckCircle,
  work: Rows,
  activity: List,
  portfolio: SlidersHorizontal,
  budget: Wallet,
  pipeline: ArrowsClockwise,
  comms: ChatsCircle,
  github: GitBranch,
  infra: Database,
  ops: Pulse,
  assurance: ShieldCheck,
  org: Users,
  enterprise: Buildings,
  reports: ChartBar,
  capabilities: Wrench,
  meetings: UsersThree,
  processes: TreeStructure,
  code_tasks: Code,
  apps: Rocket,
  shepherd: Code,
  delivery: Code,
  monitoring: Heartbeat,
  reliability: Heartbeat,
  "ai-sre": Siren,
  discovery: MagnifyingGlass,
  sandboxes: TerminalWindow,
  studio: Graph,
  foundations: PaintBrush,
  components: SquaresFour,
} as const;

// THE FULL CONSOLE LIVES HERE. Editions SUBTRACT from it; they never define it.
//
// Activity log, Reliability and Evidence were briefly deleted from this list to get them out
// of the Community sidebar. That hid them everywhere — Enterprise and Cloud too — which is not
// what "cut from Community" means. The edition allowlist in lib/edition.ts is the only place
// that decides what a Community operator sees; this list stays complete so nothing is lost by
// hiding it somewhere else.
//
// Why those three are cut from Community: Reliability and Evidence have nothing behind them
// yet (incidents: 0 rows, drift_reports: 0), and Activity log holds 47,921 events of which
// ~75% are a single internal audit-export event — not empty, unreadable.
const AREAS: AreaDef[] = [
  { id: "foundations", label: "Foundations", sub: "Design system — tokens, type, motion, status language", group: "Platform", icon: ICONS.foundations },
  { id: "components", label: "Components", sub: "Component library — buttons, composer, orbs, overlays", group: "Platform", icon: ICONS.components },
  { id: "overview", label: "Command Center", sub: "What changed · top risks · next actions", group: "Platform", icon: ICONS.overview },
  { id: "agent", label: "BOSS Agent", sub: "One conversation routes into every governed workflow", group: "Run", icon: ICONS.agent },
  { id: "metatron", label: "Metatron", sub: "Always-on captain: chat, goals, validated overwatch", group: "Run", icon: ICONS.metatron },
  { id: "dashboard", label: "Mission Dashboard", sub: "Live cross-phase snapshot — budget, queue, proof, health", group: "Run", icon: ICONS.dashboard },
  { id: "decisions", label: "Approvals", sub: "Explicit approvals before real effects", group: "Run", icon: ICONS.decisions },
  { id: "work", label: "Task Board", sub: "Tasks, artifacts, and the activity feed", group: "Run", icon: ICONS.work },
  { id: "activity", label: "Activity log", sub: "Searchable events, steps, failures, stuck work", group: "Run", icon: ICONS.activity },
  { id: "portfolio", label: "Work Allocation", sub: "Ranked priorities, quotas, leases, tickets, and release planning", group: "Run", icon: ICONS.portfolio },
  { id: "discovery", label: "Discovery", sub: "Research a signal — cited dossier & candidate outcomes", group: "Run", icon: ICONS.discovery },
  { id: "meetings", label: "Meetings", sub: "Cross-functional agent panels — debate to a PRD", group: "Run", icon: ICONS.meetings },
  { id: "processes", label: "Agent processes", sub: "Role handoffs: PRD review → build → QA/UX → panel certify → captain accept", group: "Run", icon: ICONS.processes },
  { id: "code_tasks", label: "Code Tasks", sub: "Agent edits code in a sandbox — tested, gated PRs", group: "Run", icon: ICONS.code_tasks },
  { id: "delivery", label: "CI/CD", sub: "Pipelines, code task to PR to deployment, and where it stopped", group: "Run", icon: ICONS.delivery },
  { id: "monitoring", label: "Monitoring", sub: "Deploy health, metrics, and observability suite", group: "Run", icon: ICONS.monitoring },
  { id: "shepherd", label: "Task Shepherd", sub: "How the shepherd is reasoning about stuck work", group: "Run", icon: ICONS.shepherd },
  { id: "reliability", label: "Reliability", sub: "On-call, AI SRE, incidents, and knowledge graph", group: "Run", icon: ICONS.reliability },
  { id: "ai-sre", label: "AI SRE", sub: "Honest capability ledger — category, status, and remaining gap", group: "Run", icon: ICONS["ai-sre"] },
  { id: "reports", label: "Evidence", sub: "Throughput, receipts, cost, and agent scorecards", group: "Run", icon: ICONS.reports },
  { id: "budget", label: "Budget", sub: "Live spend, reservations, token efficiency, and cap pressure", group: "Govern", icon: ICONS.budget },
  { id: "pipeline", label: "Coding Pipeline", sub: "VAG — cheap models build, frontier models verify · profiles, tiers & run costs", group: "Govern", icon: ICONS.pipeline },
  { id: "comms", label: "Communications", sub: "Outbox discipline before external sends", group: "Govern", icon: ICONS.comms },
  { id: "ops", label: "Operations", sub: "Health, audit, replay, and self-improvement", group: "Govern", icon: ICONS.ops },
  { id: "assurance", label: "Assurance", sub: "Ground truth, proof gates, and security scanning", group: "Govern", icon: ICONS.assurance },
  { id: "org", label: "Agent Roster", sub: "Actors, access, packs, and agent modules", group: "Govern", icon: ICONS.org },
  { id: "github", label: "GitHub", sub: "Repositories, branch truth, sync, conflict resolution, and merges", group: "Platform", icon: ICONS.github },
  { id: "infra", label: "System Configuration", sub: "Connections, providers, resources, worker scaling, and FinOps", group: "Platform", icon: ICONS.infra },
  { id: "capabilities", label: "Capabilities", sub: "Agent tools & MCP servers — discover, audit, extend", group: "Platform", icon: ICONS.capabilities },
  { id: "studio", label: "Agent Studio", sub: "Build focused always-on agents, then compose full studio roles", group: "Platform", icon: ICONS.studio },
  { id: "sandboxes", label: "Sandboxes", sub: "Execution plane — inspect sandboxes & governed console", group: "Platform", icon: ICONS.sandboxes },
  {
    id: "enterprise",
    label: "Editions & SSO",
    sub: "Self-hosted SSO (OIDC/SAML/SCIM) · IdP packs · Community / Cloud / Enterprise",
    group: "Platform",
    icon: ICONS.enterprise,
  },
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
  scope: { project: string; actor: string; projectId?: string; actorId?: string };
  org?: { id: string; name: string };
  /** From GET /v1/edition (or BOSS_EDITION). Defaults to community honesty. */
  editionId?: string;
  initialArea?: string;
  children: ReactNode;
};

export default function ConsoleShell({
  status,
  scope,
  org,
  editionId,
  initialArea,
  children,
}: ConsoleShellProps) {
  const resolvedEdition = normalizeEditionId(editionId);
  const community = isCommunityEdition(resolvedEdition);
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

  // IN-CONSOLE LINKS MUST NOT RELOAD THE CONSOLE.
  //
  // The sidebar switches areas client-side, which is why every area is rendered at once: the
  // trade buys instant tabs. But links inside the page -- "open PRD", "from a panel", "change
  // this app" -- were plain <a href="?area=...">, so each one did a FULL navigation and paid
  // to server-render and re-hydrate all fifty areas: ~6.5MB, ~700 buttons, about ten seconds
  // to open a PRD whose data the browser already had.
  //
  // Nothing about that round trip was needed. The panels already restore their selection from
  // the URL on popstate (see MeetingRooms), so pushing the URL and announcing it does the same
  // work instantly. Anything genuinely external -- previews, PRs, new tabs, modified clicks --
  // is left alone and navigates normally.
  useEffect(() => {
    const onClick = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = (event.target as HTMLElement | null)?.closest?.("a");
      if (!link) return;
      const raw = link.getAttribute("href");
      if (!raw || link.target === "_blank" || link.hasAttribute("download")) return;
      // Only same-document query-string links: "?area=…" or "/?area=…".
      if (!raw.startsWith("?") && !raw.startsWith("/?")) return;

      const url = new URL(raw, window.location.href);
      if (!isInConsoleLink(raw, {
        pathname: url.pathname,
        currentPathname: window.location.pathname,
        target: link.target,
        knownAreas: AREAS.map((a) => a.id),
      })) return;
      const area = url.searchParams.get("area") as AreaId;

      event.preventDefault();
      window.history.pushState({}, "", url.toString());
      // Tell every panel that reads the URL -- the same signal Back/Forward sends.
      window.dispatchEvent(new PopStateEvent("popstate"));
      setActive(area as AreaId);
    };
    // CAPTURE phase, deliberately. The spine nodes and card pills call stopPropagation() so a
    // click on them does not also open the card behind them -- which would stop a bubble-phase
    // listener from ever seeing exactly the links this exists to speed up.
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, []);

  // THE AGENT DOCK.
  //
  // Which app the agent is talking about lives in the URL (?agent=<run id>), not in component
  // state: the console switches areas client-side, so state held here would survive but state
  // held in the area would not -- and the whole point is that you can open a preview, walk to
  // the decision queue, and still be in the same conversation about the same app.
  //
  // The id is a RUN id, which is what makes the conversation unambiguous. AgentChat keys its
  // threads by external_ref, so each app gets its own thread and its own history; asking about
  // BugBox cannot surface ApproveIt's answers.
  const [dockedRun, setDockedRun] = useState<string | null>(null);
  const [dockFacts, setDockFacts] = useState<string | undefined>(undefined);
  useEffect(() => {
    const read = () => {
      const id = new URLSearchParams(window.location.search).get("agent");
      setDockedRun(id);
      try {
        setDockFacts(id ? window.sessionStorage.getItem(`boss.agent.facts.${id}`) ?? undefined : undefined);
      } catch { setDockFacts(undefined); }
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, []);

  // Width is the operator's choice and it persists. A chat you cannot widen is a chat you
  // stop using for anything longer than a sentence.
  const [dockWidth, setDockWidth] = useState(520);
  useEffect(() => {
    const saved = Number(window.localStorage.getItem("boss.dock.width") || 0);
    if (saved >= 320) setDockWidth(Math.min(saved, window.innerWidth - 280));
  }, []);
  const startResize = useCallback((event: React.MouseEvent) => {
    event.preventDefault();
    const onMove = (move: MouseEvent) => {
      // Clamped so the panel can never swallow the console or shrink past readable.
      const next = Math.max(320, Math.min(window.innerWidth - 280, window.innerWidth - move.clientX));
      setDockWidth(next);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
      setDockWidth((w) => { window.localStorage.setItem("boss.dock.width", String(w)); return w; });
    };
    document.body.style.userSelect = "none";     // dragging must not select the page text
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, []);

  const closeDock = useCallback(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete("agent");
    window.history.pushState({}, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, []);

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

  const areasForEdition = useMemo(() => {
    return AREAS.map((area) => {
      if (!community) return area;
      const copy = communityNavCopy(area.id);
      return {
        ...area,
        label: copy.label ?? area.label,
        sub: copy.sub ?? area.sub,
      };
    });
  }, [community]);

  // Sidebar only: Community v1 home is a small cut (UI_V1_CUT.md). Palette +
  // `?area=` keep every AreaId reachable (SSO stays in the sidebar).
  const navAreas = useMemo(
    () => areasForEdition.filter((a) => isCommunityPrimaryNavVisible(a.id, resolvedEdition)),
    [areasForEdition, resolvedEdition],
  );

  const activeDef = areasForEdition.find((a) => a.id === active) ?? areasForEdition[0];
  const tone = budgetTone(status.budgetState);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return areasForEdition;
    return areasForEdition.filter(
      (a) => a.label.toLowerCase().includes(q) || a.sub.toLowerCase().includes(q),
    );
  }, [query, areasForEdition]);

  const badgeFor = (id: AreaId): number | undefined => {
    if (id === "decisions" && status.pendingDecisions > 0) return status.pendingDecisions;
    if (id === "ops" && status.deadletters > 0) return status.deadletters;
    return undefined;
  };

  const labelFor = (area: AreaDef): string => area.label;

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

        <nav
          className="bx-nav"
          aria-label="Console areas"
          data-edition={resolvedEdition}
          data-community={community ? "true" : "false"}
        >
          {GROUP_ORDER.map((group) => (
            <div key={group}>
              <div className="bx-nav-label">{group}</div>
              {navAreas.filter((a) => a.group === group).map((a) => {
                const badge = badgeFor(a.id);
                const Icon = a.icon;
                const paidBadge = shouldShowPaidNavBadge(a.id, resolvedEdition);
                const ssoBadge =
                  community && a.id === "enterprise"
                    ? communityNavCopy("enterprise").badge
                    : undefined;
                return (
                  <button
                    key={a.id}
                    type="button"
                    className={`bx-nav-item${a.id === active ? " active" : ""}`}
                    aria-current={a.id === active ? "page" : undefined}
                    data-area-nav={a.id}
                    data-paid-badge={paidBadge ? "true" : undefined}
                    data-sso-visible={a.id === "enterprise" ? "true" : undefined}
                    onClick={() => go(a.id)}
                  >
                    <Icon size={17} weight={a.id === active ? "fill" : "regular"} aria-hidden />
                    <span>{labelFor(a)}</span>
                    {ssoBadge ? (
                      <span
                        className="bx-nav-badge"
                        data-testid="nav-sso-badge"
                        title="Self-hosted SSO included in Community"
                        style={{ background: "transparent", border: "1px solid var(--green, #3fb950)", color: "var(--green, #3fb950)" }}
                      >
                        {ssoBadge}
                      </span>
                    ) : null}
                    {paidBadge ? (
                      <span
                        className="bx-nav-badge"
                        data-testid={`nav-paid-badge-${a.id}`}
                        title="Some panels in this area are Cloud / Enterprise only"
                        style={{ background: "transparent", border: "1px solid #d29922", color: "#d29922" }}
                      >
                        Paid
                      </span>
                    ) : null}
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

          <EditionChip editionId={resolvedEdition} />

          <span className="bx-chip hide-sm" title={`Budget state: ${status.budgetState}`}>
            <span className={`dot ${tone}`} />
            <b style={{ textTransform: "capitalize" }}>{status.budgetState || "unknown"}</b>
          </span>

          <span className="bx-chip" title="Active actor">
            <UserCircle size={15} aria-hidden />
            <b>{scope.actor}</b>
          </span>
        </header>

        {/* The console gets out of the panel's way rather than hiding behind it. The dock is
            fixed-position, so without this inset the right-hand column of cards sat underneath
            it, unreachable. */}
        <main className="bx-content" data-active={active} aria-label={activeDef.label}
              data-dock={dockedRun ? "open" : undefined}
              style={dockedRun ? { paddingRight: dockWidth + 16 } : undefined}>
          {children}
        </main>
        {dockedRun ? (
          <aside className="bx-dock" aria-label="Ask BOSS about this app"
                 style={{ width: dockWidth }}>
            {/* Drag to resize. The panel is fixed and overlays the page, so without this it
                simply hid the right-hand column of cards with no way to see them again. */}
            <div className="bx-dock-grip" onMouseDown={startResize}
                 role="separator" aria-orientation="vertical"
                 aria-label="Resize the panel" title="Drag to resize" />
            <div className="bx-dock-head">
              <span className="eyebrow">Ask BOSS</span>
              <code className="detail">{dockedRun}</code>
              <button type="button" className="cap-btn bx-dock-close" onClick={closeDock}>
                Close
              </button>
            </div>
            <div className="bx-dock-body">
              <AgentChat projectId={scope.projectId ?? scope.project}
                         actor={scope.actorId ?? scope.actor}
                         initialTaskId={dockedRun} appFacts={dockFacts} />
            </div>
          </aside>
        ) : null}
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
