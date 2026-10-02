"use client";

import { useMemo, useState } from "react";
import {
  ArrowSquareOut,
  Broadcast,
  Clock,
  Gauge,
  GitBranch,
  Lightning,
  PlugsConnected,
  Robot,
  SlidersHorizontal,
  Ticket,
  Warning,
} from "@phosphor-icons/react";
import type {
  RankedWorkItem,
  WorkOrchestrationFeed,
} from "../lib/work-orchestration";
import {
  connectWorkTrackerAction,
  planResourceAllocationsAction,
  setDefaultWorkTrackerAction,
  syncTrackerBindingAction,
  upsertDeliveryBotAction,
  upsertRankedWorkAction,
  upsertResourceQuotaAction,
  upsertStatusChannelAction,
} from "./work-orchestration-actions";
import { ClientRelativeTime } from "./client-relative-time";
import { SubmitButton } from "./submit-button";
import { ActionForm } from "./client-forms";

type Props = {
  feed: WorkOrchestrationFeed;
  actor: string;
};

const SCORE_DRIVERS: Array<[keyof RankedWorkItem, string]> = [
  ["strategic_value", "Strategy"],
  ["customer_impact", "Customer"],
  ["urgency", "Urgency"],
  ["cost_of_delay", "Delay"],
  ["risk_reduction", "Risk"],
  ["dependency_pressure", "Dependency"],
];



function tone(status: string): string {
  const value = status.toLowerCase();
  if (["failed", "blocked", "throttled", "deadlettered"].includes(value)) return "danger";
  if (["pending_create", "queued", "waiting", "retrying"].includes(value)) return "warn";
  if (["active", "running", "leased", "synced", "healthy", "merged"].includes(value)) return "live";
  return "";
}

