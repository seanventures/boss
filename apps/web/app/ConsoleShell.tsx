"use client";

import { GatedButton } from "../components/gated";
import { IDENTITY_PARAMS } from "./url-intent";
import { rememberOrg, withOrgRestored } from "../lib/console-org";
import { isInConsoleLink } from "./console-href";
import { consoleParams, resolveLegacyArea } from "./legacy-areas";
import { GET_STARTED_EVENT, SETUP_BADGE_EVENT } from "../lib/setup-stepper";
import { onChanged } from "./changed";
import { createRefreshCoordinator, registerConsoleRefresh, type RefreshCoordinator } from "../lib/console-refresh";
import {
  cookieWith,
  loadedAreasCookie,
  loadedAreasCookieValue,
} from "../lib/area-loading";
import { ActiveAreaContext } from "./area-frame";
import { ConsoleToggle } from "../components/v2/ConsoleToggle";
import { AgentChat } from "./agent-ui";
import { dockChatKey, dockSpeaker } from "../lib/dock-subject";
import { dockWidthFor, railForDock } from "../lib/shell-rail";
import { ProjectStrip } from "./project-strip";
import { STRIP_NOTICE_SLOT } from "./strip-notice";
import { useFullPage } from "./workflows/full-page";
import { ProjectMenu, type ProjectMenuItem } from "./project-menu";
import { useDetailsDismiss } from "../lib/details-dismiss";
import { PresencePill } from "./agents/presence-pill";
import { NEW_BUILD_TIP, openNewProject } from "../lib/compose-links";
import { setConsoleLive } from "../lib/console-live";
import { PAINTED_EVENT } from "../lib/area-painted";
import { useAttention } from "../lib/agents-live";
import { useTitleCount } from "../lib/tab-title";
import { ConverseHost } from "./agents/converse-host";
import { useLoadIdentities } from "../lib/use-agent-identities";
import { CONVERSE_COMMANDS } from "../lib/agent-converse";
import { ProjectsNav } from "./projects/nav";
import { JumpResults } from "./projects/jump";
import { ShellCrumbs } from "./shell-crumbs";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
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
  HardDrives,
  ShieldCheck,
  House,
  List,
  MagnifyingGlass,
  CaretRight,
  Folders,
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
  Users,
  UsersThree,
  Plus,
  Wallet,
  Wrench,
  Bandaids,
  SignOut,
  SealCheck,
  PuzzlePiece,
  Scales,
  IdentificationCard,
  ShareNetwork,
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
import { accessForArea } from "../lib/identity/area-access";
import type { MePermissionsResponse, MeResponse } from "../lib/identity/contract";
import { IdentityProvider, useIdentity } from "../components/identity/identity-provider";
import { allRoleLabels, primaryRoleLabel } from "../lib/identity/role-label";
import { AccessDenied } from "../components/identity/access-denied";
import { AccountMenu, IdentityBanners, signOut } from "../components/identity/account-menu";
import { ConsentHost } from "../components/identity/consent-sheet";
import { Face } from "../components/identity/kit";
import { avatarTone } from "../lib/identity/avatar";
import { shellHeading, shownArea } from "../lib/shell-heading";

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
  self_fixes: Bandaids,
  agent_ops: ShareNetwork,
  work: Rows,
  activity: List,
  portfolio: SlidersHorizontal,
  budget: Wallet,
  pipeline: ArrowsClockwise,
  comms: ChatsCircle,
  github: GitBranch,
  infra: HardDrives,
  observability: Heartbeat,
  knowledge: Graph,
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
/** The Project page (BOSSAPP-914) is reached from Projects (its rail list), never as a row of its own. */
const UNDER_PROJECTS = new Set<AreaId>(["project"]);
/** Get started has its own rail button above the groups, shown only while setup is unfinished. */
const OWN_RAIL_BUTTON = new Set<AreaId>(["welcome"]);

