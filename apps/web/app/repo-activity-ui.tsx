import { changeHref, linkTarget, repoHref } from "../lib/repo-links";
import { ClientRelativeTime } from "./client-relative-time";
import type { RepoActivity, RepoRecentRun } from "../lib/repo-activity";

/** The GitHub area used to show only "is a connection configured", which tells a developer
 *  nothing about their own repo. This shows what BOSS actually DID in it: how much work ran,
 *  how much shipped, how often it lands first time, how long it takes, and what it cost. */

function fmtDuration(minutes: number | null): string {
  if (minutes === null || Number.isNaN(minutes)) return "—";
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  return hours < 24 ? `${hours.toFixed(1)}h` : `${(hours / 24).toFixed(1)}d`;
}

function statusTone(status: string): string {
  if (status === "completed") return "ok";
  if (status === "failed" || status === "cancelled") return "bad";
  if (status === "running" || status === "repairing") return "live";
  return "idle";
}

function Kpi({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="cc-kpi ra-kpi" data-tone={tone ?? "idle"}>
      <span className="cc-kpi-n">{value}</span>
      <span className="cc-kpi-l">{label}</span>
      {hint ? <span className="ra-kpi-hint">{hint}</span> : null}
    </div>
  );
}

function RepoCard({ repo }: { repo: RepoActivity }) {
  const shipped = repo.prs_merged;
  // An unknown merge count must read as unknown. A zero here would look like
  // "BOSS never lands anything", which is a very different (and wrong) claim.
  const mergedValue = shipped === null || shipped === undefined ? "—" : String(shipped);
  const mergedHint =
    shipped === null || shipped === undefined
      ? "merge state unavailable"
      : `${repo.prs_opened} opened`;

  return (
    <article className="ra-repo">
      <header className="ra-repo-head">
        <div>
          <span className="area-name">Repository</span>
          <h3 className="ra-repo-name">
            <a
              href={repoHref(repo.full_name) ?? undefined}
              {...linkTarget(repoHref(repo.full_name))}
            >
              {repo.full_name}
            </a>
          </h3>
        </div>
        <div className="ra-repo-meta">
          <span>
            last activity{" "}
            {repo.last_activity_at ? (
              <ClientRelativeTime iso={repo.last_activity_at} empty="—" />
            ) : (
              "—"
            )}
          </span>
          <span className="ra-window">last {repo.window_days}d</span>
        </div>
      </header>

      <div className="cc-kpis ra-kpis">
        <Kpi
          label="Runs"
          value={String(repo.runs_total)}
          hint={repo.runs_active > 0 ? `${repo.runs_active} active now` : "none active"}
          tone={repo.runs_active > 0 ? "live" : "idle"}
        />
        <Kpi
          label="PRs merged"
          value={mergedValue}
          hint={mergedHint}
          tone={shipped ? "ok" : "idle"}
        />
        <Kpi
          label="Success rate"
          value={repo.success_rate === null ? "—" : `${repo.success_rate}%`}
          hint={`${repo.runs_completed} done, ${repo.runs_failed} failed`}
          tone={
            repo.success_rate === null ? "idle" : repo.success_rate >= 60 ? "ok" : "bad"
          }
        />
        <Kpi
          label="First-pass"
          value={repo.first_pass_rate === null ? "—" : `${repo.first_pass_rate}%`}
          hint="shipped without a retry"
          tone={
            repo.first_pass_rate === null
              ? "idle"
              : repo.first_pass_rate >= 50
                ? "ok"
                : "bad"
          }
        />
        <Kpi
          label="Median to PR"
          value={fmtDuration(repo.median_minutes_to_pr)}
          hint="task start → PR open"
        />
        <Kpi
          label="Spend"
          value={`$${repo.spend_usd.toFixed(2)}`}
          hint={
            repo.spend_per_pr_usd === null
              ? "no PRs yet"
              : `$${repo.spend_per_pr_usd.toFixed(2)} per PR`
          }
        />
      </div>

      {repo.recent_runs.length > 0 ? (
        <table className="ra-runs">
          <thead>
            <tr>
              <th>Recent work</th>
              <th>Status</th>
              <th>PR</th>
              <th>Took</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {repo.recent_runs.map((run: RepoRecentRun) => (
              <tr key={run.id}>
                <td className="ra-run-title" title={run.title}>
                  {run.title}
                </td>
                <td>
                  <span className="ra-dot" data-tone={statusTone(run.status)} />
                  {run.status}
                </td>
                <td>
                  {run.pr && run.pr_url ? (
                    <a href={changeHref(run.pr_url)} {...linkTarget(run.pr_url)}>
                      #{run.pr}
                    </a>
                  ) : (
                    "—"
                  )}
                </td>
                <td>{fmtDuration(run.minutes)}</td>
                <td>${run.cost_usd.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </article>
  );
}

export function RepoActivityPanel({ repos }: { repos: RepoActivity[] }) {
  return (
    <section className="panel-stack" data-area="github" aria-label="Repository activity">
      <div className="section-head">
        <div>
          <span className="area-name">Repository Activity</span>
          <h2>What BOSS shipped in your repositories.</h2>
        </div>
        <p className="status-meta">
          Runs, merges, first-pass rate and spend, measured from the work BOSS actually ran.
        </p>
      </div>
      {repos.length === 0 ? (
        <p className="status-meta">
          No runs recorded yet for this project&apos;s repositories. Once BOSS runs a coding
          task, its activity appears here.
        </p>
      ) : (
        <div className="ra-repos">
          {repos.map((repo) => (
            <RepoCard key={repo.full_name} repo={repo} />
          ))}
        </div>
      )}
    </section>
  );
}
