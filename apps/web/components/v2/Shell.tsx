import Link from "next/link";
import type { ReactNode } from "react";

import { AutonomyControl } from "./AutonomyControl";
import { ConsoleToggle } from "./ConsoleToggle";
import type { CompanyOverview } from "../../lib/v2/overview";

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
export const V2_NAV = [
  { id: "missions", label: "Projects", href: "/v2", built: true, v1Area: "code_tasks" },
  { id: "hive", label: "Hive", href: "/v2/hive", built: true, v1Area: "agent" },
  { id: "factory", label: "Workforce", href: "/v2/factory", built: true, v1Area: "work" },
  { id: "org", label: "Org Chart", href: "/v2/org", built: true, v1Area: "org" },
  { id: "panels", label: "Decisions", href: "/v2/panels", built: true, v1Area: "decisions" },
  { id: "boss", label: "Workflows", href: "/v2/boss", built: true, v1Area: "delivery" },
  { id: "timeline", label: "Receipts", href: "/v2/timeline", built: true, v1Area: "activity" },
  { id: "infra", label: "Infra & Models", href: "/v2/infra", built: true, v1Area: "monitoring" },
  { id: "settings", label: "Settings", href: "/v2/settings", built: true, v1Area: "capabilities" },
] as const;

export type V2NavId = (typeof V2_NAV)[number]["id"];

function money(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return "$" + Math.round(n).toLocaleString();
}

export function Shell({
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

        {/* POINTS AT THE THING THAT ACTUALLY STARTS A PROJECT.
            This linked to /v2/new, a route that does not exist, so the console's primary
            call to action 404ed. The v2 launch flow is not built yet; Ask BOSS on the current
            console is, and it is one click away rather than a dead end. */}
        <Link
          href={`/?area=overview${orgId ? `&org=${encodeURIComponent(orgId)}` : ""}`}
          className="v2-primary"
          data-testid="v2-new-project"
          title="Describe an outcome in Ask BOSS — the v2 launch flow has not moved yet"
        >
          New project
        </Link>

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
