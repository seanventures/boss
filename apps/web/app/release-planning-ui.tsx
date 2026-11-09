"use client";

import {
  type BoardColumn,
  type BoardItem,
  type ReleaseBoard,
  type ReleasePlan,
  type ReleaseStatusSnapshot,
} from "../lib/release-planning";

const COLUMN_META: Record<
  BoardColumn,
  { label: string; color: string; hint: string }
> = {
  built: {
    label: "Built",
    color: "var(--green)",
    hint: "Shipped / implemented / deployed",
  },
  building: {
    label: "Building",
    color: "var(--teal)",
    hint: "In progress / partial / allocated",
  },
  planned: {
    label: "Planned",
    color: "var(--muted)",
    hint: "Backlog / todo / undocument features",
  },
  blocked: {
    label: "Blocked",
    color: "var(--red)",
    hint: "Waiting, failed, or explicitly blocked",
  },
};

function tone(status?: string | null): string {
  const s = (status ?? "").toLowerCase();
  if (
    s === "implemented" ||
    s === "shipped" ||
    s === "done" ||
    s === "completed" ||
    s === "healthy"
  ) {
    return "var(--green)";
  }
  if (s === "blocked" || s === "failed" || s === "cancelled") return "var(--red)";
  if (s === "partial" || s === "active" || s === "in_progress" || s === "building") {
    return "var(--teal)";
  }
  if (s === "planned" || s === "open" || s === "todo") return "var(--muted)";
  return "var(--ink-soft)";
}

function Kpi({
  label,
  value,
  color,
  hint,
}: {
  label: string;
  value: string | number;
  color?: string;
  hint?: string;
}) {
  return (
    <div className="kpi-cell">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" style={color ? { color } : undefined}>
        {value}
      </div>
      {hint ? <div className="kpi-hint">{hint}</div> : null}
    </div>
  );
}

function BoardCard({ item }: { item: BoardItem }) {
  return (
    <div
      style={{
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: "10px 12px",
        background: "var(--surface, transparent)",
        display: "grid",
        gap: 4,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          gap: 8,
          alignItems: "baseline",
        }}
      >
        <strong style={{ fontSize: 13, lineHeight: 1.3 }}>{item.title}</strong>
        <span
          className="detail"
          style={{
            fontFamily: "var(--mono)",
            fontSize: 10,
            color: "var(--muted)",
            whiteSpace: "nowrap",
          }}
        >
          {item.sourceType}
        </span>
      </div>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 8,
          alignItems: "center",
        }}
      >
        {item.status ? (
          <span
            style={{
              fontFamily: "var(--mono)",
              fontSize: 11,
              color: tone(item.status),
            }}
          >
            {item.status}
          </span>
        ) : null}
        {item.issueKey ? (
          item.externalUrl ? (
            <a
              href={item.externalUrl}
              target="_blank"
              rel="noreferrer"
              className="detail"
              style={{ fontFamily: "var(--mono)", fontSize: 11 }}
            >
              {item.tracker ? `${item.tracker}:` : ""}
              {item.issueKey}
            </a>
          ) : (
            <span
              className="detail"
              style={{ fontFamily: "var(--mono)", fontSize: 11 }}
            >
              {item.tracker ? `${item.tracker}:` : ""}
              {item.issueKey}
            </span>
          )
        ) : null}
      </div>
    </div>
  );
}

function BoardColumnView({
  column,
  items,
}: {
  column: BoardColumn;
  items: BoardItem[];
}) {
  const meta = COLUMN_META[column];
  return (
    <div style={{ display: "grid", gap: 10, minWidth: 0 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          borderBottom: "1px solid var(--border)",
          paddingBottom: 8,
        }}
      >
        <div>
          <div style={{ fontWeight: 600, color: meta.color }}>{meta.label}</div>
          <div className="detail" style={{ fontSize: 11 }}>
            {meta.hint}
          </div>
        </div>
        <span
          style={{
            fontFamily: "var(--mono)",
            fontSize: 12,
            color: meta.color,
          }}
        >
          {items.length}
        </span>
      </div>
      <div style={{ display: "grid", gap: 8 }}>
        {items.length === 0 ? (
          <p className="detail" style={{ margin: 0 }}>
            Empty
          </p>
        ) : (
          items.map((item) => <BoardCard key={item.id} item={item} />)
        )}
      </div>
    </div>
  );
}

