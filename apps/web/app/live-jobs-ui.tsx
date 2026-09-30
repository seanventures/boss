import type { LiveJobsFeed } from "../lib/live-jobs";

export function LiveJobsCard({ feed }: { feed: LiveJobsFeed }) {
  return (
    <article className="stack-card" data-testid="live-jobs">
      <h3>Live jobs</h3>
      <p className="detail">
        Newest non-terminal work first. Deadletters stay on the COO queue, not
        this card.
      </p>
      {!feed.apiReachable ? (
        <p className="detail">{feed.blockedReason ?? "Jobs API is unavailable."}</p>
      ) : feed.items.length === 0 ? (
        <p className="detail">No live jobs (queued, leased, or running).</p>
      ) : (
        <div className="feed-list">
          {feed.items.map((job) => (
            <div className="feed-row" key={job.id} data-testid={`live-job-${job.id}`}>
              <div>
                <h4>{job.kind}</h4>
                <p className="detail">
                  {job.status}
                  {job.attempt != null ? ` · attempt ${job.attempt}` : ""}
                </p>
              </div>
              <code className="mono">{job.id}</code>
            </div>
          ))}
        </div>
      )}
    </article>
  );
}
