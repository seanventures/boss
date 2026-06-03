import { fetchStatus } from "../lib/status";

export default async function HomePage() {
  const status = await fetchStatus();

  return (
    <main className="shell">
      <div className="frame">
        <section className="hero">
          <span className="eyebrow">Business Operating System Sentinel</span>
          <h1>Executive control for autonomous work.</h1>
          <p>
            Real GitHub and OpenAI integrations are gated behind receipts,
            reservations, and approvals. This shell is wired to the live control
            plane and stays visible even when integrations are blocked.
          </p>
          <span className={`pill${status.apiReachable ? "" : " warn"}`}>
            {status.apiReachable ? "API reachable" : "API unavailable"}
          </span>
        </section>

        <section className="grid">
          <article>
            <h2>Project</h2>
            <div className="metric">{status.projectName}</div>
            <p className="detail">
              {status.orgName} / {status.workspaceName} / {status.projectId}
            </p>
          </article>
          <article>
            <h2>Database</h2>
            <div className="metric">
              {status.databaseConnected ? "Connected" : "Offline"}
            </div>
            <p className="detail">
              Budget and workflow state are sourced from Postgres.
            </p>
          </article>
          <article>
            <h2>Budget State</h2>
            <div className="metric">{status.budgetState}</div>
            <p className="detail">
              Real model and GitHub actions will fail closed without an active
              reservation.
            </p>
          </article>
          <article>
            <h2>Pending Decisions</h2>
            <div className="metric">{status.pendingDecisions}</div>
            <p className="detail">
              Decisions stay explicit when credentials, budget, or approvals are
              missing.
            </p>
          </article>
        </section>

        <section className="grid">
          <article>
            <h2>Open Tasks</h2>
            <div className="metric">{status.openTasks}</div>
            <p className="detail">
              The first dogfood workspace is ready for command intake and
              software-factory execution.
            </p>
          </article>
          <article>
            <h2>Blocking State</h2>
            <div className="metric">
              {status.blockedReason ? "Visible" : "Clear"}
            </div>
            <p className="detail">
              {status.blockedReason ??
                "No bootstrap blocker reported by the control plane."}
            </p>
          </article>
        </section>
      </div>
    </main>
  );
}

