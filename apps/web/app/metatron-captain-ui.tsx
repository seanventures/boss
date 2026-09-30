"use client";

import React, { useEffect, useState, useTransition } from "react";

import { formatDateTime } from "../lib/datetime";
import {
  GOAL_KIND_OPTIONS,
  mapMetatronCaptainSession,
  mapMetatronDriveResult,
  mapMetatronTheater,
  mapMetatronWatchTick,
  unavailableMetatronCaptain,
  type MetatronCaptainState,
} from "../lib/metatron-captain";
import {
  createMetatronCaptainGoalAction,
  loadMetatronCaptainSessionAction,
  loadMetatronTheaterAction,
  proposeMetatronCaptainDriveAction,
  refreshMetatronCaptainWatchAction,
  runMetatronCaptainOverwatchAction,
  runMetatronTheaterAction,
  sendMetatronCaptainMessageAction,
} from "./metatron-captain-actions";
import { SubmitButton } from "./submit-button";

export function MetatronCaptain({
  projectId,
  actor,
}: {
  projectId: string;
  actor: string;
}) {
  const [loading, setLoading] = useState(true);
  const [state, setState] = useState<MetatronCaptainState>(
    unavailableMetatronCaptain("Loading captain session…"),
  );
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    let alive = true;
    setLoading(true);
    loadMetatronCaptainSessionAction(projectId, actor).then(async (result) => {
      if (!alive) return;
      if (result.ok && result.data) {
        const next = mapMetatronCaptainSession(result.data);
        const theater = await loadMetatronTheaterAction(projectId, actor);
        if (!alive) return;
        setState({
          ...next,
          theater:
            theater.ok && theater.data
              ? mapMetatronTheater(theater.data)
              : next.theater,
        });
        setError(null);
      } else {
        setState(
          unavailableMetatronCaptain(
            result.error ?? "Captain session unavailable.",
          ),
        );
        setError(result.error ?? "Captain session unavailable.");
      }
      setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [projectId, actor]);

  const applyFailure = (message: string, unavailable?: boolean) => {
    setError(message);
    if (unavailable) {
      setState(unavailableMetatronCaptain(message));
    }
  };

  return (
    <MetatronCaptainView
      projectId={projectId}
      actor={actor}
      state={state}
      loading={loading}
      error={error}
      pending={pending}
      onSend={(formData) => {
        setError(null);
        startTransition(async () => {
          const result = await sendMetatronCaptainMessageAction(formData);
          if (result.ok && result.data) {
            setState(mapMetatronCaptainSession(result.data));
            return;
          }
          applyFailure(
            result.error ?? "Captain turn failed.",
            result.unavailable,
          );
        });
      }}
      onAddGoal={(formData) => {
        setError(null);
        startTransition(async () => {
          const result = await createMetatronCaptainGoalAction(formData);
          if (result.ok) {
            const session = await loadMetatronCaptainSessionAction(
              projectId,
              actor,
            );
            if (session.ok && session.data) {
              setState(mapMetatronCaptainSession(session.data));
              return;
            }
            applyFailure(
              session.error ?? "Goal saved, but the captain session reload failed.",
              session.unavailable,
            );
            return;
          }
          applyFailure(result.error ?? "Goal create failed.", result.unavailable);
        });
      }}
      onWatch={(formData) => {
        setError(null);
        startTransition(async () => {
          const result = await refreshMetatronCaptainWatchAction(formData);
          if (result.ok && result.data) {
            setState((prev) => ({
              ...prev,
              apiReachable: true,
              watch: mapMetatronWatchTick(result.data) ?? prev.watch,
            }));
            return;
          }
          applyFailure(result.error ?? "Watch failed.", result.unavailable);
        });
      }}
      onDrive={(formData) => {
        setError(null);
        startTransition(async () => {
          const result = await proposeMetatronCaptainDriveAction(formData);
          if (result.ok && result.data) {
            setState((prev) => ({
              ...prev,
              apiReachable: true,
              lastDrive: mapMetatronDriveResult(result.data ?? {}),
            }));
            return;
          }
          applyFailure(
            result.error ?? "Drive proposal failed.",
            result.unavailable,
          );
        });
      }}
      onTheater={(formData) => {
        setError(null);
        startTransition(async () => {
          const result = await runMetatronTheaterAction(formData);
          if (result.ok && result.data) {
            setState((prev) => ({
              ...prev,
              apiReachable: true,
              theater: mapMetatronTheater(result.data ?? {}),
              lastOverwatch: mapMetatronDriveResult(result.data ?? {}),
              watch:
                mapMetatronWatchTick(
                  (result.data?.watch as Record<string, unknown> | undefined) ??
                    result.data,
                ) ?? prev.watch,
            }));
            return;
          }
          applyFailure(
            result.error ?? "Theater command failed.",
            result.unavailable,
          );
        });
      }}
      onOverwatch={(formData) => {
        setError(null);
        startTransition(async () => {
          const result = await runMetatronCaptainOverwatchAction(formData);
          if (result.ok && result.data) {
            setState((prev) => ({
              ...prev,
              apiReachable: true,
              lastOverwatch: mapMetatronDriveResult(result.data ?? {}),
              lastDrive: mapMetatronDriveResult(
                (result.data?.drive as Record<string, unknown> | undefined) ??
                  result.data ??
                  {},
              ),
              watch: mapMetatronWatchTick(
                (result.data?.watch as Record<string, unknown> | undefined) ??
                  result.data,
              ) ?? prev.watch,
            }));
            return;
          }
          applyFailure(
            result.error ?? "Overwatch failed.",
            result.unavailable,
          );
        });
      }}
    />
  );
}

export function MetatronCaptainView({
  projectId,
  actor,
  state,
  loading,
  error,
  pending,
  onSend,
  onAddGoal,
  onWatch,
  onDrive,
  onOverwatch,
  onTheater,
}: {
  projectId: string;
  actor: string;
  state: MetatronCaptainState;
  loading: boolean;
  error: string | null;
  pending: boolean;
  onSend: (formData: FormData) => void;
  onAddGoal: (formData: FormData) => void;
  onWatch: (formData: FormData) => void;
  onDrive: (formData: FormData) => void;
  onOverwatch?: (formData: FormData) => void;
  onTheater?: (formData: FormData) => void;
}) {
  const unavailable = !loading && !state.apiReachable;
  const blocked =
    state.blockedReason ||
    error ||
    "Metatron captain is unavailable. No session, goals, or watch were invented.";

  return (
    <article className="stack-card" data-testid="metatron-captain">
      <div className="section-head">
        <div>
          <span className="eyebrow">Metatron</span>
          <h2>Captain</h2>
        </div>
      </div>
      <p className="detail">
        Community self-hosted captain. Always-on overwatch of systems,
        agents, and captains: observe, measure goals, validate every
        action, apply only safe moves, then verify. Metatron can
        re-prioritize official work and assign more captain force against
        high-level objectives. Decisions stay human. BOSS self-change is a
        PR, never a silent write. Never a second factory job while official
        work is in flight.
      </p>

      {loading ? (
        <p className="loading" role="status" data-testid="metatron-captain-loading">
          Loading captain session…
        </p>
      ) : null}

      {unavailable ? (
        <p
          className="error-banner"
          role="alert"
          data-testid="metatron-captain-unavailable"
          data-empty-state="api-unavailable"
        >
          {blocked}
        </p>
      ) : null}

      {error && !unavailable ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      <div className="agent-chat-log" aria-live="polite" aria-label="Captain conversation">
        {!loading && state.apiReachable && state.messages.length === 0 ? (
          <p className="empty-state" data-testid="metatron-captain-empty-chat">
            No captain messages yet. Send a message to start a persistent
            thread. Try: remember … · set goal … · watch · overwatch · drive.
            Every action is validated before it runs.
          </p>
        ) : null}
        {state.messages.map((message, index) => (
          <article
            className={`agent-message ${message.role}`}
            key={message.id || `${message.role}-${index}`}
          >
            <span>
              {message.role === "assistant"
                ? "Metatron"
                : message.role === "user"
                  ? "You"
                  : message.role}
            </span>
            <p style={{ whiteSpace: "pre-wrap" }}>{message.content}</p>
          </article>
        ))}
      </div>

      <form
        className="composer"
        data-testid="metatron-captain-chat"
        action={onSend}
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        {state.threadId ? (
          <input type="hidden" name="thread_id" value={state.threadId} />
        ) : null}
        <label className="field">
          <span>Message</span>
          <textarea
            className="input"
            name="message"
            rows={3}
            required
            placeholder="Ask the captain, remember a fact, or set a goal."
            disabled={pending}
          />
        </label>
        <SubmitButton className="button" type="submit" disabled={pending}>
          Send to captain
        </SubmitButton>
      </form>

      <h4>Goals</h4>
      {!loading && state.apiReachable && state.goals.length === 0 ? (
        <p className="empty-state" data-testid="metatron-captain-empty-goals">
          No goals registered yet. Add a measurable goal — the next watch will
          score it.
        </p>
      ) : null}
      {state.goals.length > 0 ? (
        <div className="feed-list">
          {state.goals.map((goal) => (
            <div className="feed-row" key={goal.id}>
              <div>
                <h4>{goal.title}</h4>
                <p className="detail">
                  {goal.kind} / {goal.status}
                  {goal.lastScore == null ? "" : ` / score ${goal.lastScore}`}
                  {goal.lastMeasuredAt
                    ? ` / measured ${formatDateTime(goal.lastMeasuredAt)}`
                    : ""}
                </p>
              </div>
            </div>
          ))}
        </div>
      ) : null}

      <form
        className="composer compact"
        data-testid="metatron-captain-goal"
        action={onAddGoal}
      >
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <label className="field">
          <span>Goal title</span>
          <input
            className="input"
            name="title"
            required
            placeholder="Ship working software"
            disabled={pending}
          />
        </label>
        <label className="field">
          <span>Kind</span>
          <select className="input" name="kind" defaultValue="custom" disabled={pending}>
            {GOAL_KIND_OPTIONS.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Description</span>
          <textarea
            className="input"
            name="description"
            rows={2}
            placeholder="Optional measurement notes"
            disabled={pending}
          />
        </label>
        <SubmitButton className="button secondary" type="submit" disabled={pending}>
          Add goal
        </SubmitButton>
      </form>

      <h4>Last watch / overwatch</h4>
      <p className="detail">
        The worker runs overwatch about once a minute: observe, measure,
        validate, apply safe work, verify. Watch alone is observe-only.
      </p>
      {!loading && state.apiReachable && !state.watch ? (
        <p className="empty-state" data-testid="metatron-captain-empty-watch">
          No watch tick yet. Refresh watch or ask the captain to watch.
        </p>
      ) : null}
      {state.watch ? (
        <div data-testid="metatron-captain-watch-findings">
          {state.watch.createdAt ? (
            <p className="detail">
              Last tick {formatDateTime(state.watch.createdAt)}
              {state.watch.receiptId ? ` · receipt ${state.watch.receiptId}` : ""}
            </p>
          ) : null}
          {state.watch.findings.length === 0 ? (
            <p className="detail">Watch recorded no findings.</p>
          ) : (
            <ul className="detail">
              {state.watch.findings.map((finding) => (
                <li key={finding.id}>
                  [{finding.severity}] {finding.title}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      <form data-testid="metatron-captain-watch" action={onWatch}>
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <SubmitButton className="button secondary" type="submit" disabled={pending}>
          Refresh watch
        </SubmitButton>
      </form>
      {onOverwatch ? (
        <form data-testid="metatron-captain-overwatch" action={onOverwatch}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <SubmitButton className="button" type="submit" disabled={pending}>
            Run overwatch now
          </SubmitButton>
        </form>
      ) : null}

      <h4>Theater</h4>
      <p className="detail">
        Fleet overwatch of official runs, captains, playbooks, and swarms.
        Command powers re-prioritize official jobs, staff more agents, and
        loop a playbook. They never invent a pass or start a second factory
        job.
      </p>
      {state.theater ? (
        <div data-testid="metatron-captain-theater">
          <p className="detail">
            official {state.theater.fleet.officialInFlight} · queued{" "}
            {state.theater.fleet.officialQueued} · stalled{" "}
            {state.theater.fleet.factoryStalled} · captains{" "}
            {state.theater.fleet.captainAssignments} · playbooks{" "}
            {state.theater.fleet.playbooks}
          </p>
          {state.theater.note ? (
            <p className="detail">{state.theater.note}</p>
          ) : null}
          {state.theater.findings.length > 0 ? (
            <ul className="detail">
              {state.theater.findings.map((finding) => (
                <li key={finding.id}>
                  [{finding.severity}] {finding.title}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : (
        <p className="empty-state" data-testid="metatron-captain-empty-theater">
          No theater snapshot yet. Command the fleet to observe and apply
          bounded powers.
        </p>
      )}
      {onTheater ? (
        <form data-testid="metatron-captain-theater-run" action={onTheater}>
          <input type="hidden" name="project_id" value={projectId} />
          <input type="hidden" name="actor" value={actor} />
          <SubmitButton className="button" type="submit" disabled={pending}>
            Command the fleet
          </SubmitButton>
        </form>
      ) : null}
      {state.lastOverwatch ? (
        <div data-testid="metatron-captain-overwatch-result">
          <p className="detail">{state.lastOverwatch.note}</p>
        </div>
      ) : null}

      <h4>Drive</h4>
      <p className="detail">
        Every launch is validated first (budget, no duplicate, target exists).
        Decisions and deadletters stay human. BOSS self-change opens a PR.
      </p>
      {state.lastDrive ? (
        <div data-testid="metatron-captain-drive-result">
          <p className="detail">{state.lastDrive.note}</p>
          {state.lastDrive.moves.length === 0 ? (
            <p className="empty-state">No next moves proposed.</p>
          ) : (
            <ul className="detail">
              {state.lastDrive.moves.map((move, index) => (
                <li key={`${move.kind || "move"}-${index}`}>
                  {move.kind}: {move.detail}
                  {move.requiresHuman ? " (needs human)" : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}

      <form data-testid="metatron-captain-drive" action={onDrive}>
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <input type="hidden" name="auto" value="false" />
        <SubmitButton className="button secondary" type="submit" disabled={pending}>
          Propose next moves
        </SubmitButton>
      </form>
      <form data-testid="metatron-captain-drive-auto" action={onDrive}>
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <input type="hidden" name="auto" value="true" />
        <SubmitButton className="button" type="submit" disabled={pending}>
          Launch validated work
        </SubmitButton>
      </form>
    </article>
  );
}
