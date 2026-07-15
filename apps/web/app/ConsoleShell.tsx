"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useTransition,
  type ReactNode,
  type SVGProps,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";

type AreaId =
  | "agent"
  | "dashboard"
  | "overview"
  | "decisions"
  | "work"
  | "discovery"
  | "meetings"
  | "code_tasks"
  | "reports"
  | "budget"
  | "pipeline"
  | "comms"
  | "infra"
  | "capabilities"
  | "sandboxes"
  | "ops"
  | "org"
  | "enterprise";

type AreaDef = {
  id: AreaId;
  label: string;
  sub: string;
  group: "Run" | "Govern" | "Platform";
  icon: (props: SVGProps<SVGSVGElement>) => ReactNode;
};

const I = (d: ReactNode) => (props: SVGProps<SVGSVGElement>) => (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.7}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden
    {...props}
  >
    {d}
  </svg>
);

const ICONS = {
  agent: I(<><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H11l-4.5 4v-4A2.5 2.5 0 0 1 4 13.5Z" /><path d="M8 8h8M8 11.5h5" /></>),
  dashboard: I(<><rect x="3" y="3" width="8" height="8" rx="1.5" /><rect x="13" y="3" width="8" height="5" rx="1.5" /><rect x="13" y="10" width="8" height="11" rx="1.5" /><rect x="3" y="13" width="8" height="8" rx="1.5" /></>),
  overview: I(<><path d="M3 12l9-8 9 8" /><path d="M5 10v10h14V10" /></>),
  decisions: I(<><circle cx="12" cy="12" r="9" /><path d="M8.5 12.5l2.5 2.5 4.5-5" /></>),
  work: I(<><rect x="3" y="4" width="18" height="6" rx="1.5" /><rect x="3" y="14" width="18" height="6" rx="1.5" /></>),
  budget: I(<><rect x="3" y="6" width="18" height="13" rx="2" /><path d="M3 10h18" /><circle cx="16.5" cy="14" r="1.3" /></>),
  comms: I(<><path d="M21 11.5a8 8 0 0 1-11.6 7.1L4 20l1.4-4.4A8 8 0 1 1 21 11.5Z" /></>),
  infra: I(<><rect x="3" y="4" width="18" height="6" rx="1.5" /><rect x="3" y="14" width="18" height="6" rx="1.5" /><path d="M7 7h.01M7 17h.01" /></>),
  ops: I(<><path d="M3 12h4l2.5 7 4-15 2.5 8H21" /></>),
  org: I(<><circle cx="9" cy="8" r="3.2" /><path d="M3.5 20a5.5 5.5 0 0 1 11 0" /><path d="M16 6.5a3 3 0 0 1 0 5.8" /><path d="M17 14.5a5.5 5.5 0 0 1 3.5 5.1" /></>),
  enterprise: I(<><path d="M4 21V6l8-3 8 3v15" /><path d="M9 21v-5h6v5" /><path d="M8 9h.01M12 9h.01M16 9h.01M8 12.5h.01M12 12.5h.01M16 12.5h.01" /></>),
  reports: I(<><path d="M3 21h18" /><rect x="5" y="11" width="3" height="7" rx="1" /><rect x="10.5" y="5" width="3" height="13" rx="1" /><rect x="16" y="13" width="3" height="5" rx="1" /></>),
  capabilities: I(<><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 0 0 5.4-5.4l-2.3 2.3-2-2 2.3-2.3Z" /></>),
  meetings: I(<><circle cx="8" cy="9" r="2.4" /><circle cx="16" cy="9" r="2.4" /><path d="M3.5 19a4.5 4.5 0 0 1 9 0" /><path d="M12.5 18.5a4.5 4.5 0 0 1 8-2.6" /></>),
  code_tasks: I(<><path d="M8 9l-3 3 3 3" /><path d="M16 9l3 3-3 3" /><path d="M13 5l-2 14" /></>),
  discovery: I(<><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /><path d="M11 8v6M8 11h6" /></>),
  sandboxes: I(<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 9h18" /><path d="M7 6.5h.01M10 6.5h.01" /><path d="M7 13l2 2-2 2" /><path d="M12 17h4" /></>),
} as const;

const AREAS: AreaDef[] = [
  { id: "agent", label: "BOSS Agent", sub: "One conversation routes into every governed workflow", group: "Run", icon: ICONS.agent },
  { id: "dashboard", label: "Dashboard", sub: "Live cross-phase snapshot — budget, queue, proof, health", group: "Run", icon: ICONS.dashboard },
  { id: "overview", label: "Overview", sub: "What changed · top risks · next actions", group: "Run", icon: ICONS.overview },
  { id: "decisions", label: "Decisions", sub: "Explicit approvals before real effects", group: "Run", icon: ICONS.decisions },
  { id: "work", label: "Work", sub: "Tasks, artifacts, and the activity feed", group: "Run", icon: ICONS.work },
  { id: "discovery", label: "Discovery", sub: "Research a signal — cited dossier & candidate outcomes", group: "Run", icon: ICONS.discovery },
  { id: "meetings", label: "Meetings", sub: "Cross-functional agent panels — debate to a PRD", group: "Run", icon: ICONS.meetings },
  { id: "code_tasks", label: "Code Tasks", sub: "Agent edits code in a sandbox — tested, gated PRs", group: "Run", icon: ICONS.code_tasks },
  { id: "reports", label: "Reports", sub: "Throughput, cost, and agent scorecards", group: "Run", icon: ICONS.reports },
  { id: "budget", label: "Budget", sub: "Live spend, reservations, and cap pressure", group: "Govern", icon: ICONS.budget },
  { id: "pipeline", label: "Coding Pipeline", sub: "Cost vs rigor — profiles, tiers, and run costs", group: "Govern", icon: ICONS.budget },
  { id: "comms", label: "Communications", sub: "Outbox discipline before external sends", group: "Govern", icon: ICONS.comms },
  { id: "ops", label: "Operations", sub: "Health, audit, replay, and self-improvement", group: "Govern", icon: ICONS.ops },
  { id: "org", label: "Org & Roles", sub: "Actors, access, packs, and agent modules", group: "Govern", icon: ICONS.org },
  { id: "infra", label: "Infrastructure", sub: "Providers, resources, and AI capacity", group: "Platform", icon: ICONS.infra },
  { id: "capabilities", label: "Capabilities", sub: "Agent tools & MCP servers — discover, audit, extend", group: "Platform", icon: ICONS.capabilities },
  { id: "sandboxes", label: "Sandboxes", sub: "Execution plane — inspect sandboxes & governed console", group: "Platform", icon: ICONS.sandboxes },
  { id: "enterprise", label: "Enterprise", sub: "Editions, identity, and account connect", group: "Platform", icon: ICONS.enterprise },
];

const GROUP_ORDER: AreaDef["group"][] = ["Run", "Govern", "Platform"];

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
            src="/BOSS-logo.png"
            alt="BOSS — Business Operating System Sentinel"
          />
          <span className="bx-brand-tag">Autonomous System</span>
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
                    <Icon />
                    <span>{a.label}</span>
                    {badge ? <span className="bx-nav-badge">{badge}</span> : null}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="bx-sidebar-foot">
          <span style={{ display: "inline-flex", alignItems: "center", gap: 7 }}>
            <span className={`dot ${status.apiReachable ? "green" : "red"}`} />
            {status.apiReachable ? "Control plane online" : "Control plane offline"}
          </span>
          <span>{org ? `${org.name} · ${scope.project}` : scope.project}</span>
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
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M4 7h16M4 12h16M4 17h16" />
            </svg>
          </button>

          <div className="bx-topbar-title">
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
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.8">
              <circle cx="12" cy="8" r="3.4" /><path d="M5.5 20a6.5 6.5 0 0 1 13 0" />
            </svg>
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
                    <Icon />
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
