import Link from "next/link";
import type { ReactNode } from "react";

import { NavIcon } from "./NavIcon";
import { AutonomyControl } from "./AutonomyControl";
import { LaunchButton } from "./LaunchButton";
import { ConsoleToggle } from "./ConsoleToggle";
import type { CompanyOverview } from "../../lib/v2/overview";
import { IdentityProvider } from "../identity/identity-provider";
import { loadIdentity } from "../../lib/identity/server";
import { identityStubEnabled } from "../../lib/identity/stub/enabled";

/**
 * The v2 shell: top bar, project strip, sidebar, autonomy footer.
 *
 * The nav is nine items, in the artifact's order. The internal ids are kept as the route
 * segments because they say what each screen is FOR in a way the labels do not — `panels`
 * is where agents argue, `factory` is the workforce, `timeline` is the audit trail.
 */

/**
 * The nine screens, and honestly which of them exist yet.
 *
 * `built: false` is not decoration. Six of these pointed at routes that did not exist, so the
 * nav offered six links that 404 — which is the same defect the operator opened this whole
 * effort with ("fake buttons"), reproduced in the replacement for the console that had them.
 *
 * An unbuilt entry still navigates, to a page that says what will live there and links to the
 * v1 area that has it today. That is the difference between a placeholder and a dead end: the
 * feature stays reachable while it is being moved.
 */
/**
 * Labels match v1's for the same screen, and each entry links to the v1 area that IS that
 * screen. The console walk on boss-1 (2026-10-02) found "Workflows" linking to v1 Infra,
 * "Infra & Models" to v1 Observability, "Workforce" to the Task Board and "Org Chart" to Access
 * and packs, with the same screens named differently in each console. Where a v2 label still
 * differs, docs/product/console_v2_parity.yaml says why (tests/console-v2-surface.test.ts).
 */
export const V2_NAV = [
  { id: "missions", label: "Code Tasks", href: "/v2", built: true, v1Area: "code_tasks" },
  { id: "hive", label: "Hive", href: "/v2/hive", built: true, v1Area: "agent" },
  { id: "factory", label: "Agent Studio", href: "/v2/factory", built: true, v1Area: "workforce" },
  { id: "org", label: "Team structure", href: "/v2/org", built: true, v1Area: "workforce" },
  { id: "panels", label: "Approvals", href: "/v2/panels", built: true, v1Area: "decisions" },
  { id: "boss", label: "Workflows", href: "/v2/boss", built: true, v1Area: "delivery_workflows" },
  { id: "timeline", label: "Receipts", href: "/v2/timeline", built: true, v1Area: "activity" },
  { id: "infra", label: "Infra & Models", href: "/v2/infra", built: true, v1Area: "infra" },
  { id: "settings", label: "Settings", href: "/v2/settings", built: true, v1Area: "setup" },
] as const;

export type V2NavId = (typeof V2_NAV)[number]["id"];

function money(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return "$" + Math.round(n).toLocaleString();
}

/**
 * THE v2 SCREENS MIRROR WHO IS SIGNED IN (round 3 role walk). The Shell rendered no
 * IdentityProvider, so every gated control under it — the autonomy footer's "Switch to Full auto"
 * (approval_rule:manage, Owner only) — asked the unmirrored default, which allows everything:
 * a Viewer saw it live. Like ConsoleShell, the Shell loads the person's org and project
 * permissions and provides them; the API still decides every call.
 */
export async function Shell(props: {
  active: V2NavId;
  overview: CompanyOverview;
  orgId?: string;
  children: ReactNode;
}) {
  const projectId = props.overview.company.id || null;
  const identity = await loadIdentity(projectId);
  return (
    <IdentityProvider me={identity.me} org={identity.org} project={identity.project} projectId={projectId}
      stub={identityStubEnabled()} unavailable={!identity.me || !identity.org}>
      <ShellFrame {...props} />
    </IdentityProvider>
  );
}

