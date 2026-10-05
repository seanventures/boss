"use client";

import { IDENTITY_PARAMS } from "./url-intent";
import { isInConsoleLink } from "./console-href";
import { resolveLegacyArea } from "./legacy-areas";
import { SETUP_BADGE_EVENT } from "../lib/setup-stepper";
import { onChanged } from "./changed";
import { createRefreshCoordinator, registerConsoleRefresh } from "../lib/console-refresh";
import {
  cookieWith,
  loadedAreasCookie,
  loadedAreasCookieValue,
} from "../lib/area-loading";
import { ConsoleToggle } from "../components/v2/ConsoleToggle";
import { AgentChat } from "./agent-ui";
import { dockChatKey, dockSpeaker } from "../lib/dock-subject";
import { ProjectStrip } from "./project-strip";
import { PresencePill } from "./agents/presence-pill";
import { openPlan } from "../lib/converse-doors";
import { setConsoleLive } from "../lib/console-live";
import { PAINTED_EVENT } from "../lib/area-painted";
import { useAttention } from "../lib/agents-live";
import { useTitleCount } from "../lib/tab-title";
import { ConverseHost } from "./agents/converse-host";
import { useLoadIdentities } from "../lib/use-agent-identities";
import { CONVERSE_COMMANDS } from "../lib/agent-converse";

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
  HardDrives,
  ShieldCheck,
  House,
  List,
  MagnifyingGlass,
  CaretRight,
  FlowArrow,
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
  X,
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
import { areaVisibilityCss } from "../lib/area-visibility";
import type { AreaId } from "../lib/area-ids";

export type { AreaId };

type AreaDef = {
  id: AreaId;
  label: string;
  sub: string;
  //: THE GROUP AND THE AREA SWAPPED NAMES. "Govern" was the group heading and "Settings" was
  //: one area inside it — which read backwards: the knobs (concurrency, turn budgets, spend)
  //: ARE the governing, and everything else in the group is a setting. So the group is
  //: "Settings" and the area that holds the governing knobs is "Govern".
  group: "Run" | "Settings" | "Platform";
  icon: Icon;
  /** The section this area sits INSIDE.
   *
   *  Observability is not one screen, it is a place you go with a question: is it up, and what
   *  happened. Those are two screens — the dashboards and the log — and listing them as
   *  unrelated siblings made the log look like a separate product rather than the thing you
   *  open when a dashboard says something is wrong. */
  parent?: NavSectionId;
};

// No section holds an area today: Observability is one area (infra-observability spec §4.1) and
// the dead `observability` section that no area used is gone. The machinery stays for the next.
type NavSectionId = string;

/** Sections that hold areas. A section is not itself an area: it has no screen, and clicking it
 *  opens or closes the group rather than navigating somewhere that does not exist. */
const NAV_SECTIONS: Record<NavSectionId, { label: string; icon: Icon; group: AreaDef["group"] }> = {};

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
  infra: HardDrives,
  observability: Heartbeat,
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
  delivery_workflows: FlowArrow,
  monitoring: Heartbeat,
  reliability: Heartbeat,
  "ai-sre": Siren,
  discovery: MagnifyingGlass,
  sandboxes: TerminalWindow,
  foundations: PaintBrush,
  components: SquaresFour,
} as const;

// THE FULL CONSOLE LIVES HERE. Editions SUBTRACT from it; they never define it.
//
// Order inside a group is the ORDER OF WORK, not the order things were built: research it
// (Discovery), spec it (Meetings), build it (Factory), approve it (Approvals), ship it (CI/CD),
// watch it (Monitoring). It had drifted into an accretion — Approvals and Task Board sat above
// Discovery because they existed first — so the sidebar described the codebase's history
// instead of the operator's path.
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
/** Areas that still EXIST but no longer earn a place in the left rail.
 *
 *  Research and the PRD panel are steps of a workflow now, not destinations: the workflow
 *  drives both, and every run links to the dossier it produced and the panel it convened. Two
 *  permanent nav entries for things you arrive at from somewhere else is two rows of standing
 *  cost for a trip nobody takes deliberately. Reachable by URL and by every link that names
 *  them — hidden from the rail, not removed from the console. */
//  AND THE ANSWER TO AN UNREACHABLE DOSSIER IS NOT A NAV ROW. A discovery started from chat
//  belonged to no run, so nothing linked to it — briefly fixed here by putting Discovery back
//  on the rail, which is the wrong end of the problem: it advertises starting one on its own,
//  and nobody should. The chat agent starts a WORKFLOW now (see start_workflow in builtin.py),
//  so the dossier is a step's output and the run is what you open.
const OFF_THE_NAV = new Set<AreaId>(["discovery", "meetings"]);