export function ReleaseStatusPanel({
  snapshot,
}: {
  snapshot: ReleaseStatusSnapshot | null;
}) {
  if (!snapshot) {
    return (
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Release status</h3>
        <p className="detail">Status snapshot unavailable.</p>
      </article>
    );
  }
  if (!snapshot.apiReachable) {
    return (
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Release status</h3>
        <p className="detail">
          Control plane unreachable
          {snapshot.blockedReason ? `, ${snapshot.blockedReason}` : "."}
        </p>
      </article>
    );
  }

  const last = snapshot.lastRelease;
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="kpi-strip">
        <Kpi
          label="Built"
          value={snapshot.builtCount}
          color="var(--green)"
          hint="documented / shipped"
        />
        <Kpi
          label="Building"
          value={snapshot.inProgress}
          color={snapshot.inProgress > 0 ? "var(--teal)" : undefined}
          hint="in progress"
        />
        <Kpi
          label="To build"
          value={snapshot.todoCount}
          hint="planned / backlog"
        />
        <Kpi
          label="Blocked"
          value={snapshot.blocked}
          color={snapshot.blocked > 0 ? "var(--red)" : undefined}
          hint="needs decision"
        />
        <Kpi
          label="Releases"
          value={snapshot.totals.releasePlans}
          hint={
            last
              ? `${last.name}${last.targetDate ? `, ${last.targetDate}` : ""}`
              : "none yet"
          }
        />
      </div>
      {last ? (
        <article className="stack-card" style={{ padding: 0, overflow: "hidden" }}>
          <div style={{ padding: "13px 16px", borderBottom: "1px solid var(--border)" }}>
            <span className="area-name">Last / active release</span>
          </div>
          <div className="hairline-list">
            <div className="hairline-row">
              <span className="detail" style={{ color: "var(--ink-soft)" }}>
                Name
              </span>
              <span style={{ fontWeight: 600 }}>{last.name}</span>
            </div>
            <div className="hairline-row">
              <span className="detail" style={{ color: "var(--ink-soft)" }}>
                Status
              </span>
              <span
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 12,
                  color: tone(last.status),
                }}
              >
                {last.status.toUpperCase()}
              </span>
            </div>
            <div className="hairline-row">
              <span className="detail" style={{ color: "var(--ink-soft)" }}>
                Target
              </span>
              <span className="detail" style={{ fontFamily: "var(--mono)" }}>
                {last.targetDate ?? "—"}
              </span>
            </div>
            <div className="hairline-row">
              <span className="detail" style={{ color: "var(--ink-soft)" }}>
                Tracker links
              </span>
              <span className="detail">
                {last.issueLinks.length === 0
                  ? "none"
                  : last.issueLinks.map((ln, i) => (
                      <span key={`${ln.tracker}-${ln.issueKey}-${i}`}>
                        {i > 0 ? ", " : ""}
                        {ln.url ? (
                          <a href={ln.url} target="_blank" rel="noreferrer">
                            {ln.tracker}:{ln.issueKey}
                          </a>
                        ) : (
                          `${ln.tracker}:${ln.issueKey}`
                        )}
                      </span>
                    ))}
              </span>
            </div>
          </div>
        </article>
      ) : null}
    </div>
  );
}

export function ReleaseBoardPanel({ board }: { board: ReleaseBoard | null }) {
  if (!board) {
    return (
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Delivery board</h3>
        <p className="detail">Board unavailable.</p>
      </article>
    );
  }
  if (!board.apiReachable) {
    return (
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Delivery board</h3>
        <p className="detail">
          Control plane unreachable
          {board.blockedReason ? `, ${board.blockedReason}` : "."}
        </p>
      </article>
    );
  }

  const order: BoardColumn[] = ["built", "building", "planned", "blocked"];
  return (
    <article className="stack-card">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "baseline",
          gap: 10,
          marginBottom: 14,
          flexWrap: "wrap",
        }}
      >
        <div>
          <h3 style={{ margin: 0 }}>Delivery board</h3>
          <p className="detail" style={{ margin: "4px 0 0" }}>
            Built, building, planned, blocked, feature catalog, work items,
            tasks, and deployments.
          </p>
        </div>
        <span className="detail" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>
          {Object.values(board.counts).reduce((a, b) => a + b, 0)} items
        </span>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
          gap: 16,
        }}
      >
        {order.map((col) => (
          <BoardColumnView
            key={col}
            column={col}
            items={board.columns[col] ?? []}
          />
        ))}
      </div>
    </article>
  );
}

export function ReleasePlansList({ plans }: { plans: ReleasePlan[] }) {
  if (!plans.length) {
    return (
      <article className="stack-card">
        <h3 style={{ margin: 0 }}>Release plans</h3>
        <p className="detail">No release plans yet. Create one via the API.</p>
      </article>
    );
  }
  return (
    <article className="stack-card" style={{ padding: 0, overflow: "hidden" }}>
      <div style={{ padding: "13px 16px", borderBottom: "1px solid var(--border)" }}>
        <span className="area-name">Release plans</span>
      </div>
      <div className="hairline-list">
        {plans.map((plan) => (
          <div key={plan.id} className="hairline-row" style={{ alignItems: "flex-start" }}>
            <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
              <span style={{ fontWeight: 600 }}>{plan.name}</span>
              <span className="detail" style={{ fontFamily: "var(--mono)", fontSize: 11 }}>
                {plan.id}
                {plan.targetDate ? `, ${plan.targetDate}` : ""}
                {plan.issueLinks.length
                  ? `, ${plan.issueLinks.length} linked issue${plan.issueLinks.length === 1 ? "" : "s"}`
                  : ""}
              </span>
            </div>
            <span
              style={{
                fontFamily: "var(--mono)",
                fontSize: 11,
                color: tone(plan.status),
                whiteSpace: "nowrap",
              }}
            >
              {plan.status.toUpperCase()}
            </span>
          </div>
        ))}
      </div>
    </article>
  );
}

export function ReleasePlanningPanel({
  snapshot,
  board,
  plans,
}: {
  snapshot: ReleaseStatusSnapshot | null;
  board: ReleaseBoard | null;
  plans?: ReleasePlan[];
}) {
  return (
    <div style={{ display: "grid", gap: 20 }}>
      <ReleaseStatusPanel snapshot={snapshot} />
      <ReleaseBoardPanel board={board} />
      {plans ? <ReleasePlansList plans={plans} /> : null}
    </div>
  );
}
