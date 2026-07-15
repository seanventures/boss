"use client";

import { useActionState } from "react";

import type {
  AgentAction,
  AgentMessage,
  AgentPendingConfirmation,
} from "@boss/api-client";

import { agentChatAction, type ActionResult } from "./actions";

type AgentTurnData = {
  thread_id: string;
  reply: string;
  actions: AgentAction[];
  pending_confirmation?: AgentPendingConfirmation | null;
  messages: AgentMessage[];
};

function turnData(state: ActionResult | null): AgentTurnData | null {
  if (!state?.ok || !state.data || typeof state.data.thread_id !== "string") {
    return null;
  }
  return state.data as unknown as AgentTurnData;
}

function actionLabel(action: AgentAction): string {
  return action.kind.replaceAll("_", " ");
}

export function AgentChat({ projectId, actor }: { projectId: string; actor: string }) {
  const [state, submit, pending] = useActionState(agentChatAction, null);
  const turn = turnData(state);
  const messages = turn?.messages ?? [];
  const pendingPlan = turn?.pending_confirmation ?? null;

  return (
    <div className="agent-chat-shell">
      <div className="agent-chat-intro">
        <span className="eyebrow">BOSS Agent</span>
        <h2>Describe the outcome. BOSS picks the governed path.</h2>
        <p>
          Start code, research, a decision meeting, a PRD panel, or a phased program
          from one conversation. Multi-task plans always pause for your confirmation.
        </p>
      </div>

      <div className="agent-chat-log" aria-live="polite" aria-label="Agent conversation">
        {messages.length === 0 ? (
          <div className="agent-chat-empty">
            <p>Try “Fix the typo in the footer” or “Research auth providers, then build login.”</p>
          </div>
        ) : (
          messages.map((message) => (
            <article className={`agent-message ${message.role}`} key={message.id}>
              <span>{message.role === "assistant" ? "BOSS" : "You"}</span>
              <p>{message.content}</p>
              {message.actions.length > 0 ? (
                <div className="agent-action-row">
                  {message.actions.map((action, index) =>
                    action.href ? (
                      <a href={action.href} key={`${message.id}-${action.kind}-${index}`}>
                        {actionLabel(action)} ↗
                      </a>
                    ) : (
                      <em key={`${message.id}-${action.kind}-${index}`}>
                        {actionLabel(action)}
                      </em>
                    ),
                  )}
                </div>
              ) : null}
            </article>
          ))
        )}
      </div>

      {pendingPlan ? (
        <aside className="agent-plan-gate" aria-label="Plan confirmation">
          <div>
            <span className="eyebrow">Human gate</span>
            <h3>{pendingPlan.phase_count} sequenced tasks are ready to dispatch</h3>
            <ol>
              {pendingPlan.phases.map((phase, index) => (
                <li key={`${pendingPlan.plan_id}-${index}`}>
                  {String(phase.title ?? `Phase ${index + 1}`)}
                </li>
              ))}
            </ol>
          </div>
          <form action={submit}>
            <input type="hidden" name="project_id" value={projectId} />
            <input type="hidden" name="actor" value={actor} />
            <input type="hidden" name="thread_id" value={turn?.thread_id ?? ""} />
            <input type="hidden" name="confirm_plan_id" value={pendingPlan.plan_id} />
            <button className="button" type="submit" disabled={pending}>
              {pending ? "Dispatching…" : "Confirm and dispatch"}
            </button>
          </form>
        </aside>
      ) : null}

      <form className="agent-composer" action={submit}>
        <input type="hidden" name="project_id" value={projectId} />
        <input type="hidden" name="actor" value={actor} />
        <input type="hidden" name="thread_id" value={turn?.thread_id ?? ""} />
        <label htmlFor="agent-message">Message BOSS</label>
        <div>
          <textarea
            id="agent-message"
            key={messages.length}
            name="message"
            placeholder="What should BOSS accomplish?"
            rows={3}
            disabled={pending}
            required
          />
          <button className="button" type="submit" disabled={pending}>
            {pending ? "Routing…" : "Send"}
          </button>
        </div>
      </form>
      {state && !state.ok ? <p className="agent-chat-error">{state.error}</p> : null}
    </div>
  );
}