const AREAS: AreaDef[] = [
  { id: "overview", label: "Command Center", sub: "What needs you · what's moving", group: "Platform", icon: ICONS.overview },
  // THE ORDER IS THE ARGUMENT. The Run group opens with the place work is DONE and the path it
  // follows — Factory, then the workflow governing it, then who does it and who approves it,
  // then where it ships and whether it is alive. Discovery and Meetings are ways IN and follow
  // after; they were first only because they come first chronologically, which is not the same
  // as being what an operator opens the console to look at.
  { id: "code_tasks", label: "Factory", sub: "Builds, apps and gated PRs", group: "Run", icon: ICONS.code_tasks },
  // ONE HOME PER OBJECT. Workflow, Agent work, Team structure and Agent Studio were four rows
  // for two things: workflows (built, run and watched in one area) and the agents that work
  // them (roster, team and panels in one area). Old links to the retired two still land —
  // app/legacy-areas.ts.
  { id: "delivery_workflows", label: "Workflows", sub: "Build, run and watch workflows", group: "Run", icon: ICONS.delivery_workflows },
  { id: "workforce", label: "Agent Studio", sub: "Who does the work", group: "Run", icon: ICONS.org },
  { id: "decisions", label: "Approvals", sub: "Explicit approvals before real effects", group: "Run", icon: ICONS.decisions },
  // TWO SCREENS, TWO QUESTIONS (infra-observability spec §0, §4.1). Infra is where it runs and
  // how it got there; Observability is whether it is alive, how fast, and what thinking costs.
  // The ids say so now: `delivery` (labelled Infra) and `monitoring` (labelled Observability)
  // redirect here (app/legacy-areas.ts), and System Configuration's contents moved into Infra ›
  // Cloud, Repo and models, Skills & MCP and Observability, so it has no rail entry.
  { id: "infra", label: "Infra", sub: "Where your apps run and how they got there", group: "Run", icon: ICONS.infra },
  { id: "observability", label: "Observability", sub: "Is it alive, how fast, what thinking costs", group: "Run", icon: ICONS.observability },
  { id: "activity", label: "Activity log", sub: "Every line BOSS wrote, searchable", group: "Run", icon: ICONS.activity },
  { id: "discovery", label: "Discovery", sub: "Research a signal — cited dossier & candidate outcomes", group: "Run", icon: ICONS.discovery },
  { id: "meetings", label: "Meetings", sub: "Cross-functional agent panels — debate to a PRD", group: "Run", icon: ICONS.meetings },
  // ONE ENTRY, NOT TWO. "Metatron" was an always-on captain whose session table does not exist
  // in this database — a page over state that was never created — and the operator never used
  // it. Its idea (a captain with the whole factory in view) is what the BOSS Agent area now IS.
  { id: "agent", label: "BOSS Agent", sub: "What needs you, across the whole factory — and one conversation that can act on it", group: "Run", icon: ICONS.agent },
  { id: "dashboard", label: "Mission Dashboard", sub: "Live cross-phase snapshot — budget, queue, proof, health", group: "Run", icon: ICONS.dashboard },
  { id: "work", label: "Task Board", sub: "Tasks, artifacts, and the activity feed", group: "Run", icon: ICONS.work },

  { id: "portfolio", label: "Work Allocation", sub: "Ranked priorities, quotas, leases, tickets, and release planning", group: "Run", icon: ICONS.portfolio },
  { id: "processes", label: "Agent processes", sub: "Role handoffs: PRD review → build → QA/UX → panel certify → captain accept", group: "Run", icon: ICONS.processes },
  { id: "shepherd", label: "Task Shepherd", sub: "How the shepherd is reasoning about stuck work", group: "Run", icon: ICONS.shepherd },
  { id: "reliability", label: "Reliability", sub: "On-call, AI SRE, incidents, and knowledge graph", group: "Run", icon: ICONS.reliability },
  { id: "ai-sre", label: "AI SRE", sub: "Honest capability ledger — category, status, and remaining gap", group: "Run", icon: ICONS["ai-sre"] },
  { id: "reports", label: "Evidence", sub: "Throughput, receipts, cost, and agent scorecards", group: "Run", icon: ICONS.reports },
  // NAMED FOR WHAT IS IN IT, AND FIRST IN SETTINGS. "Setup" is a stage of the product's life —
  // it reads as something you do once and never open again, so the page an operator needs when
  // a model runs out of credit or a repo token expires was filed under a name that says
  // "already done". It carries a badge while anything on it is unfinished or failing.
  { id: "setup", label: "Repo and models", sub: "Where BOSS writes code and what it thinks with", group: "Settings", icon: Database },
  // WHAT YOUR CREW CAN DO (skills-mcp spec §5.1): the Kit of abilities, who holds them, and every
  // skill and MCP server. Second in Settings: a repo and a model come first.
  { id: "skills", label: "Skills & MCP", sub: "What your crew can do", group: "Settings", icon: ICONS.capabilities },
  { id: "budget", label: "Govern", sub: "Spend caps, pace and limits", group: "Settings", icon: ICONS.budget },
  { id: "pipeline", label: "Coding Pipeline", sub: "VAG — cheap models build, frontier models verify · profiles, tiers & run costs", group: "Settings", icon: ICONS.pipeline },
  { id: "comms", label: "Communications", sub: "Outbox discipline before external sends", group: "Settings", icon: ICONS.comms },
  { id: "ops", label: "Operations", sub: "Health, audit, replay, and self-improvement", group: "Settings", icon: ICONS.ops },
  { id: "assurance", label: "Assurance", sub: "Ground truth, proof gates, and security scanning", group: "Settings", icon: ICONS.assurance },
  { id: "org", label: "Access and packs", sub: "Actors, permissions, packs, and modules", group: "Settings", icon: ICONS.org },
  { id: "github", label: "GitHub", sub: "Repositories, branch truth, sync, conflict resolution, and merges", group: "Platform", icon: ICONS.github },
  { id: "capabilities", label: "Capabilities", sub: "Agent tools & MCP servers — discover, audit, extend", group: "Platform", icon: ICONS.capabilities },
  { id: "sandboxes", label: "Sandboxes", sub: "Execution plane — inspect sandboxes & governed console", group: "Platform", icon: ICONS.sandboxes },
  { id: "foundations", label: "Foundations", sub: "Design system — tokens, type, motion, status language", group: "Platform", icon: ICONS.foundations },
  { id: "components", label: "Components", sub: "Component library — buttons, composer, orbs, overlays", group: "Platform", icon: ICONS.components },
  {
    id: "enterprise",
    // WHO CAN GET IN is a setting, not a platform feature sitting above the work. It was
    // second in the sidebar, under Command Center, ahead of everything an operator actually
    // does — and called "SSO", which names one protocol rather than the thing it configures.
    label: "Identity",
    sub: "Identity providers and member sync — console sign-in is the installer password",
    group: "Settings",
    icon: ICONS.enterprise,
  },
];

/** The CSS that reveals the active area, generated from AREAS so a navigable area can never be
 *  a hidden one (lib/area-visibility.ts says how AI SRE shipped blank). */
const AREA_VISIBILITY_CSS = areaVisibilityCss(AREAS.map((a) => a.id));

const GROUP_ORDER: AreaDef["group"][] = ["Platform", "Run", "Settings"];

function budgetTone(state?: string): "green" | "amber" | "red" {
  const s = (state ?? "").toLowerCase();
  if (s === "red") return "red";
  if (s === "orange" || s === "yellow") return "amber";
  return "green";
}

/** A control-plane URL, shortened to the part that tells them apart. */
function planeLabel(url: string): string {
  try {
    const host = new URL(url).host;
    return isLocalPlane(url) ? `local · ${host}` : host;
  } catch {
    return url;
  }
}

/** Local means "this laptop's database", which is the case worth calling out: it looks like
 *  production and contains almost none of it. */
function isLocalPlane(url: string): boolean {
  return /^(https?:\/\/)?(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:|$|\/)/i.test(url);
}

export type ConsoleShellProps = {
  status: {
    apiReachable: boolean;
    budgetState: string;
    deadletters: number;
    pendingDecisions: number;
    /** Repo and models' rail badge, from GET /v1/setup: how many rows are unfinished or
     *  failing, red when any check is failing. Absent or 0: no badge. */
    setup?: { count: number; tone: "amber" | "red" } | null;
    /** WHICH control plane this console is reading.
     *
     *  Two consoles with identical chrome and completely different worlds is how an operator
     *  concludes their data has been lost: a dev server pointed at a laptop database showed one
     *  project beside a tab showing forty, and nothing on either screen said why. For a customer
     *  with staging and production open in two tabs it is worse than confusing.
     *
     *  Comes from the SERVER, because every fetch on this page is server-side — the browser's
     *  build-time NEXT_PUBLIC value can disagree with what is actually being called. */
    controlPlane?: string;
  };
  scope: { project: string; actor: string; projectId?: string; actorId?: string };
  org?: { id: string; name: string };
  /** From GET /v1/edition (or BOSS_EDITION). Defaults to community honesty. */
  editionId?: string;
  initialArea?: string;
  /** True when console sign-in is configured (BOSS_CONSOLE_PASSWORD set); Sign out is shown only
   *  then. Read on the server, where the password lives. */
  signOutAvailable?: boolean;
  /** The areas the server rendered this time (lib/area-loading.ts). An area not in it is not
   *  in the DOM yet: opening it shows a loading state and asks the server for it. Absent means
   *  every area was rendered, which is how this shell behaved before areas went lazy. */
  loadedAreas?: string[];
  children: ReactNode;
};