function ShellFrame({
  active,
  overview,
  orgId,
  children,
}: {
  active: V2NavId;
  overview: CompanyOverview;
  orgId?: string;
  children: ReactNode;
}) {
  const q = orgId ? `?org=${encodeURIComponent(orgId)}` : "";
  const gates = overview.gates;

  return (
    <div className="v2-root v2-shell" data-console="v2">
      {/* ── Top bar ─────────────────────────────────────────────────── */}
      <header className="v2-topbar">
        <span className="v2-brand">BOSS</span>
        <span className="v2-label v2-brand-sub">Mission Control</span>

        <button type="button" className="v2-company" data-testid="v2-company">
          <span className="v2-dot" data-status="RUNNING" aria-hidden />
          {overview.company.name}
        </button>

        {/* Starts a project HERE now, rather than sending the operator to the other console.
            Was a link to /v2/new, a route that did not exist -- the console's primary call to
            action 404ed. */}
        <LaunchButton projectId={overview.company.id} orgId={orgId} />

        <div className="v2-topbar-right">
          {/* Burn reads as machine data, so it is mono and tabular — the numbers line up
              as they change, which is the point of watching a spend figure at all. */}
          <span className="v2-meta" data-testid="v2-burn">
            <span className="v2-label">Burn</span>
            <b className="v2-mono">{money(overview.burn.spent_usd)}</b>
            {overview.burn.cap_usd !== null ? (
              <span className="v2-mono v2-meta-dim"> / {money(overview.burn.cap_usd)}</span>
            ) : null}
          </span>

          {/* A count of zero is not the same as a queue with work in it: at zero this is
              deliberately quiet, so an amber number always means something is waiting. */}
          <Link
            href={`/v2/panels${q}`}
            className="v2-meta v2-meta-link"
            data-active={gates > 0}
            data-testid="v2-gates"
          >
            <b className="v2-mono">{gates}</b>
            <span className="v2-label">{gates === 1 ? "Gate" : "Gates"}</span>
          </Link>

          <ConsoleToggle current="v2" orgId={orgId} />
        </div>
      </header>

      {/* ── Project strip ───────────────────────────────────────────── */}
      <nav className="v2-strip" aria-label="Projects">
        <span className="v2-label v2-strip-label">Projects</span>
        {overview.projects.slice(0, 6).map((p) => (
          <Link
            key={p.id}
            href={`/v2/missions/${encodeURIComponent(p.id)}${q}`}
            className="v2-strip-item"
            data-testid={`v2-strip-${p.id}`}
          >
            <span className="v2-dot" data-status={p.status} aria-hidden />
            <span className="v2-strip-name">{p.title}</span>
            <span className="v2-mono v2-strip-stage">{p.stage}</span>
            <span className="v2-mono v2-strip-pct">{p.pct}%</span>
            {p.status === "GATED" ? (
              <span className="v2-mono v2-strip-alert">GATED</span>
            ) : null}
          </Link>
        ))}
        {overview.projects.length === 0 ? (
          <span className="v2-strip-empty">No projects yet</span>
        ) : null}
      </nav>

      <div className="v2-body">
        {/* ── Sidebar ───────────────────────────────────────────────── */}
        <aside className="v2-sidebar">
          <nav aria-label="Console sections">
            {V2_NAV.map((item) => (
              <Link
                key={item.id}
                href={`${item.href}${q}`}
                className="v2-nav-item"
                data-active={item.id === active}
                data-built={item.built}
                data-testid={`v2-nav-${item.id}`}
              >
                <NavIcon id={item.id} />
                {item.label}
                {item.id === "panels" && gates > 0 ? (
                  <span className="v2-mono v2-nav-badge">{gates}</span>
                ) : null}
                {/* Says which sections are not moved yet, so nothing here reads as broken.
                    A word, not only a colour. */}
                {!item.built ? (
                  <span className="v2-mono v2-nav-soon" data-testid={`v2-soon-${item.id}`}>
                    v1
                  </span>
                ) : null}
              </Link>
            ))}
          </nav>

          {/* Autonomy. A REAL switch now: Full auto lets small, reversible, well-understood
              gates approve themselves, and nothing else. See services/autonomy.py for what it
              must still refuse — the design question was never "how do I auto-approve a gate"
              but "what must remain impossible after I do". */}
          <AutonomyControl
            projectId={overview.company.id}
            actor="executive_console"
            initial={overview.autonomy}
          />
        </aside>

        <main className="v2-main">{children}</main>
      </div>
    </div>
  );
}