export function WorkOrchestration({ feed, actor }: Props) {
  const [selectedId, setSelectedId] = useState(feed.workItems[0]?.id);
  const selected = useMemo(
    () => feed.workItems.find((item) => item.id === selectedId) ?? feed.workItems[0],
    [feed.workItems, selectedId],
  );
  const selectedBinding = feed.trackerBindings.find(
    (binding) => binding.work_item_id === selected?.id,
  );
  const selectedAllocation = feed.allocations.find(
    (allocation) =>
      allocation.work_item_id === selected?.id &&
      ["active", "throttled"].includes(allocation.status),
  );
  const capacity = feed.quotas.reduce(
    (total, quota) => total + quota.hard_limit + quota.burst_limit,
    0,
  );
  const used = feed.quotas.reduce((total, quota) => total + quota.used_units, 0);

  if (!feed.apiReachable) {
    return (
      <div className="wo-unavailable">
        <Warning size={24} />
        <div>
          <strong>Work orchestration is unavailable</strong>
          <p>{feed.blockedReason}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="wo-shell">
      <header className="wo-header">
        <div>
          <span className="eyebrow">Allocation control plane</span>
          <h2>Put scarce attention and compute on the work that matters.</h2>
          <p>
            Explainable priority feeds a quota-bounded lease queue.{" "}
            <strong>Plane is the default</strong> work tracker; connect Jira,
            Linear, GitHub Issues, or Azure DevOps when you prefer another system
            of record.
          </p>
        </div>
        <div className="wo-pulse" aria-label={`${feed.summary.active_jobs} active jobs`}>
          <span className="wo-pulse-ring" />
          <span>
            <strong>{feed.summary.active_jobs}</strong>
            active jobs
          </span>
        </div>
      </header>

      <div className="wo-metrics" aria-label="Portfolio summary">
        <div><strong>{feed.summary.ranked_work}</strong><span>ranked</span></div>
        <div><strong>{feed.summary.p0_p1_work}</strong><span>critical</span></div>
        <div><strong>{used}/{capacity || 0}</strong><span>slots used</span></div>
        <div><strong>{feed.summary.tracked_jobs}/{feed.summary.active_jobs}</strong><span>jobs tracked</span></div>
        <div className={feed.summary.throttled_work ? "is-alert" : ""}>
          <strong>{feed.summary.throttled_work}</strong><span>throttled</span>
        </div>
        <div><strong>{feed.summary.active_channels}</strong><span>status channels</span></div>
      </div>

      <div className="wo-ledger">
        <section className="wo-column wo-ranked" aria-label="Ranked portfolio">
          <div className="wo-column-head">
            <div><Lightning size={17} /><span>Ranked portfolio</span></div>
            <small>highest score first</small>
          </div>
          <div className="wo-work-list">
            {feed.workItems.length === 0 ? (
              <p className="wo-empty">Add the first product outcome to begin allocation.</p>
            ) : (
              feed.workItems.map((item, index) => (
                <button
                  className={`wo-work-row${selected?.id === item.id ? " is-selected" : ""}`}
                  key={item.id}
                  onClick={() => setSelectedId(item.id)}
                  type="button"
                >
                  <span className="wo-rank">{String(index + 1).padStart(2, "0")}</span>
                  <span className={`wo-band ${item.priority_band}`}>{item.priority_band}</span>
                  <span className="wo-work-copy">
                    <strong>{item.title}</strong>
                    <small>{item.workload_class} · {item.allocation_status}</small>
                  </span>
                  <span className="wo-score">{item.effective_score.toFixed(1)}</span>
                </button>
              ))
            )}
          </div>
          <details className="wo-editor" data-testid="wo-add-work">
            <summary>Add or rescore work</summary>
            <ActionForm action={upsertRankedWorkAction}>
              <input type="hidden" name="project_id" value={feed.projectId} />
              <input type="hidden" name="actor" value={actor} />
              <div className="wo-form-pair">
                <label>Type<select name="subject_type" defaultValue="feature">
                  <option value="feature">Feature</option>
                  <option value="project">Project</option>
                  <option value="ticket">Ticket</option>
                  <option value="bug">Bug</option>
                  <option value="prd">PRD</option>
                </select></label>
                <label>Reference<input name="subject_id" placeholder="BOSS-270" /></label>
              </div>
              <label>Outcome<input name="title" required placeholder="Customer outcome, not activity" /></label>
              <label>Why now<textarea name="rationale" rows={2} placeholder="Evidence behind this priority" /></label>
              <div className="wo-score-inputs">
                {SCORE_DRIVERS.map(([key, label]) => (
                  <label key={String(key)}>{label}<input name={String(key)} type="number" min={0} max={100} defaultValue={50} /></label>
                ))}
                <label>Confidence<input name="confidence" type="number" min={0} max={100} defaultValue={75} /></label>
                <label>Units<input name="estimated_units" type="number" min={1} defaultValue={1} /></label>
              </div>
              <label>Workload class<input name="workload_class" defaultValue="general" /></label>
              <SubmitButton className="button" type="submit">Score and track</SubmitButton>
            </ActionForm>
          </details>
        </section>

        <section className="wo-column wo-capacity" aria-label="Capacity and job queue">
          <div className="wo-column-head">
            <div><Gauge size={17} /><span>Capacity envelope</span></div>
            <ActionForm action={planResourceAllocationsAction}>
              <input type="hidden" name="project_id" value={feed.projectId} />
              <input type="hidden" name="actor" value={actor} />
              <input type="hidden" name="resource_kind" value="job_concurrency" />
              <SubmitButton type="submit">Rebalance</SubmitButton>
            </ActionForm>
          </div>
          <div className="wo-quota-list">
            {feed.quotas.map((quota) => {
              const limit = quota.hard_limit + quota.burst_limit;
              const fill = limit ? Math.min(100, (quota.used_units / limit) * 100) : 0;
              return (
                <div className="wo-quota" key={quota.id}>
                  <div>
                    <strong>{quota.resource_kind.replaceAll("_", " ")}</strong>
                    <small>{quota.scope_type}:{quota.scope_id}</small>
                  </div>
                  <div className="wo-quota-value">{quota.used_units}/{limit}</div>
                  <div className="wo-meter"><span style={{ width: `${fill}%` }} /></div>
                </div>
              );
            })}
          </div>
          <div className="wo-flow-label"><span>LEASED JOB FLOW</span><i /></div>
          <div className="wo-job-list">
            {feed.jobs.length === 0 ? (
              <p className="wo-empty">No jobs are waiting for or holding capacity.</p>
            ) : (
              feed.jobs.map((job) => (
                <div className="wo-job" key={job.id}>
                  <span className={`wo-status ${tone(job.status)}`} />
                  <div>
                    <strong>{job.work_title ?? job.kind}</strong>
                    <small>{job.kind} · {job.workload_class}</small>
                  </div>
                  <div className="wo-job-state">
                    <span>{job.status}</span>
                    <small>
                      {job.lease_owner ?? (
                        <>
                          ready{" "}
                          <ClientRelativeTime iso={job.run_after} as="span" empty="now" />
                        </>
                      )}
                    </small>
                  </div>
                </div>
              ))
            )}
          </div>
          <details className="wo-editor">
            <summary>Change quota</summary>
            <ActionForm action={upsertResourceQuotaAction}>
              <input type="hidden" name="project_id" value={feed.projectId} />
              <input type="hidden" name="actor" value={actor} />
              <div className="wo-form-pair">
                <label>Scope<select name="scope_type" defaultValue="project">
                  <option value="global">Global</option>
                  <option value="org">Organization</option>
                  <option value="workspace">Workspace</option>
                  <option value="project">Project</option>
                  <option value="agent">Agent</option>
                  <option value="workload">Workload</option>
                </select></label>
                <label>Scope key<input name="scope_id" defaultValue={feed.projectId} required /></label>
              </div>
              <label>Resource<select name="resource_kind" defaultValue="job_concurrency">
                <option value="job_concurrency">Job concurrency</option>
                <option value="model_calls">Model calls</option>
                <option value="tool_calls">Tool calls</option>
                <option value="worker_slots">Worker slots</option>
              </select></label>
              <div className="wo-form-pair">
                <label>Soft<input name="soft_limit" type="number" min={0} defaultValue={2} /></label>
                <label>Hard<input name="hard_limit" type="number" min={1} defaultValue={3} /></label>
              </div>
              <div className="wo-form-pair">
                <label>Reserved<input name="reserved_min" type="number" min={0} defaultValue={0} /></label>
                <label>Burst<input name="burst_limit" type="number" min={0} defaultValue={0} /></label>
              </div>
              <SubmitButton className="button" type="submit">Save envelope</SubmitButton>
            </ActionForm>
          </details>
        </section>

        <section className="wo-column wo-presence" aria-label="Tracker and delivery presence">
          <div className="wo-column-head">
            <div><PlugsConnected size={17} /><span>Delivery presence</span></div>
            <small>tracker + deploy truth</small>
          </div>
          <div className="wo-provider-strip" data-testid="work-tracker-providers">
            {feed.trackerProviders.map((provider) => (
              <div className={provider.is_default ? "is-default" : ""} key={provider.id}>
                <Ticket size={15} />
                <span>{provider.display_name}</span>
                {/* CONNECTED OR NOT, SAID ON THE ROW. All five trackers are seeded as `active`
                    with empty configurations, so a list that said "default" and "available" read
                    as five working trackers while none could create a ticket — and
                    meeting-to-tickets failed at its approved last step (boss-1, 2026-10-02). */}
                <small data-testid={`work-tracker-state-${provider.provider_key}`}>
                  {provider.connected ? "connected" : "not connected"} ·{" "}
                  {provider.is_default ? "default" : "available"} ·{" "}
                  {provider.binding_count} bound
                </small>
                {!provider.is_default ? (
                  <ActionForm action={setDefaultWorkTrackerAction} className="wo-inline-sync">
                    <input type="hidden" name="project_id" value={feed.projectId} />
                    <input type="hidden" name="actor" value={actor} />
                    <input type="hidden" name="provider_key" value={provider.provider_key} />
                    <SubmitButton className="button secondary" type="submit">
                      Make default
                    </SubmitButton>
                  </ActionForm>
                ) : null}
              </div>
            ))}
          </div>
          <details className="wo-editor" data-testid="work-tracker-connect">
            <summary>Connect work tracker (Plane default · Jira · Linear · GitHub · Azure DevOps)</summary>
            <ActionForm action={connectWorkTrackerAction}>
              <input type="hidden" name="project_id" value={feed.projectId} />
              <input type="hidden" name="actor" value={actor} />
              <label>
                System
                <select name="provider_key" defaultValue="plane" required>
                  <option value="plane">Plane (default)</option>
                  <option value="jira">Jira Cloud</option>
                  <option value="linear">Linear</option>
                  <option value="github_issues">GitHub Issues</option>
                  <option value="azure_devops">Azure DevOps</option>
                </select>
              </label>
              <p className="wo-empty">
                Fill the fields for your system. Leave blank fields unused.
                Tokens are stored on the project config (prefer env SecretRefs in production).
              </p>
              <div className="wo-form-pair">
                <label>API / site URL<input name="api_base_url" placeholder="Plane URL or Jira site" /></label>
                <label>Base URL (Jira)<input name="base_url" placeholder="https://x.atlassian.net" /></label>
              </div>
              <div className="wo-form-pair">
                <label>Workspace slug (Plane)<input name="workspace_slug" placeholder="workspace" /></label>
                <label>Plane project UUID<input name="project_id_external" placeholder="3f2a9c1e-…" /></label>
              </div>
              <div className="wo-form-pair">
                <label>Jira email<input name="email" type="email" placeholder="you@company.com" /></label>
                <label>Jira project key<input name="project_key" placeholder="BOSS" /></label>
              </div>
              <div className="wo-form-pair">
                <label>Linear team id<input name="team_id" placeholder="uuid" /></label>
                <label>Linear team key<input name="team_key" placeholder="ENG" /></label>
              </div>
              <div className="wo-form-pair">
                <label>GitHub owner<input name="owner" placeholder="acme" /></label>
                <label>GitHub repo<input name="repo" placeholder="product" /></label>
              </div>
              <div className="wo-form-pair">
                <label>Azure org<input name="organization" placeholder="my-org" /></label>
                <label>Azure project<input name="project" placeholder="Product" /></label>
              </div>
              <label>
                API token / PAT
                <input name="api_token" type="password" autoComplete="off" placeholder="••••••••" />
              </label>
              <label className="check-row">
                <input type="checkbox" name="set_as_default" value="true" />
                <span>Set as project default tracker</span>
              </label>
              <label className="check-row">
                <input type="checkbox" name="skip_test" value="true" />
                <span>Skip live connection test</span>
              </label>
              <SubmitButton className="button" type="submit">
                Connect tracker
              </SubmitButton>
            </ActionForm>
          </details>
          <div className="wo-binding-list">
            {feed.trackerBindings.slice(0, 8).map((binding) => (
              <div className="wo-binding" key={binding.id}>
                <span className={`wo-status ${tone(binding.sync_status)}`} />
                <div>
                  <strong>{binding.external_key ?? binding.work_title}</strong>
                  <small>{binding.provider_name} · {binding.external_state ?? binding.sync_status}</small>
                </div>
                {binding.external_url ? (
                  <a href={binding.external_url} target="_blank" rel="noreferrer" aria-label={`Open ${binding.external_key ?? binding.work_title}`}>
                    <ArrowSquareOut size={16} />
                  </a>
                ) : <Clock size={15} />}
              </div>
            ))}
          </div>
          <div className="wo-flow-label"><span>LIVE STATUS RAIL</span><i /></div>
          <div className="wo-presence-list">
            {feed.presenceEvents.length === 0 ? (
              <p className="wo-empty">PR, deployment, and bot events will stream here.</p>
            ) : (
              feed.presenceEvents.slice(0, 8).map((event) => (
                <div className="wo-event" key={event.id}>
                  <span>{event.provider === "github" ? <GitBranch size={15} /> : <Broadcast size={15} />}</span>
                  <div>
                    <strong>{event.summary}</strong>
                    <small>
                      {event.bot_name ?? event.provider} ·{" "}
                      <ClientRelativeTime iso={event.created_at} as="span" />
                    </small>
                  </div>
                </div>
              ))
            )}
          </div>
          <details className="wo-editor">
            <summary>Connect bot or status channel</summary>
            <ActionForm action={upsertDeliveryBotAction}>
              <input type="hidden" name="project_id" value={feed.projectId} />
              <input type="hidden" name="actor" value={actor} />
              <div className="wo-form-pair">
                <label>Provider<select name="provider"><option value="mattermost">Mattermost</option><option value="github">GitHub</option><option value="plane">Plane</option></select></label>
                <label>Agent key<input name="agent_key" defaultValue="product_manager" required /></label>
              </div>
              <label>Bot display name<input name="display_name" defaultValue="BOSS Product Manager" required /></label>
              <label>External identity<input name="external_identity" placeholder="boss-product-manager" /></label>
              <SubmitButton className="button secondary" type="submit">Register bot identity</SubmitButton>
            </ActionForm>
            <ActionForm action={upsertStatusChannelAction}>
              <input type="hidden" name="project_id" value={feed.projectId} />
              <input type="hidden" name="actor" value={actor} />
              <div className="wo-form-pair">
                <label>Provider<select name="provider"><option value="mattermost">Mattermost</option><option value="github">GitHub</option><option value="plane">Plane</option></select></label>
                <label>Channel key<input name="channel_key" defaultValue="delivery-status" required /></label>
              </div>
              <label>Name<input name="display_name" defaultValue="Delivery status" required /></label>
              <label>Purpose<input name="purpose" defaultValue="Ticket, PR, and deployment status" required /></label>
              <label>Events<textarea name="event_kinds" rows={2} defaultValue={"ticket.updated\ndeployment.status\npull_request.status"} /></label>
              <SubmitButton className="button secondary" type="submit">Save status channel</SubmitButton>
            </ActionForm>
          </details>
        </section>
      </div>

      {selected ? (
        <aside className="wo-inspector" aria-label="Selected work details">
          <div className="wo-inspector-title">
            <span className={`wo-band ${selected.priority_band}`}>{selected.priority_band}</span>
            <div><strong>{selected.title}</strong><small>{selected.subject_type}:{selected.subject_id}</small></div>
            <span className="wo-score-large">{selected.effective_score.toFixed(1)}</span>
          </div>
          <div className="wo-driver-grid">
            {SCORE_DRIVERS.map(([key, label]) => (
              <div key={String(key)}><span>{label}</span><strong>{String(selected[key])}</strong><i><b style={{ width: `${Number(selected[key])}%` }} /></i></div>
            ))}
          </div>
          <div className="wo-inspector-meta">
            <div><SlidersHorizontal size={16} /><span>{selected.rationale || "No priority rationale recorded."}</span></div>
            <div><Gauge size={16} /><span>{selectedAllocation ? `${selectedAllocation.granted_units}/${selectedAllocation.requested_units} units · ${selectedAllocation.reason}` : "No active allocation"}</span></div>
            <div><Ticket size={16} /><span>{selectedBinding ? `${selectedBinding.provider_name} · ${selectedBinding.external_key ?? selectedBinding.sync_status}` : "Tracker binding required"}</span></div>
            <div><Robot size={16} /><span>{selected.owner_actor ?? "Unassigned"} · {selected.workload_class}</span></div>
          </div>
          {selectedBinding && selectedBinding.sync_status !== "synced" ? (
            <ActionForm action={syncTrackerBindingAction} className="wo-inline-sync">
              <input type="hidden" name="project_id" value={feed.projectId} />
              <input type="hidden" name="actor" value={actor} />
              <input type="hidden" name="work_item_id" value={selected.id} />
              <input type="hidden" name="provider_key" value={selectedBinding.provider_key} />
              <input name="external_key" placeholder="BOSS-270" aria-label="External ticket key" />
              <input name="external_issue_id" placeholder="External issue id (optional)" aria-label="External issue ID" />
              <SubmitButton className="button" type="submit">Confirm ticket link</SubmitButton>
            </ActionForm>
          ) : null}
        </aside>
      ) : null}
    </div>
  );
}