export default function ConsoleShell({
  status,
  scope,
  org,
  editionId,
  initialArea,
  signOutAvailable = false,
  loadedAreas,
  children,
}: ConsoleShellProps) {
  const resolvedEdition = normalizeEditionId(editionId);
  const community = isCommunityEdition(resolvedEdition);
  // Every agent name and face in the console reads the install's identities from one store.
  useLoadIdentities(scope.projectId ?? scope.project);
  // THE TAB TITLE'S `(n)` HAS ONE OWNER: the shell, from the decisions that need you (the
  // pill's own count), re-applied whenever a navigation rewrites the title (lib/tab-title.ts).
  const titleAttention = useAttention(scope.projectId ?? scope.project);
  useTitleCount(titleAttention?.count ?? 0);
  const switchOrg = () => {
    try {
      window.localStorage.removeItem("boss.area");
      window.localStorage.removeItem("boss.org");
    } catch {}
    window.location.href = "/?pick=1";
  };
  const deepLinkArea = (AREAS.some((a) => a.id === initialArea) ? initialArea : undefined) as AreaId | undefined;
  const [active, setActive] = useState<AreaId>(deepLinkArea ?? "overview");
  // `active` is the area currently painted. `navTarget` mirrors the last press; with
  // client-side navigation the two are set together and never diverge, but the rail reads
  // `selected` so that a future change which does defer `active` cannot silently stop the
  // highlight from answering the click.
  const [navTarget, setNavTarget] = useState<AreaId | null>(null);
  // No transition: there is nothing to wait for. A rail click reveals a section already in
  // the DOM, so `active` commits on the press and the screen never blanks or lags.
  const selected = navTarget ?? active;
  useEffect(() => {
    // The transition committed (or the URL changed underneath us). Stop showing a target that
    // is now simply the active area, so a later failure cannot leave the rail lying.
    if (navTarget && navTarget === active) setNavTarget(null);
  }, [navTarget, active]);
  const [navOpen, setNavOpen] = useState(false);
  // Open by default. A section that starts closed hides the areas inside it from anyone who
  // does not already know they are there, which for a newly-added area is indistinguishable
  // from not having shipped it.
  const [openSections, setOpenSections] = useState<Set<NavSectionId>>(
    () => new Set<NavSectionId>(),
  );
  const toggleSection = (id: NavSectionId) =>
    setOpenSections((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  // IS THE PAGE STILL FILLING UP?
  //
  // This console streams: the server sends HTML as its ~55 feeds answer, so panels appear one
  // at a time and a list can read "12 code tasks" for a second before it reads 200. Watching
  // that with no indicator, the honest conclusion is that projects are missing — which is
  // exactly the conclusion that got drawn.
  //
  // Starts TRUE and is cleared after mount, deliberately: the server renders it as loading, so
  // the streamed HTML carries the bar before any client JS exists to set it. A flag that only
  // became true on the client would miss the entire window it exists for.
  const [settling, setSettling] = useState(true);
  //: How long this load has been going, so the bar is visibly ALIVE rather than a static
  //: sentence that could equally mean "stuck".
  const [settledFor, setSettledFor] = useState(0);
  useEffect(() => {
    // UNTIL THE PAGE HAS ACTUALLY ARRIVED, NOT FOR A FIXED 1.2 SECONDS.
    //
    // Measured on reference host: the Factory page is 9.2 MB and streams for 10.4 seconds. The bar
    // was cleared 1200ms after hydration, which happens early in that stream — so for the
    // remaining ~9 seconds cards kept appearing with nothing on screen saying so. A list that
    // is still arriving looks exactly like a list that is short, and the honest conclusion
    // from a page that has stopped saying it is busy is that the rest is not coming.
    //
    // `load` IS NOT THE END OF THE PAGE FILLING UP. Measured on reference host:
    //
    //     loading fallback   0.3s      (the route skeleton)
    //     content streams    6.8s -> 7.95s
    //     stream ends        7.95s     -> `load` fires -> this bar used to clear
    //
    // and React then hydrates a 6 MB payload, which is when sections visibly appear. So the
    // indicator went off at exactly the moment the expensive part began, and the operator
    // watched projects arrive into a page that had stopped saying it was busy — "not all
    // projects loading in the console, but there's no indicator", reported twice.
    //
    // What the bar claims is "this page is still filling up", so it now MEASURES that: the
    // document being done, AND the content having stopped changing. A MutationObserver over
    // the content is the direct observation; `load` alone was a proxy that stopped being true
    // once a route-level skeleton split the wait in two.
    let done = false;
    const finish = () => { if (!done) { done = true; setSettling(false); } };

    // A floor, so a genuinely fast page does not blink the bar on and straight off.
    const floor = new Promise<void>((r) => window.setTimeout(r, 1200));
    const documentDone = document.readyState === "complete"
      ? Promise.resolve()
      : new Promise<void>((r) => window.addEventListener("load", () => r(), { once: true }));

    // QUIET FOR LONG ENOUGH TO MEAN IT. 700ms of no DOM changes under the content: long enough
    // that a gap between two React commits does not read as finished, short enough that the bar
    // does not linger over a page nobody is waiting for. Resolves immediately where there is no
    // observer to use, rather than holding the bar up for ever.
    const settledDom = new Promise<void>((resolve) => {
      const target = document.querySelector(".bx-content") ?? document.body;
      if (typeof MutationObserver === "undefined" || !target) { resolve(); return; }
      let quiet: number | undefined;
      const arm = () => {
        window.clearTimeout(quiet);
        quiet = window.setTimeout(() => { observer.disconnect(); resolve(); }, 700);
      };
      const observer = new MutationObserver(arm);
      observer.observe(target, { childList: true, subtree: true });
      arm();
    });
    const arrived = Promise.all([documentDone, settledDom]).then(() => undefined);
    // A ceiling, because an indicator that CANNOT go away is the failure the last one had —
    // it became permanent furniture that explained nothing.
    const ceiling = window.setTimeout(finish, 60_000);
    const ticking = window.setInterval(
      () => setSettledFor((n) => n + 1), 1000);

    // AN AREA THAT KNOWS IT HAS PAINTED SAYS SO (independent test D11). The heuristics above
    // are for areas that cannot tell; one that can (the Command Center, once its view is on
    // screen) dispatches PAINTED_EVENT, and the bar ends then — not 1.2s + 700ms of quiet later
    // on a page whose clocks keep the DOM busy.
    const painted = new Promise<void>((r) => window.addEventListener(PAINTED_EVENT, () => r(), { once: true }));
    void Promise.race([Promise.all([floor, arrived]), Promise.all([painted, documentDone])]).then(() => {
      window.clearTimeout(ceiling);
      window.clearInterval(ticking);
      finish();
    });
    return () => {
      done = true;
      window.clearTimeout(ceiling);
      window.clearInterval(ticking);
    };
  }, []);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [live, setLive] = useState(true);
  // Published for the panels that poll on their own (Agents › Live, the presence pill).
  useEffect(() => { setConsoleLive(live); }, [live]);
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
  //  - never poll faster than a refresh actually COSTS. The interval was a fixed 15s while a
  //    render takes 20-27s, so a tab asked for the next one before the last had finished. The
  //    per-tab guard below stops ONE tab stacking, but every open tab then issues a
  //    near-continuous 23s render, and enough of them saturate a single-threaded Node process:
  //    unrelated requests intermittently took 56s, the api healthcheck went unhealthy, and the
  //    console 502'd mid-session. The comment above says this happened once before at 7s; 15s
  //    only moved the threshold. Measuring the cost and backing off removes it.
  const [refreshPending, startRefresh] = useTransition();
  // EVERY REFRESH GOES THROUGH ONE COORDINATOR (lib/console-refresh.ts): at most one in flight,
  // at most one more owed. Refreshes asked for faster than they complete never commit — React
  // renders every pending transition together and waits for the newest — which is how the
  // Discovery area sat on "Loading…" forever on boss-1 while the server rendered it four times.
  // This is the one `router.refresh()` in the console; panels ask via useConsoleRefresh().
  const refreshCoordinator = useMemo(() => createRefreshCoordinator({
    start: () => startRefresh(() => { router.refresh(); }),
  }), [router]);
  useEffect(() => {
    const unregister = registerConsoleRefresh(refreshCoordinator);
    return () => { unregister(); refreshCoordinator.dispose(); };
  }, [refreshCoordinator]);
  // The transition settling IS the refresh landing: React committed what it fetched.
  const refreshWasPending = useRef(false);
  useEffect(() => {
    if (refreshWasPending.current && !refreshPending) refreshCoordinator.landed();
    refreshWasPending.current = refreshPending;
  }, [refreshPending, refreshCoordinator]);
  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    let timer: number | undefined;

    const schedule = () => {
      // At least 20s, and at least three times what the last refresh cost — so a slow console
      // polls itself gently instead of continuously, and a fast one stays responsive. The cost
      // is measured start-to-landing by the coordinator; timing the synchronous call to
      // router.refresh() measured nothing, so this was 20s however slow the console was.
      const delay = Math.min(180_000, Math.max(20_000, refreshCoordinator.lastDurationMs() * 3));
      timer = window.setTimeout(tick, delay);
    };

    const tick = () => {
      if (cancelled) return;
      if (paletteOpen || refreshCoordinator.busy() || document.hidden) { schedule(); return; }
      const tag = document.activeElement?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") { schedule(); return; }
      refreshCoordinator.request("poll");
      schedule();
    };

    schedule();
    return () => { cancelled = true; if (timer) window.clearTimeout(timer); };
  }, [live, paletteOpen, refreshCoordinator]);

  // AND IMMEDIATELY WHEN SOMETHING ACTUALLY CHANGED.
  //
  // The loop above is a poll: it backs off to three times what a render costs, so a change
  // made in the dock or on another screen can sit unshown for up to three minutes. Every
  // server-rendered panel — the governing knobs, spend, assurance, the identity providers —
  // is a snapshot from the last tick, and an operator who has just changed one of those
  // reasonably expects the page to know.
  //
  // The same guards, for the same reasons: never while a render is in flight, never in a
  // hidden tab, never while somebody is typing.
  useEffect(() => onChanged(() => {
    if (document.hidden) return;
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    // Coalesced: asked during a refresh, it runs once that one lands, so the change is shown.
    refreshCoordinator.request("now");
  }, "console-shell"), [refreshCoordinator]);

  // Depend on the query STRING (stable across router.refresh(), changes on real
  // navigation) so the area syncs from the URL on deep links like "View in Work →"
  // without the 7s auto-refresh snapping the user back.
  const queryString = searchParams.toString();
  useEffect(() => {
    // A RETIRED SURFACE IS REWRITTEN BEFORE ANYTHING READS IT. The server redirects these too;
    // this covers the URL changing under a mounted console (Back, a pushState from a panel).
    const legacy = resolveLegacyArea(new URLSearchParams(queryString));
    if (legacy) {
      try {
        window.history.replaceState(null, "", `${window.location.pathname}?${legacy}${window.location.hash}`);
        window.dispatchEvent(new PopStateEvent("popstate"));
      } catch { /* non-browser */ }
    }
    const area = (legacy ?? new URLSearchParams(queryString)).get("area");
    if (area && AREAS.some((a) => a.id === area)) {
      setActive(area as AreaId);
      return;
    }
    const stored = window.localStorage.getItem("boss.area");
    if (stored && AREAS.some((a) => a.id === stored)) {
      setActive(stored as AreaId);
    }
  }, [queryString]);

  // AN AREA OPENED FOR THE FIRST TIME IS FETCHED ONCE, WITH A REAL LOADING STATE.
  //
  // The server renders the requested area plus the ones this session has opened
  // (lib/area-loading.ts). One that is not in the DOM yet is added to the session cookie and
  // the page refreshes; the areas already on screen keep their tree positions, so they update
  // rather than remount. Every later click on it is the pure reveal below.
  const loadedRef = useRef<string[] | undefined>(loadedAreas);
  loadedRef.current = loadedAreas;
  const [areaLoadAsked, setAreaLoadAsked] = useState<{ id: string; at: number } | null>(null);
  const ensureLoaded = useCallback((id: string) => {
    const loaded = loadedRef.current;
    if (!loaded || loaded.includes(id)) return;
    try {
      document.cookie = loadedAreasCookie(
        cookieWith(loadedAreasCookieValue(document.cookie), id));
    } catch {
      /* cookies blocked: the requested area still renders, from the URL */
    }
    setAreaLoadAsked({ id, at: Date.now() });
    // Through the coordinator: if a refresh is in flight (fetched before the cookie named this
    // area), this one is owed and starts the moment it lands — never alongside it.
    refreshCoordinator.request("now");
  }, [refreshCoordinator]);
  const activeLoading = !!loadedAreas && !loadedAreas.includes(active);
  useEffect(() => {
    // Opened by a deep link, Back/Forward or anything else that set `active` without going
    // through go(): the same fetch, once.
    if (activeLoading && areaLoadAsked?.id !== active) ensureLoaded(active);
  }, [activeLoading, active, areaLoadAsked, ensureLoaded]);
  // A REFRESH THAT CAME BACK WITHOUT THE AREA IS ASKED AGAIN AT ONCE.
  //
  // The area-load refresh can settle without the area in it (it raced another refresh in
  // flight), and nothing asked again until the background refresh 20s+ later — so every
  // first click on an area sat on "Loading …" for 10-30 seconds on boss-1 (2026-10-03),
  // while the server answered in under a second. Ask again, a few times, then let the
  // "taking longer than usual" note speak.
  //
  // ONLY ONCE NOTHING IS IN FLIGHT. A retry is a request to the coordinator, so it can never
  // start a refresh alongside one still streaming; and it waits for the console to be idle,
  // because a refresh in flight (or owed) is already going to bring the area.
  const areaRetries = useRef<{ id: string; n: number }>({ id: "", n: 0 });
  useEffect(() => {
    if (!activeLoading || refreshPending || refreshCoordinator.busy()
        || areaLoadAsked?.id !== active) return;
    if (areaRetries.current.id !== active) areaRetries.current = { id: active, n: 0 };
    if (areaRetries.current.n >= 5) return;
    const t = window.setTimeout(() => {
      areaRetries.current.n += 1;
      setAreaLoadAsked(null);  // the effect above sees an area not yet asked for, and asks
    }, 250);
    return () => window.clearTimeout(t);
  }, [activeLoading, refreshPending, refreshCoordinator, areaLoadAsked, active]);
  const [loadSlow, setLoadSlow] = useState(false);
  useEffect(() => {
    setLoadSlow(false);
    if (!activeLoading) return;
    const t = window.setTimeout(() => setLoadSlow(true), 20_000);
    return () => window.clearTimeout(t);
  }, [activeLoading, active]);

  const go = useCallback((id: AreaId) => {
    // A RAIL CLICK TOUCHES THE SERVER ZERO TIMES.
    //
    // Every area is in the DOM and `main.bx-content[data-active=X]` reveals one with CSS, so
    // the section being opened is ALREADY THERE. There is nothing to fetch, nothing to mount
    // and nothing to wait for: flipping `active` is the whole navigation.
    //
    // This used `router.push`, which is a SOFT NAVIGATION — the server re-renders the tree and
    // streams it back. That was right while the page rendered one area per request, because the
    // incoming section genuinely did not exist yet. It is pure cost now: a 6.3 MB, 6-9 second
    // round trip to fetch a screen the browser is already holding. Reported, finally, as "WHEN
    // I CLICK ON THE LEFT PANE NAVIGATION BUTTON IT IS TAKING SEVERAL SECONDS TO LOAD".
    //
    // `history.pushState` keeps the URL honest and Back working without asking the server for
    // anything. The popstate that follows is what tells the panels listening on the URL that
    // the area changed — dispatched here because pushState does not fire one on its own.
    setActive(id);
    setNavTarget(id);
    setNavOpen(false);
    setPaletteOpen(false);
    setQuery("");
    window.localStorage.setItem("boss.area", id);
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("area", id);
      if (url.toString() !== window.location.href) {
        window.history.pushState(null, "", `${url.pathname}${url.search}`);
        window.dispatchEvent(new PopStateEvent("popstate"));
      }
    } catch {
      /* non-browser / unsupported — the area is already set above, so the console still moves */
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
  // THE TRADE THIS BOUGHT WAS NOT WORTH ITS PRICE.
  //
  // "The sidebar switches areas client-side, which is why every area is rendered at once: the
  // trade buys instant tabs." Measured on the live box, that trade cost 8.3 MB of HTML and ~9s
  // of server render on EVERY load — 78% of it an RSC payload carrying forty areas the operator
  // cannot see, all of it parsed and hydrated by their machine. Discovery alone was 949 KB.
  // An operator reported their laptop heating up during a video call; this was why.
  //
  // A full browser navigation was indeed the wrong answer. A Next SOFT navigation is the right
  // one: the server re-renders the tree for the requested area and streams it, the document is
  // never reloaded, and — with page.tsx rendering only the active area — each switch now costs
  // one area instead of forty. Tabs stay fast because the payload got forty times smaller, not
  // because it was all shipped up front.
  //
  // The popstate announcement stays. Panels restore their selection by reading
  // window.location.search (see MeetingRooms), which router.push has already updated by the
  // time this fires, so they behave exactly as they did on Back/Forward.
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
      // An old link (Agent Studio, Team structure, a retired Workflow tab) opens what it meant.
      const legacy = resolveLegacyArea(url.searchParams);
      if (legacy) url.search = `?${legacy}`;
      if (!isInConsoleLink(`?${url.searchParams}`, {
        pathname: url.pathname,
        currentPathname: window.location.pathname,
        target: link.target,
        knownAreas: AREAS.map((a) => a.id),
      })) return;
      const area = url.searchParams.get("area") as AreaId;

      // KEEP WHO YOU ARE, WHATEVER THE LINK SAYS.
      //
      // The console gates on `?org=`: a URL without it renders "Select your organization".
      // Any in-console link written as `?area=…` — and several were — dropped it, so following
      // one mid-session read as being logged out. Restored here, at the single place every one
      // of these links passes through, so a link written without it cannot break the session.
      for (const key of IDENTITY_PARAMS) {
        if (url.searchParams.get(key)) continue;
        const mine = new URLSearchParams(window.location.search).get(key);
        if (mine) url.searchParams.set(key, mine);
      }

      event.preventDefault();
      // Same navigation the rail does, for the same reason: the area being opened is already
      // in the DOM, so this is a reveal, not a fetch. `router.push` here asked the server to
      // re-render all forty areas to show one the browser already had.
      setActive(area as AreaId);
      setNavTarget(area as AreaId);
      window.history.pushState(null, "", `${url.pathname}${url.search}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
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
  // `?agent=` normally carries a RUN id. This sentinel opens the same dock for a
  // conversation that is not about a record — BOSS in general, or a role a door names.
  // Kept as one constant so the shell and the panel cannot drift on the spelling.
  const DOCK_GENERAL = "general";
  const [dockedRun, setDockedRun] = useState<string | null>(null);
  // A PREFILL ONLY THE DOCK CONSUMES. `?ask=` is read by whichever AgentChat sees it first,
  // and the BOSS Agent area is mounted permanently — so a starter clicked in Infra was eaten
  // by an instance nobody was looking at and the dock opened with an empty box. This param is
  // read here and handed down as a prop, so there is exactly one consumer.
  const [dockAsk, setDockAsk] = useState<string>("");
  // WHICH MEMBER OF THE WORKFORCE THE DOCK IS TALKING TO.
  //
  // "Ask" on an agent card navigated to the Agents PAGE — leaving whatever the operator was
  // looking at — to have one question answered. The dock exists for exactly this, and it can
  // carry an identity as easily as a run id.
  const [dockRole, setDockRole] = useState<string>("");
  //: Agent World: who answers BY KEY (the face, the name) and the work the dock is about.
  const [dockRoleKey, setDockRoleKey] = useState<string>("");
  const [dockAct, setDockAct] = useState<string>("");
  //: Bumped when the dock is asked to start fresh. Passed to AgentChat as a key, so React
  //: remounts it with no thread and no history rather than the component having to unpick its
  //: own state — a reset that has to remember every field is a reset that misses one.
  const [dockFresh, setDockFresh] = useState(0);
  //: Whether this dock is for thinking about an idea rather than operating the factory. It
  //: changes who is answering — nobody in particular — as well as how they answer.
  const [dockDiscussing, setDockDiscussing] = useState(false);
  //: WHICH BUMP HAS ALREADY BEEN SPENT ON A MOUNT.
  //:
  //: `fresh={dockDiscussing}` cost an operator a conversation. Discuss mode lives in the URL,
  //: so every later mount — a reload, reopening the dock, a starter question — also asked for
  //: a new thread, and a real discussion about a stock-recommendation site came back as an
  //: empty panel with the idea still in the box. The thread was in the database the whole
  //: time; the console simply refused to open it. Starting fresh belongs to the PRESS, not to
  //: the mode.
  const spentFresh = useRef(0);
  const startFresh = dockDiscussing && dockFresh > spentFresh.current;
  useEffect(() => {
    // Parent effects run after the child's, so the dock has already decided by now.
    if (startFresh) spentFresh.current = dockFresh;
  }, [startFresh, dockFresh]);
  useEffect(() => {
    const read = () => {
      // The run id is the whole handoff now. It goes to the server as `external_ref`, which
      // resolves it into the system prompt — the console no longer ships a context blob.
      const params = new URLSearchParams(window.location.search);
      setDockedRun(params.get("agent"));
      setDockRole(params.get("dock_role") ?? "");
      setDockRoleKey(params.get("dock_role_key") ?? "");
      setDockAct(params.get("dock_act") ?? "");
      setDockDiscussing(params.get("dock_mode") === "discuss");
      // START A NEW CONVERSATION, consumed once. Left in the URL it would reset the thread on
      // every re-render and every back button, wiping a discussion mid-sentence.
      if (params.get("dock_fresh") === "1") {
        setDockFresh((n) => n + 1);
        try {
          const url = new URL(window.location.href);
          url.searchParams.delete("dock_fresh");
          window.history.replaceState(null, "", url.toString());
        } catch { /* non-browser */ }
      }
      const asked = params.get("dock_ask");
      if (asked) {
        setDockAsk(asked);
        try {
          const url = new URL(window.location.href);
          url.searchParams.delete("dock_ask");
          window.history.replaceState(null, "", url.toString());
        } catch { /* non-browser */ }
      }
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
    // AND WHAT IT WAS FOR. Left behind, `dock_mode=discuss` makes the next dock opened for any
    // reason — a run, a role, a question about a deploy — arrive as an idea discussion that
    // refuses to start anything.
    url.searchParams.delete("dock_mode");
    window.history.pushState({}, "", url.toString());
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, []);

  // ANNOUNCE THE URL ONCE IT IS TRUE, NOT WHEN IT WAS ASKED FOR.
  //
  // This cost ~30 seconds per click when the rail used `router.push`: announcing the url on the
  // next line told ELEVEN components it had changed while `window.location.search` still held
  // the old area, so every one re-read a stale url and refetched, the navigation committed, the
  // area remounted, and they all fetched again.
  //
  // The rail no longer pushes — it uses pushState and announces once, itself, with the url
  // already correct. This effect still covers Back/Forward, where `useSearchParams` updates
  // without anyone announcing it, which is the case every listener was written against.
  const announcedSearch = useRef<string | null>(null);
  useEffect(() => {
    const current = searchParams?.toString() ?? "";
    if (announcedSearch.current === null) {
      // The first render is not a navigation; announcing it would make every panel refetch on
      // arrival, which is the thing the server render just did for them.
      announcedSearch.current = current;
      return;
    }
    if (announcedSearch.current === current) return;
    announcedSearch.current = current;
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, [searchParams]);

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
    () => areasForEdition.filter(
      (a) => isCommunityPrimaryNavVisible(a.id, resolvedEdition) && !OFF_THE_NAV.has(a.id)),
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

  // Repo and models' badge: the server's on first paint, then whatever the board last saw.
  const [setupBadge, setSetupBadge] = useState(status.setup ?? null);
  useEffect(() => { setSetupBadge(status.setup ?? null); }, [status.setup]);
  useEffect(() => {
    const onBadge = (event: Event) => setSetupBadge((event as CustomEvent).detail ?? null);
    window.addEventListener(SETUP_BADGE_EVENT, onBadge);
    return () => window.removeEventListener(SETUP_BADGE_EVENT, onBadge);
  }, []);
  const badgeFor = (id: AreaId): number | undefined => {
    if (id === "decisions" && status.pendingDecisions > 0) return status.pendingDecisions;
    if (id === "ops" && status.deadletters > 0) return status.deadletters;
    if (id === "setup" && setupBadge && setupBadge.count > 0) return setupBadge.count;
    return undefined;
  };

  const labelFor = (area: AreaDef): string => area.label;

  // A DOCK, NOT A DISAPPEARANCE. Collapsing to icons keeps every destination one click away;
  // hiding the sidebar entirely trades screen width for having to remember what was in it.
  // Remembered per browser, because a width preference that resets every visit is not a
  // preference.
  const [docked, setDocked] = useState(false);
  useEffect(() => {
    try { setDocked(localStorage.getItem("boss.nav.docked") === "1"); } catch { /* private mode */ }
  }, []);
  const toggleDock = () => {
    setDocked((wasDocked) => {
      const next = !wasDocked;
      try { localStorage.setItem("boss.nav.docked", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };

  return (
    <div className={`bx-shell${navOpen ? " nav-open" : ""}${docked ? " nav-docked" : ""}`}>
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
          <button
            type="button"
            className="bx-dock-toggle"
            data-testid="nav-dock-toggle"
            aria-pressed={docked}
            aria-label={docked ? "Expand the sidebar" : "Collapse the sidebar to icons"}
            title={docked ? "Expand the sidebar" : "Collapse to icons"}
            onClick={toggleDock}
          >
            <CaretRight size={14} weight="bold" aria-hidden />
          </button>
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
              {/* A SECTION RENDERS WHERE ITS FIRST AREA WOULD HAVE.
                  Not at the top of the group and not in a list of its own: the order operators
                  learned stays put, and the two areas simply move one level in underneath a
                  heading. Anything without a parent renders exactly as before. */}
              {navAreas.filter((a) => a.group === group).map((a) => {
                if (a.parent) {
                  const section = NAV_SECTIONS[a.parent];
                  const siblings = navAreas.filter((x) => x.parent === a.parent);
                  // Only the FIRST child draws the section; the rest are drawn by it.
                  if (siblings[0]?.id !== a.id) return null;
                  const open = openSections.has(a.parent);
                  const holdsActive = siblings.some((x) => x.id === active);
                  const SectionIcon = section.icon;
                  return (
                    <div key={`section-${a.parent}`} className="bx-nav-section"
                         data-open={open || undefined}
                         data-testid={`nav-section-${a.parent}`}>
                      <button
                        type="button"
                        className="bx-nav-item bx-nav-parent"
                        aria-expanded={open}
                        // A section has no screen, so it must not claim to be the current page
                        // even when one of its children is.
                        data-holds-active={holdsActive || undefined}
                        onClick={() => toggleSection(a.parent!)}
                      >
                        <SectionIcon size={17} weight={holdsActive ? "fill" : "regular"} aria-hidden />
                        <span>{section.label}</span>
                        <CaretRight className="bx-nav-caret" size={12} weight="bold" aria-hidden />
                      </button>
                      {/* COLLAPSED STILL SHOWS WHERE YOU ARE.
                          Hiding the current page inside a closed section is the "where am I"
                          bug: the nav stops answering the one question it exists for. So a
                          closed section keeps the active child and drops the rest — the
                          control still works, and you never lose your place. */}
                      {open || holdsActive ? (
                        <div className="bx-nav-children" data-collapsed={!open || undefined}>
                          {(open ? siblings : siblings.filter((x) => x.id === active)).map((child) => {
                            const ChildIcon = child.icon;
                            const childBadge = badgeFor(child.id);
                            return (
                              <button
                                key={child.id}
                                type="button"
                                className={`bx-nav-item bx-nav-child${child.id === selected ? " active" : ""}`}
                                aria-current={child.id === selected ? "page" : undefined}
                                data-area-nav={child.id}
                                onClick={() => go(child.id)}
                              >
                                <ChildIcon size={15}
                                            weight={child.id === selected ? "fill" : "regular"} aria-hidden />
                                <span>{labelFor(child)}</span>
                                {childBadge ? <span className="bx-nav-badge">{childBadge}</span> : null}
                              </button>
                            );
                          })}
                        </div>
                      ) : null}
                    </div>
                  );
                }
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
                    className={`bx-nav-item${a.id === selected ? " active" : ""}`}
                    aria-current={a.id === selected ? "page" : undefined}
                    data-area-nav={a.id}
                    // The label survives docking as a tooltip — icons alone are a memory test.
                    data-label={labelFor(a)}
                    title={a.sub}
                    data-paid-badge={paidBadge ? "true" : undefined}
                    data-sso-visible={a.id === "enterprise" ? "true" : undefined}
                    onClick={() => go(a.id)}
                  >
                    <Icon size={17} weight={a.id === selected ? "fill" : "regular"} aria-hidden />
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
                    {badge ? (
                      <span className="bx-nav-badge" data-testid={`nav-badge-${a.id}`}
                            data-tone={a.id === "setup" ? setupBadge?.tone : undefined}>
                        {badge}
                      </span>
                    ) : null}
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
          {status.controlPlane ? (
            <div className="bx-control-host" title={status.controlPlane}
                 data-local={isLocalPlane(status.controlPlane) || undefined}
                 data-testid="control-plane-host">
              {planeLabel(status.controlPlane)}
            </div>
          ) : null}
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

          {/* START A BUILD FROM ANYWHERE (factory spec §4.1, operator decision 2026-10-04).
              The same first press as the Factory's `+ New build`: Plan it with Juno — a PRD
              first, then the build. The Factory's menu keeps the other doors (a workflow, a
              direct change, a benchmark); one primary press in both places. */}
          {/* ONE PRODUCT (walk D19): on the Factory its own header holds `+ New build`. */}
          {active !== "code_tasks" ? <button
            type="button"
            className="bx-new-project"
            data-testid="topbar-new-project"
            title="Plan it with Juno: a PRD first, then the build"
            onClick={() => openPlan("pm", { from: "palette" })}
          >
            <span aria-hidden>+</span> New build
          </button> : null}

          <div className="bx-topbar-title" id="boss-console-title">
            {activeDef.label}
            <small>{activeDef.sub}</small>
          </div>

          <div className="bx-topbar-spacer" />

          {org ? (
            <button type="button" className="bx-org-chip" onClick={switchOrg} title="Switch organization">
              <span className="bx-org-name">{org.name}</span>
              <span className="bx-org-switch" aria-hidden>⌄</span>
            </button>
          ) : null}

          <button
            type="button"
            className="bx-live-status"
            title={live
              ? (refreshPending
                 ? "Fetching the latest data — click to pause live updates"
                 : "Live: auto-refreshing — click to pause")
              : "Paused — click to resume live updates"}
            onClick={() => setLive((v) => !v)}
            style={{ cursor: "pointer" }}
          >
            {/* A refresh in flight shows HERE: same chip, no layout shift, gone when it
                lands. Enough to answer "is it updating?" without occupying the page. */}
            <span className={live ? "dot green bx-pulse" : "dot"} />
            {live ? (refreshPending ? "Syncing" : "Live") : "Paused"}
          </button>

          <button type="button" className="bx-kbd" onClick={() => setPaletteOpen(true)}>
            <span>Jump to</span>
            <kbd>⌘K</kbd>
          </button>
          <PresencePill projectId={scope.projectId ?? scope.project} />
          <details className="bx-system-menu">
            <summary aria-label="System details">System</summary>
            <div className="bx-system-panel">
              <div><span>Edition</span><EditionChip editionId={resolvedEdition} /></div>
              <div><span>Budget</span><strong className={`bx-system-budget ${tone}`}>
                {status.budgetState || "unknown"}</strong></div>
              <div><span>Operator</span><strong><UserCircle size={15} aria-hidden /> {scope.actor}</strong></div>
              <div><span>Console</span><ConsoleToggle current="v1" orgId={org?.id} /></div>
              {/* SIGN OUT. /api/auth/logout existed and nothing called it, so the only way to
                  end a console session on a shared machine was to clear the browser's cookies.
                  Shown only when sign-in is configured: there is no session to end otherwise. */}
              {signOutAvailable ? (
                <form action="/api/auth/logout" method="post" data-testid="bx-sign-out">
                  <button type="submit" className="cap-btn">Sign out</button>
                </form>
              ) : null}
            </div>
          </details>
        </header>

        {/* EVERY PROJECT, WITHOUT LEAVING THE AREA YOU ARE IN.
            v1 made you open Factory and scroll 74 cards to learn what was running. This is v2's
            strip, in v1's header: status, stage and progress per project, filterable, and one
            click into the project itself. It fetches on its own through a route handler, so a
            slow or missing overview delays this strip and nothing else on the page. */}
        {/* A thin bar rather than a spinner over the content: the panels that HAVE arrived are
            usable while the rest land, and covering them would make a fast console feel slower
            than a slow one. */}
        {/* THE FIRST PAINT ONLY. NOT THE BACKGROUND REFRESH.
            `refreshPending` was here too, and it is true for the whole server re-render — 20
            seconds and more on this console — against an interval computed from that same
            cost. The result was a bar that never went off: an indicator meant to explain why
            the page was still filling up became a permanent piece of furniture that explained
            nothing and could not be dismissed.
            A background refresh is background. The data is already on screen, so it raises
            none of the questions the first load does; it belongs in the Live chip, which is
            already there and does not move anything. */}
        {/* A REFRESH IN FLIGHT, AT THE TOP EDGE, WHERE IT CANNOT BE MISSED.
            The Syncing chip already existed and is easy to miss: it is one small pill among
            six in a busy header, and it carries `hide-sm` so on a narrow window it is not
            there at all. "I don't see any indicator" was a fair reading of a screen that was
            quietly re-fetching fifty feeds.
            NOT the sticky strip below — that one WAS wired to refreshPending once and became
            permanent furniture, because a refresh takes about as long as the gap between
            refreshes. A two-pixel line at the very edge cannot become furniture: it occupies
            no layout, moves nothing when it appears, and is unmistakably progress rather than
            content. */}
        {refreshPending ? (
          <div className="bx-refresh-line" role="status" aria-live="polite"
               aria-label="Refreshing data" data-testid="console-refreshing" />
        ) : null}

        {settling ? (
          <div className="bx-loading" role="status" aria-live="polite"
               data-testid="console-loading">
            <span className="bx-loading-bar" aria-hidden />
            <span className="bx-loading-text">
              Loading the console — panels fill in as they arrive
              {/* The elapsed count is the difference between "it is working" and "it has
                  stopped". Only after a few seconds: on a fast load it would be noise. */}
              {settledFor >= 3 ? (
                <span className="bx-loading-elapsed" data-testid="console-loading-elapsed">
                  {settledFor}s
                </span>
              ) : null}
            </span>
          </div>
        ) : null}

        {/* The Factory's Floor IS the list of builds (walk D19): no second strip of them. */}
        {active !== "code_tasks" ? <ProjectStrip projectId={scope.projectId ?? scope.project} area={active} /> : null}

        {/* The console gets out of the panel's way rather than hiding behind it. The dock is
            fixed-position, so without this inset the right-hand column of cards sat underneath
            it, unreachable. */}
        {/* The area being left stays on screen while the next one is fetched, so without this
            a slow area looks like a dead click on the old screen. A hairline across the top is
            the smallest honest signal that the press was taken and work is in flight. */}
        <style data-testid="bx-area-visibility" dangerouslySetInnerHTML={{ __html: AREA_VISIBILITY_CSS }} />
        <main className="bx-content" data-testid="console-area" data-active={active} aria-label={activeDef.label}
              data-dock={dockedRun ? "open" : undefined}
              style={dockedRun ? { paddingRight: dockWidth + 16 } : undefined}>
          {activeLoading ? (
            <div className="area-loading" role="status" aria-live="polite"
                 data-testid="area-loading">
              <div className="area-loading-bar" aria-hidden />
              <span>Loading {activeDef.label}…</span>
              {loadSlow ? (
                <span>
                  This is taking longer than usual — the control plane may be busy.{" "}
                  <button type="button" className="linkish"
                          onClick={() => window.location.reload()}>
                    Reload the page
                  </button>
                </span>
              ) : null}
            </div>
          ) : null}
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
              {/* WHO IS ANSWERING. One meaning for "no role": BOSS in general (dockSpeaker in
                  lib/dock-subject.ts). This head used to call the no-subject, no-role dock the
                  Deployment Manager, so Repo and model's "Ask BOSS about this step" went to the
                  role whose subject is hosts and rollbacks. */}
              <span className="eyebrow" data-testid="bx-dock-who">
                {dockSpeaker({ role: dockRole, discussing: dockDiscussing }).heading}
              </span>
              {dockDiscussing
                ? <span className="detail">
                    an idea or the running system &mdash; nothing starts unless you say so
                  </span>
                : dockRole
                ? <span className="detail">answering from this role&rsquo;s own work here</span>
                : dockedRun === DOCK_GENERAL
                  ? <span className="detail">BOSS in general &mdash; across the whole factory</span>
                  : <code className="detail">{dockedRun}</code>}
              {/* Icon, not a labelled button. The head is a tight strip already holding the
                  title and the run id, and a text "Close" competed with both while doing the
                  most ordinary thing in the panel. aria-label keeps it announced to screen
                  readers, and title keeps it discoverable on hover. */}
              <button type="button" className="cap-btn bx-dock-close" onClick={closeDock}
                      aria-label="Close the Ask BOSS panel" title="Close">
                <X size={14} weight="bold" aria-hidden />
              </button>
            </div>
            <div className="bx-dock-body">
              {/* The sentinel is not a task: passing it through would send AgentChat
                  looking for a run that does not exist. */}
              <AgentChat projectId={scope.projectId ?? scope.project}
                         actor={scope.actorId ?? scope.actor}
                         initialTaskId={dockedRun === DOCK_GENERAL ? undefined : dockedRun}
                         initialMessage={dockAsk || undefined}
                         // NOBODY IN PARTICULAR, WHEN DISCUSSING AN IDEA.
                         //
                         // The general dock used to default to the Deployment Manager. It made
                         // "Discuss it" open under "Continuing with the Deployment Manager",
                         // resuming that role's week-old thread about probe latencies with the
                         // new idea typed into it. The heading was the visible half; the real
                         // damage is that the thread's history goes into the model's context.
                         roleName={dockDiscussing
                           ? undefined
                           : dockSpeaker({ role: dockRole, discussing: false }).roleName}
                         // `dockFresh` in the key so starting a discussion remounts with no
                         // thread and no history, rather than the component unpicking its own
                         // state — a reset that has to remember every field misses one.
                         // And the SUBJECT: see dockChatKey — without it a second Ask door with
                         // the same question sent its turn about the previous subject.
                         fresh={startFresh}
                         roleKey={dockDiscussing ? undefined : dockRoleKey || undefined}
                         actId={dockAct || undefined}
                         key={dockChatKey({ agent: dockedRun, ask: dockAsk, role: dockRole,
                                            discussing: dockDiscussing,
                                            roleKey: dockRoleKey, actId: dockAct, fresh: dockFresh })} />
            </div>
          </aside>
        ) : null}
      </div>

      <ConverseHost projectId={scope.projectId ?? scope.project} actor={scope.actorId ?? scope.actor} />
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
              {/* TALKING WITH AGENTS: talk 1:1, plan a start, open a room (lib/agent-converse). */}
              {CONVERSE_COMMANDS.filter((c) => !query.trim()
                || c.label.toLowerCase().includes(query.trim().toLowerCase())).map((c) => (
                <button key={c.key} type="button" className="bx-palette-item"
                        data-testid={`palette-converse-${c.key}`}
                        onClick={() => { setPaletteOpen(false); c.run(); }}>
                  <UsersThree size={17} aria-hidden />
                  <span>
                    <span style={{ fontWeight: 600 }}>{c.label}</span>
                    <span style={{ display: "block", fontSize: "0.78rem", color: "var(--muted)" }}>{c.sub}</span>
                  </span>
                </button>
              ))}
              {filtered.length === 0 && !CONVERSE_COMMANDS.some((c) => c.label.toLowerCase()
                .includes(query.trim().toLowerCase())) ? (
                <div style={{ padding: "14px 12px", color: "var(--muted)" }}>No matching area.</div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