const AREAS: AreaDef[] = [
  { id: "overview", label: "Command Center", sub: "What needs you, across every project", group: "Platform", icon: SquaresFour },
  // THE ORDER IS THE ARGUMENT. The Run group opens with the place work is DONE and the path it
  // follows — Factory, then the workflow governing it, then who does it and who approves it,
  // then where it ships and whether it is alive. Discovery and Meetings are ways IN and follow
  // after; they were first only because they come first chronologically, which is not the same
  // as being what an operator opens the console to look at.
  // PROJECTS FIRST (BOSSAPP-914): one home per project — its work, repo, deploys and health —
  // with the projects listed under it in the rail. First run lives here too (Get started's steps).
  { id: "projects", label: "Projects", sub: "Every project: its work, crew, repo and deploys", group: "Run", icon: Folders },
  { id: "project", label: "Project", sub: "One project's work, repo, deploys and health", group: "Run", icon: Folders },
  // CODE TASKS, was Factory (BOSSAPP-914): the same area id, tabs and links; only the name changed.
  { id: "code_tasks", label: "Code Tasks", sub: "Builds, apps and gated PRs (was Factory)", group: "Run", icon: ICONS.code_tasks },
  // AGENTS AT WORK (autonomous agents, design §11.3): what agents noticed and resolved on their
  // own, each as a flow chart. Beside the Factory: the other place work is done.
  { id: "agent_ops", label: "Agents at work", sub: "What agents resolved on their own", group: "Run", icon: Robot },
  // ONE HOME PER OBJECT. Workflow, Agent work, Team structure and Agent Studio were four rows
  // for two things: workflows (built, run and watched in one area) and the agents that work
  // them (roster, team and panels in one area). Old links to the retired two still land —
  // app/legacy-areas.ts.
  { id: "delivery_workflows", label: "Workflows", sub: "Build, run and watch workflows", group: "Run", icon: ICONS.delivery_workflows },
  { id: "workforce", label: "Agent Studio", sub: "Who does the work", group: "Run", icon: UsersThree },
  // THE GRAPH (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §5.1): after who does the work, what they work
  // ON and what they learned doing it. Scoped to the active project like every area.
  // NAMED FOR WHAT IT IS (operator, 2026-10-24): "Graph" told nobody it was a knowledge graph.
  { id: "knowledge", label: "Knowledge Graph", sub: "Code, rules and what agents learned", group: "Run", icon: ICONS.knowledge },
  { id: "decisions", label: "Approvals", sub: "Explicit approvals before real effects", group: "Run", icon: SealCheck },
  // TWO SCREENS, TWO QUESTIONS (infra-observability spec §0, §4.1). Infra is where it runs and
  // how it got there; Observability is whether it is alive, how fast, and what thinking costs.
  // The ids say so now: `delivery` (labelled Infra) and `monitoring` (labelled Observability)
  // redirect here (app/legacy-areas.ts), and System Configuration's contents moved into Infra ›
  // Cloud, Repo and models, Skills & MCP and Observability, so it has no rail entry.
  { id: "infra", label: "Infra", sub: "Where your apps run", group: "Run", icon: ICONS.infra },
  { id: "observability", label: "Observability", sub: "Is it alive, how fast, what it costs", group: "Run", icon: Pulse },
  { id: "activity", label: "Activity log", sub: "Every line BOSS wrote, searchable", group: "Run", icon: ICONS.activity },
  // BOSS FIXES ITSELF (docs/product/BOSS_FIXES_ITSELF.md §10.3): every fix BOSS made to its own
  // code, from the problem to the release, and the autonomy it bought. After the log: what BOSS
  // did about what went wrong.
  { id: "self_fixes", label: "Self-fixes", sub: "Bugs in BOSS that BOSS fixed itself", group: "Run", icon: Wrench },
  { id: "discovery", label: "Research", sub: "Cited dossiers — a verdict, outcomes and the evidence", group: "Run", icon: ICONS.discovery },
  { id: "meetings", label: "Panels", sub: "Agents debate it — a decision or a PRD", group: "Run", icon: ICONS.meetings },
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
  // GET STARTED (BOSSAPP-808): the 3-step Welcome — its own place, never Repo and models' body.
  { id: "welcome", label: "Get started", sub: "Get BOSS working in 3 steps", group: "Settings", icon: Rocket },
  // WHAT YOUR CREW CAN DO (skills-mcp spec §5.1): the Kit of abilities, who holds them, and every
  // skill and MCP server. Second in Settings: a repo and a model come first.
  { id: "skills", label: "Skills & MCP", sub: "What your crew can do", group: "Settings", icon: PuzzlePiece },
  { id: "budget", label: "Govern", sub: "Spend caps, pace and limits", group: "Settings", icon: Scales },
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
    sub: "People, roles, agents and sign-in",
    group: "Settings",
    icon: IdentificationCard,
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

/** An address only the console's own network can resolve ("api:8000", the compose service): it
 *  names no environment a person could tell apart, so the rail shows nothing rather than it. */
export function isInternalPlane(url: string): boolean {
  try {
    const host = new URL(url).hostname;
    return !host.includes(".") && !host.includes(":") && host !== "localhost" && !host.startsWith("[");
  } catch {
    return false;
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
    /** Setup is unfinished: the rail shows Get started (Setup's Welcome) above the areas. */
    getStarted?: boolean;
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
  /** The org's projects the viewer can open, and which one is the org's own (no ?project_id=):
   *  the top bar's project menu (switch, New project). Absent on route pages. */
  projects?: { items: ProjectMenuItem[]; homeId: string };
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
  /** A value the server makes fresh on EVERY render (page.tsx). Its change is how the shell
   *  knows a refresh has landed: React's transition flag is not (see refreshLanded below). */
  renderToken?: string;
  /** Who is signed in and what they hold, read on the server (lib/identity/server.ts), so the
   *  rail and every control mirror the API from the first paint. */
  identity?: {
    me: MeResponse | null;
    org: MePermissionsResponse | null;
    project?: MePermissionsResponse | null;
    stub?: boolean;
    unavailable?: boolean;
  };
  /** The shell around a ROUTE (/identity, /account) rather than the area page: a rail press
   *  navigates to the area instead of revealing a section already in the DOM. */
  routeMode?: boolean;
  /** The top bar's title on a route page with no rail area of its own (My account). */
  title?: { label: string; sub: string };
  children: ReactNode;
};

/** IDENTITY AREAS LIVE ON THEIR OWN ROUTES (/identity/**), not as a section of this page. */
const ROUTE_AREAS: Partial<Record<AreaId, string>> = { enterprise: "/identity" };

/** The palette's Enter: press the first choice the list shows — an area, New build, or a
 *  conversation — exactly as a click would. False when the list shows nothing. */
export function activatePaletteChoice(list: HTMLElement | null): boolean {
  const first = list?.querySelector<HTMLButtonElement>(".bx-palette-item");
  if (!first) return false;
  first.click();
  return true;
}

export default function ConsoleShell(props: ConsoleShellProps) {
  const { identity, scope } = props;
  return (
    <IdentityProvider
      me={identity?.me ?? null}
      org={identity?.org ?? null}
      project={identity?.project ?? null}
      projectId={scope.projectId ?? null}
      stub={identity?.stub ?? false}
      unavailable={identity ? (identity.unavailable ?? false) : true}
    >
      <ConsoleShellInner {...props} />
      <ConsentHost />
    </IdentityProvider>
  );
}

/** Whether a console refresh is in flight, re-rendering only its own children (see the
 *  coordinator comment in ConsoleShellInner). `fallback` is the transition flag on a route page. */
function RefreshState({ coordinator, fallback, children }: {
  coordinator: RefreshCoordinator; fallback: boolean; children: (refreshing: boolean) => React.ReactNode;
}) {
  const busy = useSyncExternalStore(coordinator.subscribe, coordinator.busy, () => false);
  return <>{children(busy || fallback)}</>;
}

/** How long the server took to send the document this console loaded with (0 = unknown). */
function documentServerMs(): number {
  try {
    const nav = performance.getEntriesByType?.("navigation")?.[0] as PerformanceNavigationTiming | undefined;
    const ms = nav ? nav.responseEnd - nav.requestStart : 0;
    return Number.isFinite(ms) && ms > 0 ? ms : 0;
  } catch {
    return 0;
  }
}

function ConsoleShellInner({
  status,
  scope,
  org,
  projects,
  editionId,
  initialArea,
  signOutAvailable = false,
  loadedAreas,
  renderToken,
  routeMode = false,
  title,
  children,
}: ConsoleShellProps) {
  const { check, me } = useIdentity();
  /** Can the viewer open this area? S2's declaration, else the draft (lib/identity/area-access). */
  const areaCan = useCallback((id: AreaId) => {
    const access = accessForArea(id);
    return "permission" in access ? check(access.permission) : { allowed: true, reason: "", needsStepUp: false };
  }, [check]);
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
  // A route page with no rail area (My account) highlights nothing in the rail.
  const railSelected: AreaId | null = routeMode && !(AREAS.some((a) => a.id === initialArea)) ? null : selected;
  useEffect(() => {
    // The transition committed (or the URL changed underneath us). Stop showing a target that
    // is now simply the active area, so a later failure cannot leave the rail lying.
    if (navTarget && navTarget === active) setNavTarget(null);
  }, [navTarget, active]);
  const [navOpen, setNavOpen] = useState(false);
  // The rail's collapse, for the ⌘\ shortcut (set below, where the toggle is defined).
  const toggleDockRef = useRef<() => void>(() => undefined);
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
  const paletteList = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryString = searchParams.toString();
  const loadedRef = useRef<string[] | undefined>(loadedAreas);
  loadedRef.current = loadedAreas;
  const [areaLoadAsked, setAreaLoadAsked] = useState<{ id: string; at: number } | null>(null);

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
  //
  // A REFRESH HAS LANDED WHEN THE SERVER'S NEW RENDER IS ON SCREEN — NOT WHEN `useTransition`
  // SAYS SO. router.refresh() dispatches into Next's own action queue, so the transition around it
  // settles within a frame (measured: "Syncing" on for 15 ms of a 900 ms refresh). The shell took
  // that as landed, its area-load retry 250 ms later started a SECOND router.refresh while the
  // first was still streaming, and Next 15 dropped the first and left the tree suspended on it —
  // so the area sat on "Loading Factory…" until the next background poll, 20 s later. Measured on
  // the laptop install, 2026-10-10: 7 of 9 first clicks on an area after a page load took
  // 20.1-20.2 s; the server answered each in under a second.
  //
  // So the page renders a fresh `renderToken` every time, and its change is the landing. A
  // shell without one (a route page) keeps the transition flag.
  //
  // AND THE SHELL DOES NOT RE-RENDER WHEN ONE STARTS. useTransition's pending flag (and a
  // "syncing" state after it) re-rendered this whole shell — every mounted area — in the same
  // tick Next dispatched the refresh, and a refresh that answered while that render was still
  // running was the one Next never completed: rail clicks from a heavy screen (Command Center,
  // Decisions) to a cheap area (Activity, Govern) hung 4 times in 4, from a light one 0 in 4.
  // So the refresh is dispatched bare, and the Syncing chip and the edge line read the
  // coordinator through their own small subscriber (<RefreshState>).
  const tokenDriven = renderToken !== undefined;
  const refreshCoordinator = useMemo(() => createRefreshCoordinator({
    start: tokenDriven
      ? () => { router.refresh(); }
      : () => startRefresh(() => { router.refresh(); }),
    // A refresh Next never completed (lib/console-refresh.ts): a navigation to the same URL
    // drops it from Next's queue, and the refresh after it brings what was asked for.
    unstick: () => {
      router.replace(`${window.location.pathname}${window.location.search}${window.location.hash}`,
                     { scroll: false });
      router.refresh();
    },
    // Long enough for a slow render under load; short enough that a refresh Next discarded (a
    // navigation raced it) does not stop the console refreshing for minutes.
    ceilingMs: 30_000,
    // What this server is measured to take, so the watchdog never restarts a refresh that is
    // only slow (the unstick is for one Next never completes).
    serverMs: documentServerMs(),
  }), [router, tokenDriven]);
  // A URL RESTORE DISCARDS THE REFRESH IN FLIGHT (rc9). Next prioritizes a URL restore over a
  // pending refresh and discards its response. That response cannot acknowledge our
  // coordinator, even if the server finishes it. Release its logical bookkeeping only after the
  // new URL commits, then load the current area. Previously loaded areas still need the
  // discarded read replaced. Declared BEFORE the landing effects so a render that commits with
  // the new URL is not taken as the canceled refresh landing. (`unstick` replaces the SAME URL,
  // so it never trips this.)
  const committedQuery = useRef(queryString);
  const canceledInThisCommit = useRef(false);
  useEffect(() => {
    if (committedQuery.current === queryString) return;
    committedQuery.current = queryString;
    if (!refreshCoordinator.busy()) return;
    refreshCoordinator.cancel();
    canceledInThisCommit.current = true;
    setAreaLoadAsked(null);
    const requested = new URLSearchParams(queryString).get("area");
    if (requested && loadedRef.current?.includes(requested)) refreshCoordinator.request("now");
  }, [queryString, refreshCoordinator]);
  const lastToken = useRef(renderToken);
  useEffect(() => {
    if (!tokenDriven) return;
    const canceled = canceledInThisCommit.current;
    canceledInThisCommit.current = false;
    if (lastToken.current === renderToken) return;
    lastToken.current = renderToken;
    if (!canceled) refreshCoordinator.landed();
  }, [renderToken, refreshCoordinator, tokenDriven, queryString]);
  useEffect(() => {
    const unregister = registerConsoleRefresh(refreshCoordinator);
    return () => { unregister(); refreshCoordinator.dispose(); };
  }, [refreshCoordinator]);
  // A shell without a render token (a route page): newly committed server props acknowledge the
  // refresh (rc9). Dispatching router.refresh() can finish the transition before Next delivers
  // its response, so a transition ending is not an acknowledgment. Both objects come from the
  // page's server render; local shell state changes keep their references.
  const serverSnapshot = useRef({ status, loadedAreas });
  useEffect(() => {
    if (tokenDriven) return;
    const canceled = canceledInThisCommit.current;
    canceledInThisCommit.current = false;
    const previous = serverSnapshot.current;
    if (previous.status === status && previous.loadedAreas === loadedAreas) return;
    serverSnapshot.current = { status, loadedAreas };
    if (!canceled) refreshCoordinator.landed();
  }, [status, loadedAreas, refreshCoordinator, queryString, tokenDriven]);
  // Only for a shell without a render token (a route page): there the transition flag stands in
  // for the Syncing chip.
  const refreshing = tokenDriven ? false : refreshPending;
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

  // THE ORG THIS CONSOLE IS OPEN IN IS REMEMBERED, AND THE ADDRESS BAR KEEPS IT.
  //
  // The server resolves a URL without `?org=` to this remembered org (lib/console-org.ts), so a
  // link or page load that dropped it no longer flashes "Select your organization". The URL is
  // put back too, so every href built from location.search from here on carries it again.
  const orgId = org?.id;
  useEffect(() => {
    if (!orgId) return;
    rememberOrg(orgId);
    if (routeMode) return;
    const restored = withOrgRestored(window.location.search, orgId);
    if (restored) {
      try {
        window.history.replaceState(null, "", `${window.location.pathname}${restored}${window.location.hash}`);
      } catch { /* non-browser */ }
    }
  }, [orgId, routeMode, queryString]);

  // Depend on the query STRING (stable across router.refresh(), changes on real
  // navigation) so the area syncs from the URL on deep links like "View in Work →"
  // without the 7s auto-refresh snapping the user back.
  useEffect(() => {
    // A RETIRED SURFACE IS REWRITTEN BEFORE ANYTHING READS IT. The server redirects these too;
    // this covers the URL changing under a mounted console (Back, a pushState from a panel).
    const meant = consoleParams(new URLSearchParams(queryString));
    const legacy = meant.changed ? meant.params : null;
    if (legacy) {
      try {
        window.history.replaceState(null, "", `${window.location.pathname}?${legacy}${window.location.hash}`);
        window.dispatchEvent(new PopStateEvent("popstate"));
      } catch { /* non-browser */ }
    }
    // A route page (Identity, My account) is what its ROUTE says: a remembered area from the
    // area page must not repaint it as Command Center (lib/shell-heading.ts).
    const shown = shownArea({
      routeMode, routeArea: deepLinkArea ?? null,
      requested: (legacy ?? new URLSearchParams(queryString)).get("area"),
      stored: window.localStorage.getItem("boss.area"),
      known: AREAS.map((a) => a.id), fallback: active,
    });
    if (shown) setActive(shown as AreaId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryString]);

  // AN AREA OPENED FOR THE FIRST TIME IS FETCHED ONCE, WITH A REAL LOADING STATE.
  //
  // The server renders the requested area plus the ones this session has opened
  // (lib/area-loading.ts). One that is not in the DOM yet is added to the session cookie and
  // the page refreshes; the areas already on screen keep their tree positions, so they update
  // rather than remount. Every later click on it is the pure reveal below.
  const deferredUrl = useRef<{ id: string; href: string } | null>(null);
  // A DRAFT ON SCREEN: something typed into the console since this document loaded. Opening an
  // area is a page load unless a draft would be lost by one (ensureLoaded below).
  const typedSinceLoad = useRef(false);
  useEffect(() => {
    const onInput = (event: Event) => {
      const el = event.target as HTMLElement | null;
      if (el && typeof el.closest === "function" && el.closest("main.bx-content, .bx-dock")) {
        typedSinceLoad.current = true;
      }
    };
    document.addEventListener("input", onInput, true);
    return () => document.removeEventListener("input", onInput, true);
  }, []);
  const ensureLoaded = useCallback((id: string) => {
    const loaded = loadedRef.current;
    if (!loaded || loaded.includes(id)) return;
    // AN AREA NOT YET IN THIS DOCUMENT IS OPENED WITH A PAGE LOAD (2026-10-10).
    //
    // Fetching it into the open page is a router.refresh(), and Next 15.5 left that refresh
    // uncommitted far too often: measured on a seeded stack, rail clicks from the Command Center
    // to Activity hung 4 times in 4, to the Factory 4 in 4 — the RSC arrived whole, React never
    // committed it, and the operator read "Loading Factory…" until the 20 s background poll
    // (7 of 9 first clicks on the laptop install). A document load of the area is deterministic
    // and fast — 0.38-0.41 s to content for every area on the same stack — so it is the path.
    // The refresh stays for one case only: something typed on screen that a page load would
    // throw away. That path keeps every mounted area (the cookie below) and is unstuck by the
    // coordinator's watchdog if Next drops it.
    if (!typedSinceLoad.current) {
      try {
        const url = new URL(window.location.href);
        if (url.searchParams.get("area") === id) window.location.replace(url.toString());
        else {
          url.searchParams.set("area", id);
          window.location.assign(url.toString());
        }
        setAreaLoadAsked({ id, at: Date.now() });
        return;
      } catch {
        /* no location (a test): fall through to the refresh */
      }
    }
    try {
      // The first document renders its area without adding it to the cookie. Preserve every
      // mounted area before fetching another, so that refresh cannot discard an unsaved draft.
      let remembered = loadedAreasCookieValue(document.cookie);
      for (const area of loaded) remembered = cookieWith(remembered, area);
      document.cookie = loadedAreasCookie(
        cookieWith(remembered, id));
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
    // through go(): the same fetch, once. First wait for Next's URL restore to commit:
    // a restore prioritizes itself over a pending refresh and discards that response.
    // The urgent area reveal can precede useSearchParams, so loading immediately would
    // fetch the right URL but lose the payload when the router catches up.
    // A rail click (go()) DEFERS its URL until the area arrives (deferredUrl, below), so there
    // the URL is not waited for: it is this shell's own, not a restore Next is still committing.
    // A BARE URL HAS NOTHING TO RESTORE. `/?org=x` reopens the area remembered in this browser
    // (shownArea: stored), which the server never rendered; waiting for `?area=` to name it
    // waited forever: "Loading Get started…" until a reload or another page (2026-11-05).
    const urlArea = new URLSearchParams(queryString).get("area");
    const urlCommitted = urlArea === active || (!urlArea && !routeMode)
      || deferredUrl.current?.id === active;
    if (activeLoading && areaLoadAsked?.id !== active && urlCommitted) ensureLoaded(active);
  }, [activeLoading, active, areaLoadAsked, ensureLoaded, queryString, routeMode]);
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
    if (!activeLoading || refreshing || refreshCoordinator.busy()
        || areaLoadAsked?.id !== active) return;
    if (areaRetries.current.id !== active) areaRetries.current = { id: active, n: 0 };
    if (areaRetries.current.n >= 5) return;
    const t = window.setTimeout(() => {
      areaRetries.current.n += 1;
      setAreaLoadAsked(null);  // the effect above sees an area not yet asked for, and asks
    }, 250);
    return () => window.clearTimeout(t);
    // renderToken: a refresh that landed WITHOUT the area changes nothing else this reads.
  }, [activeLoading, refreshing, refreshCoordinator, areaLoadAsked, active, renderToken, status, loadedAreas]);
  const [loadSlow, setLoadSlow] = useState(false);
  useEffect(() => {
    setLoadSlow(false);
    if (!activeLoading) return;
    const t = window.setTimeout(() => setLoadSlow(true), 20_000);
    return () => window.clearTimeout(t);
  }, [activeLoading, active]);

  const go = useCallback((id: AreaId) => {
    // An area with its own route (Identity), or any area pressed from a route page: navigate.
    const routed = ROUTE_AREAS[id];
    if (routed || routeMode) {
      setNavTarget(id);
      setNavOpen(false);
      setPaletteOpen(false);
      const org = new URLSearchParams(window.location.search).get("org");
      const target = routed ?? `/?area=${id}${org ? `&org=${encodeURIComponent(org)}` : ""}`;
      window.location.assign(target);
      return;
    }
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
        // AN AREA STILL TO BE FETCHED GETS ITS URL WHEN IT ARRIVES. A pushState is a router
        // action in Next 15 (it syncs the router's URL); one issued just before the area's
        // refresh made that refresh hang far more often (lib/console-refresh.ts: most first
        // clicks after a load, against ~1 in 5 for a refresh alone). The refresh asks for the
        // area through the session cookie, not the URL, so the URL can follow it.
        const loaded = loadedRef.current;
        if (loaded && !loaded.includes(id)) {
          deferredUrl.current = { id, href: `${url.pathname}${url.search}` };
        } else {
          deferredUrl.current = null;
          window.history.pushState(null, "", `${url.pathname}${url.search}`);
          window.dispatchEvent(new PopStateEvent("popstate"));
        }
      }
    } catch {
      /* non-browser / unsupported — the area is already set above, so the console still moves */
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, [routeMode]);
  // The deferred URL, once the area it names is on screen (and is still the one being shown).
  useEffect(() => {
    const due = deferredUrl.current;
    if (!due || !loadedAreas?.includes(due.id)) return;
    deferredUrl.current = null;
    if (active !== due.id) return;
    try {
      // From the URL as it is NOW, not as it was at the click: a filter or a restore that
      // committed while the area loaded (`&state=pending`) is kept, never overwritten.
      const url = new URL(window.location.href);
      url.searchParams.set("area", due.id);
      if (url.toString() === window.location.href) return;
      window.history.pushState(null, "", `${url.pathname}${url.search}${url.hash}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    } catch { /* non-browser */ }
  }, [loadedAreas, active]);

  // The Identity area lives at /identity: however it was opened here (a link, Back, the
  // palette), go there.
  useEffect(() => {
    if (!routeMode && ROUTE_AREAS[active]) window.location.assign(ROUTE_AREAS[active]!);
  }, [active, routeMode]);

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
        currentSearch: window.location.search,
        load: link.hasAttribute("data-console-load"),
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
  // RESPONSIVE BY DEFAULT (cc/v2): clamp(360px, 40vw, 520px), the dragged width when there
  // is one, and never so wide that the page column falls under 560px (lib/shell-rail.ts).
  const [dragged, setDragged] = useState<number | null>(null);
  const [viewportW, setViewportW] = useState(1440);
  useEffect(() => {
    const saved = Number(window.localStorage.getItem("boss.dock.width") || 0);
    if (saved >= 320) setDragged(saved);
    const read = () => setViewportW(window.innerWidth || 1440);
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, []);
  const dockWidth = dockWidthFor(viewportW, dragged);
  const startResize = useCallback((event: React.MouseEvent) => {
    event.preventDefault();
    const onMove = (move: MouseEvent) => {
      // Clamped so the panel can never swallow the console or shrink past readable.
      setDragged(dockWidthFor(window.innerWidth, window.innerWidth - move.clientX));
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.userSelect = "";
      setDragged((w) => { if (w) window.localStorage.setItem("boss.dock.width", String(w)); return w; });
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
      if (routeMode) return; // a route page's area is its route, not ?area=
      const area = new URLSearchParams(window.location.search).get("area");
      if (area && AREAS.some((a) => a.id === area)) setActive(area as AreaId);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [routeMode]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // An area that answered the key itself (the Graph's own ⌘K search, a canvas's Esc) has
      // prevented its default: the shell neither opens the palette nor collapses the rail.
      if (e.defaultPrevented) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      } else if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        // ⌘\ docks the rail to icons and back (BOSSAPP-914 screen 1): the same press as its button.
        e.preventDefault();
        toggleDockRef.current();
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
  // THE RAIL SHOWS ONLY WHAT THE VIEWER CAN OPEN (IDENTITY.md §6.5). A direct URL to one they
  // cannot still lands — on <AccessDenied> naming the permission, never a blank page.
  const navAreas = useMemo(
    () => areasForEdition.filter(
      (a) => isCommunityPrimaryNavVisible(a.id, resolvedEdition) && !OWN_RAIL_BUTTON.has(a.id) && !UNDER_PROJECTS.has(a.id) && !OFF_THE_NAV.has(a.id) && areaCan(a.id).allowed),
    [areasForEdition, resolvedEdition, areaCan],
  );

  const activeDef = areasForEdition.find((a) => a.id === active) ?? areasForEdition[0];
  const heading = shellHeading(areasForEdition, { routeMode, routeArea: deepLinkArea ?? null, active, title });
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
  // GET STARTED (BOSSAPP-808): shown while GitHub, a model or a first PR is missing.
  // The rail's project list: open by default, the operator's choice remembered.
  const [projectsOpen, setProjectsOpen] = useState(true);
  useEffect(() => {
    try { if (localStorage.getItem("boss.nav.projects") === "0") setProjectsOpen(false); } catch { /* private mode */ }
  }, []);
  useEffect(() => {
    try { localStorage.setItem("boss.nav.projects", projectsOpen ? "1" : "0"); } catch { /* private mode */ }
  }, [projectsOpen]);
  const [getStarted, setGetStarted] = useState(Boolean(status.getStarted));
  useEffect(() => { setGetStarted(Boolean(status.getStarted)); }, [status.getStarted]);
  useEffect(() => {
    const on = (event: Event) => setGetStarted(Boolean((event as CustomEvent).detail));
    window.addEventListener(GET_STARTED_EVENT, on);
    return () => window.removeEventListener(GET_STARTED_EVENT, on);
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
  // THE DOCK MAKES ROOM (operator decision, cc/v2): with Talk to BOSS open and the page column
  // under 720px, the sidebar folds to its icon rail; it comes back when the dock closes. The
  // operator's own collapse is `docked` (remembered) and is never touched by this. Pressing the
  // toggle while it is folded for the dock keeps it open until the dock closes.
  const [railForChat, setRailForChat] = useState(false);
  const [keptOpen, setKeptOpen] = useState(false);
  useEffect(() => {
    if (!dockedRun) { setRailForChat(false); setKeptOpen(false); return undefined; }
    const read = () => setRailForChat(railForDock({ dockOpen: true, viewport: window.innerWidth, dockWidth }));
    read();
    window.addEventListener("resize", read);
    return () => window.removeEventListener("resize", read);
  }, [dockedRun, dockWidth]);
  const railed = docked || (railForChat && !keptOpen);
  const toggleDock = () => {
    if (!docked && railForChat && !keptOpen) { setKeptOpen(true); return; }
    setDocked((wasDocked) => {
      const next = !wasDocked;
      try { localStorage.setItem("boss.nav.docked", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  };
  toggleDockRef.current = toggleDock;

  // ONE WORKFLOW, OR ONE RUN, WITH THE WHOLE WINDOW (`page=full`, app/workflows/full-page.ts):
  // the sidebar, the top bar and the project strip step aside; the area draws its own way back.
  const fullPage = useFullPage() && active === "delivery_workflows" && !routeMode;

  return (
    <div className={`bx-shell${navOpen ? " nav-open" : ""}${railed ? " nav-docked" : ""}`}
         data-route-mode={routeMode || undefined}
         data-page={fullPage ? "full" : undefined}
         data-rail-for-dock={railed && !docked ? "true" : undefined}>
      <a className="skip-link" href="#decision-inbox">Skip to decision inbox</a>

      {/* The rail's colour runs the full height of the page, however long the page is. */}
      <div className="bx-rail-bg" aria-hidden />
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
          </span>
          <button
            type="button"
            className="bx-dock-toggle"
            data-testid="nav-dock-toggle"
            aria-pressed={railed}
            aria-label={railed ? "Expand the sidebar" : "Collapse the sidebar to icons"}
            title={railed ? "Expand the sidebar" : "Collapse to icons"}
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
          {/* GET STARTED IS PART OF PROJECTS NOW (BOSSAPP-914): while setup is unfinished the rail's
              Projects opens the first run (its 3 steps, then Helm onboarding the first project). The
              old Welcome stays reachable by URL (`?area=welcome`) for its quick-PR row. */}
          {getStarted && areaCan("projects").allowed && !navAreas.some((a) => a.id === "projects") ? (
            <button type="button" className={`bx-nav-item bx-nav-get-started${railSelected === "projects" ? " active" : ""}`}
                    data-testid="nav-get-started" data-area-nav="projects"
                    data-label="Get started" title="Get BOSS working: GitHub, a model, your first project"
                    onClick={() => go("projects")}>
              <Rocket size={17} aria-hidden />
              <span>Get started</span>
            </button>
          ) : null}
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
                                className={`bx-nav-item bx-nav-child${child.id === railSelected ? " active" : ""}`}
                                aria-current={child.id === railSelected ? "page" : undefined}
                                data-area-nav={child.id}
                                onClick={() => go(child.id)}
                              >
                                <ChildIcon size={15}
                                            weight={child.id === railSelected ? "fill" : "regular"} aria-hidden />
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
                if (a.id === "projects") {
                  return (
                    <ProjectsNav key="projects" items={projects?.items ?? []} active={railSelected ?? ""}
                                 activeProjectId={scope.projectId ?? null} onGo={() => go("projects")}
                                 open={projectsOpen} onToggle={() => setProjectsOpen((v) => !v)} />
                  );
                }
                const badge = badgeFor(a.id);
                const Icon = a.icon;
                const paidBadge = shouldShowPaidNavBadge(a.id, resolvedEdition);
                // Identity is more than SSO now: no "SSO" badge on its rail row.
                const ssoBadge: string | undefined = undefined;
                return (
                  <button
                    key={a.id}
                    type="button"
                    className={`bx-nav-item${a.id === railSelected ? " active" : ""}`}
                    aria-current={a.id === railSelected ? "page" : undefined}
                    data-area-nav={a.id}
                    // The label survives docking as a tooltip — icons alone are a memory test.
                    data-label={`${labelFor(a)} — ${a.sub}`}
                    title={`${labelFor(a)} — ${a.sub}`}
                    data-paid-badge={paidBadge ? "true" : undefined}
                    data-sso-visible={a.id === "enterprise" ? "true" : undefined}
                    onClick={() => go(a.id)}
                  >
                    <Icon size={17} weight={a.id === railSelected ? "fill" : "regular"} aria-hidden />
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
          {/* WHO IS SIGNED IN — the person, from the session, never a claimed actor string. */}
          <div className="bx-operator" data-testid="signed-in-as">
            {/* The person's own face (the identity kit's, as in the avatar menu), then "Name · Role". */}
            {me ? <Face name={me.display_name} tone={avatarTone(me.principal_id, me.avatar_seed)} size={22} />
              : <span className="bx-operator-avatar" aria-hidden>{scope.actor.slice(0, 1).toUpperCase()}</span>}
            <span>
              <strong>{me?.display_name ?? scope.actor}</strong>
              <span className="bx-operator-dot" aria-hidden>·</span>
              {/* The highest role, from /v1/identity/me (lib/identity/role-label); never a guess. */}
              <small data-testid="signed-in-role" title={me ? allRoleLabels(me.access_roles) : undefined}>
                {me ? primaryRoleLabel(me.access_roles) : "Signed in"}
              </small>
            </span>
          </div>
          <div className="bx-control-line">
            <div className="bx-control-status">
              <span className={`dot ${status.apiReachable ? "green" : "red"}`} />
              <span>{status.apiReachable ? "Control plane online" : "Control plane offline"}</span>
            </div>
            {status.controlPlane && !isInternalPlane(status.controlPlane) ? (
              <div className="bx-control-host" title={status.controlPlane}
                   data-local={isLocalPlane(status.controlPlane) || undefined}
                   data-testid="control-plane-host">
                {planeLabel(status.controlPlane)}
              </div>
            ) : null}
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
            {heading.label}
            <small>{heading.sub}</small>
          </div>

          {/* WHERE YOU ARE (BOSSAPP-914 screen 1): Projects / <Project ▾> / <Tab> on a project's page,
              <Project ▾> / <Area> elsewhere. The project crumb IS the project menu (switch, New
              project, archive), so scope is said once, here — no "Project: X ← Back to Y" banner. */}
          <ShellCrumbs active={active} areaLabel={heading.label} routeMode={routeMode}
                       projects={projects} projectId={scope.projectId}
                       onProjects={() => go("projects")} />

          <div className="bx-topbar-spacer" />

          <button type="button" className="bx-jump" data-testid="topbar-jump" onClick={() => setPaletteOpen(true)}
                  aria-label="Jump to… (⌘K)" title="Jump to any project, run, PR, deploy or agent">
            <MagnifyingGlass size={15} aria-hidden />
            <span className="bx-jump-text">Jump to…</span>
            <kbd>⌘K</kbd>
          </button>

          <RefreshState coordinator={refreshCoordinator} fallback={refreshing}>{(refreshing) => (
          <button
            type="button"
            className="bx-live-status"
            data-live={(live && status.apiReachable) || undefined}
            title={live
              ? (refreshing
                 ? "Fetching the latest data — click to pause live updates"
                 : "Live: auto-refreshing — click to pause")
              : "Paused — click to resume live updates"}
            onClick={() => setLive((v) => !v)}
            style={{ cursor: "pointer" }}
          >
            {/* A refresh in flight shows HERE: same chip, no layout shift, gone when it
                lands. Enough to answer "is it updating?" without occupying the page. */}
            {/* Grey and "Offline" when the control plane does not answer: Live is never claimed then. */}
            <span className={live && status.apiReachable ? "dot green bx-pulse" : "dot"} />
            <span className="bx-live-word">{!status.apiReachable ? "Offline" : live ? (refreshing ? "Syncing" : "Live") : "Paused"}</span>
          </button>)}</RefreshState>

          {/* START A BUILD FROM ANYWHERE (factory spec §4.1). The same first press as the
              Factory's `+ New build`: the New project composer — a new app, or a feature added to
              one (operator, 2026-10-07; it replaced the 2026-10-04 "Plan it with Juno" door).
              "Plan it with an agent" is an option inside the composer. */}
          {/* ONE PRODUCT (walk D19): on the Factory its own header holds `+ New build`. */}
          {active !== "code_tasks" ? <GatedButton
            permission="workflow_run:start"
            type="button"
            className="bx-new-project"
            data-testid="topbar-new-project"
            aria-label="New build"
            title={NEW_BUILD_TIP}
            onClick={() => openNewProject()}
          >
            {/* An icon button with its tooltip (BOSSAPP-914 top bar); the avatar menu and ⌘K say
                "New build" in words. */}
            <Plus size={15} weight="bold" aria-hidden /><span className="bx-new-label">New build</span>
          </GatedButton> : null}

          {/* BUILDS, A CHIP BESIDE THE BELL (BOSSAPP-914 round 3): the old strip under the bar is in no
              wireframe and cost a row of every screen (a tenth of a phone's). Same list, from the chip.
              The Factory's Floor IS the list of builds (walk D19); a project's page has its Work tab. */}
          {active !== "code_tasks" && active !== "projects" && active !== "project" && !routeMode && !fullPage ? <ProjectStrip projectId={scope.projectId ?? scope.project} area={active} chip /> : null}

          {/* THE BELL: what needs you (the count) and who is working (the teal dot), with the crew
              and the way to Approvals behind it — the old "1 working · 2 need you" pill, folded in. */}
          <PresencePill projectId={scope.projectId ?? scope.project} variant="bell" />

          {/* THE AVATAR MENU: you, your account, the organization (switch), New build, and the old
              System menu (edition, budget, console version, sign out). */}
          {me ? (
            <AccountMenu compact>
              <ShellMenuRows org={org} onSwitchOrg={switchOrg} edition={resolvedEdition}
                             budgetState={status.budgetState} tone={tone}
                             projects={projects} projectId={scope.projectId} />
            </AccountMenu>
          ) : (
            <MenuDetails className="idn-me bx-user-fallback">
              <summary aria-label={`Signed in as ${scope.actor}`} title={scope.actor}>
                <span className="bx-operator-avatar" aria-hidden>{scope.actor.slice(0, 1).toUpperCase()}</span>
              </summary>
              <div className="idn-me-menu" role="menu">
                <div className="idn-me-who"><b>{scope.actor}</b><small>Signed in</small></div>
                <ShellMenuRows org={org} onSwitchOrg={switchOrg} edition={resolvedEdition}
                               budgetState={status.budgetState} tone={tone}
                             projects={projects} projectId={scope.projectId} />
                {/* SIGN OUT. /api/auth/logout existed and nothing called it, so the only way to
                    end a console session on a shared machine was to clear the browser's cookies.
                    Shown only when sign-in is configured: there is no session to end otherwise. */}
                {signOutAvailable ? (
                  <div data-testid="bx-sign-out">
                    <button type="button" role="menuitem" onClick={signOut}><SignOut size={16} aria-hidden /> Sign out</button>
                  </div>
                ) : null}
              </div>
            </MenuDetails>
          )}
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
        <RefreshState coordinator={refreshCoordinator} fallback={refreshing}>{(refreshing) => (
        refreshing ? (
          <div className="bx-refresh-line" role="status" aria-live="polite"
               aria-label="Refreshing data" data-testid="console-refreshing" />
        ) : null)}</RefreshState>

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

        {/* The open project's archived notice, when the Builds chip in the top bar has one. */}
        <div id={STRIP_NOTICE_SLOT} className="bx-strip-notice" />

        {/* The console gets out of the panel's way rather than hiding behind it. The dock is
            fixed-position, so without this inset the right-hand column of cards sat underneath
            it, unreachable. */}
        {/* The area being left stays on screen while the next one is fetched, so without this
            a slow area looks like a dead click on the old screen. A hairline across the top is
            the smallest honest signal that the press was taken and work is in flight. */}
        <style data-testid="bx-area-visibility" dangerouslySetInnerHTML={{ __html: AREA_VISIBILITY_CSS }} />
        <ActiveAreaContext.Provider value={routeMode ? null : active}>
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
                  <button type="button" className="linklike"
                          onClick={() => window.location.reload()}>
                    Reload the page
                  </button>
                </span>
              ) : null}
            </div>
          ) : null}
          <IdentityBanners />
          {/* A DIRECT URL TO AN AREA THE VIEWER CANNOT OPEN names what is missing (§9.10).
              Route pages (Identity, My account) guard themselves in their layouts. */}
          {!routeMode && !areaCan(active).allowed ? (() => {
            const access = accessForArea(active);
            return "permission" in access
              ? <AccessDenied permission={access.permission} page={activeDef.label} />
              : children;
          })() : children}
        </main>
        </ActiveAreaContext.Provider>
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
                // ENTER TAKES THE FIRST CHOICE SHOWN, whatever it is (rc10.5 walk m2): it acted
                // on areas only, so "new build" + Enter did nothing.
                if (e.key === "Enter" && activatePaletteChoice(paletteList.current)) e.preventDefault();
              }}
            />
            <div className="bx-palette-list" ref={paletteList}>
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
              {/* PROJECT RECORDS (BOSSAPP-914 §9): runs, PRs, builds, deploys and agents of the open
                  project, and the other projects — one keystroke from anywhere. */}
              <JumpResults query={query} projectId={scope.projectId ?? scope.project}
                           projects={projects?.items ?? []} onDone={() => setPaletteOpen(false)} />
              {/* START SOMETHING: the New project composer, like the top bar's New build. */}
              {!query.trim() || "new build new project".includes(query.trim().toLowerCase()) ? (
                <button type="button" className={`bx-palette-item${filtered.length ? "" : " active"}`}
                        data-testid="palette-new-build"
                        title={NEW_BUILD_TIP}
                        onClick={() => { setPaletteOpen(false); openNewProject(); }}>
                  <Plus size={17} aria-hidden />
                  <span>
                    <span style={{ fontWeight: 600 }}>New build</span>
                    <span style={{ display: "block", fontSize: "0.78rem", color: "var(--muted)" }}>A new app or a feature</span>
                  </span>
                </button>
              ) : null}
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
              {filtered.length === 0 && !"new build new project".includes(query.trim().toLowerCase())
                && !CONVERSE_COMMANDS.some((c) => c.label.toLowerCase()
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


/** A header menu on <details> that closes on Esc and on a press outside it (lib/details-dismiss). */
function MenuDetails({ className, children }: { className: string; children: React.ReactNode }) {
  const ref = React.useRef<HTMLDetailsElement>(null);
  const { onToggle } = useDetailsDismiss(ref);
  return <details ref={ref} className={className} onToggle={onToggle}>{children}</details>;
}

/** The avatar menu's rows beyond your account: the organization (and switching it), New build, and
 *  what the old System menu held — edition, budget state and the console version. */
function ShellMenuRows({ org, onSwitchOrg, edition, budgetState, tone, projects, projectId }: {
  org?: { id: string; name: string }; onSwitchOrg: () => void; edition: string;
  budgetState: string; tone: "green" | "amber" | "red";
  projects?: { items: ProjectMenuItem[]; homeId: string }; projectId?: string;
}) {
  return (
    <>
      {projects && projectId ? (
        <>
          {/* THE PROJECT MENU (switch, + New project, Archive project…, Show archived): moved here
              from the bar, where the breadcrumb and the rail's list now say which project is open. */}
          <div className="bx-menu-sep" role="separator">Project</div>
          <div className="bx-menu-project">
            <ProjectMenu projects={projects.items} activeId={projectId} homeId={projects.homeId} orgId={org?.id} />
          </div>
        </>
      ) : null}
      <div className="bx-menu-sep" role="separator">Organization</div>
      {org ? (
        <button type="button" role="menuitem" className="bx-menu-org" data-testid="menu-switch-org"
                onClick={onSwitchOrg} title="Switch organization">
          <Buildings size={16} aria-hidden /><span className="bx-menu-org-name">{org.name}</span>
          <span className="bx-menu-aside">Switch</span>
        </button>
      ) : null}
      <GatedButton permission="workflow_run:start" type="button" role="menuitem" data-testid="menu-new-build"
                   title={NEW_BUILD_TIP} onClick={() => openNewProject()}>
        <Plus size={16} aria-hidden /> New build
      </GatedButton>
      <div className="bx-menu-sep" role="separator">System</div>
      <div className="bx-system-panel" data-testid="menu-system">
        <div><span>Edition</span><EditionChip editionId={edition} /></div>
        <div><span>Budget</span><strong className={`bx-system-budget ${tone}`}>{budgetState || "unknown"}</strong></div>
        <div><span>Console</span><ConsoleToggle current="v1" orgId={org?.id} /></div>
      </div>
    </>
  );
}

