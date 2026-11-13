"use client";

/**
 * SIGN IN AND WHOSE ACCOUNT (BOSSAPP-846, 847) and TOOLS GRANTED ON REQUEST (BOSSAPP-848), for the
 * MCP drawer.
 *
 *   Whose account   [Shared] [Each person] [Person, else shared] [Each agent]
 *   Shared          Connected as alice@acme.com            [Reconnect]
 *   You             Not signed in                          [Use my own account]
 *   Agents          pm  ✓ Connected                        [Sign in as pm]
 *
 * Sign in opens the provider in a new tab (BOSS's callback closes the loop there); the drawer
 * re-reads when the tab gives focus back. No token ever reaches the browser: the API answers who
 * signed in and whether it still works (needs_reauth -> Reconnect).
 */
import { GatedButton } from "../../components/gated";
import React, { useCallback, useEffect, useState } from "react";

import {
  mcpApi, type AccessGrant, type CredentialPolicy, type CrewMember, type McpCredential, type McpCredentials,
  type McpServer,
} from "../../lib/kit-api";
import { showToast } from "../client-forms";
import { agentName, shortError } from "./kit-copy";

export const POLICY_LABELS: Record<CredentialPolicy, { label: string; tip: string }> = {
  shared: { label: "Shared", tip: "Every call uses the server's one sign-in" },
  per_user: { label: "Each person", tip: "A call uses the account of the person it is for; agents only with that person's grant" },
  per_user_fallback: { label: "Person, else shared", tip: "The person's own account when they have one, otherwise the shared sign-in" },
  per_agent: { label: "Each agent", tip: "Every agent signs in with its own account" },
};

const STATUS_WORDS: Record<string, string> = {
  connected: "Connected", active: "Connected", needs_reauth: "Needs reconnect", not_connected: "Not signed in",
};

/** Open the provider's consent page; BOSS's callback finishes the sign-in in that tab. */
function openProvider(url: string): void {
  if (typeof window !== "undefined") window.open(url, "_blank", "noopener,noreferrer");
}

function who(c: McpCredential): string {
  if (c.subject_kind === "org") return "Shared";
  if (c.subject_kind === "agent") return agentName(c.subject_id);
  return c.mine ? "You" : (c.connected_as ?? "A person");
}

export function useCredentials(projectId: string, serverId: string) {
  const [data, setData] = useState<McpCredentials | null>(null);
  const reload = useCallback(async () => {
    const r = await mcpApi.credentials(projectId, serverId);
    if (r.ok && Array.isArray(r.data?.credentials)) setData(r.data);
  }, [projectId, serverId]);
  useEffect(() => {
    void reload();
    // Back from the provider's tab: the sign-in finished there, so read again.
    const onFocus = () => void reload();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [reload]);
  return { data, reload };
}

export function SignInSection({ projectId, actor, server, crew, onChanged }: {
  projectId: string; actor: string; server: McpServer; crew: CrewMember[]; onChanged: () => void;
}) {
  const { data, reload } = useCredentials(projectId, server.id);
  const [busy, setBusy] = useState(false);
  const oauth = server.auth?.type === "oauth";
  const policy: CredentialPolicy = data?.credential_policy ?? server.credential_policy ?? "shared";
  const creds = data?.credentials ?? [];
  const shared = creds.find((c) => c.subject_kind === "org");
  const mine = creds.find((c) => c.subject_kind === "user" && c.mine);
  const status = data?.auth_status ?? server.auth_status ?? null;

  const signIn = async (subject: "org" | "agent" | "me", role?: string) => {
    setBusy(true);
    const r = subject === "me" ? await mcpApi.signInMine(projectId, actor, server.id)
      : await mcpApi.signIn(projectId, actor, server.id, subject, role);
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    openProvider(r.data.redirect);
    showToast("success", "Finish signing in in the new tab");
  };

  const setPolicy = async (next: CredentialPolicy) => {
    const r = await mcpApi.credentialPolicy(projectId, actor, server.id, next);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Uses: ${POLICY_LABELS[next].label}`);
    await reload();
    onChanged();
  };

  const disconnect = async (c: McpCredential) => {
    const r = await mcpApi.removeCredential(projectId, actor, server.id, c.id, Boolean(c.mine));
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Disconnected ${who(c)}`);
    await reload();
    onChanged();
  };

  const personal = policy === "per_user" || policy === "per_user_fallback";
  return (
    <div className="kit-signin" data-testid="kit-mcp-signin">
      <div className="kit-field">
        <span className="kit-field-label" data-tip="Whose account a call to this server carries">Whose account</span>
        <span className="kit-seg kit-seg-sm" role="radiogroup" aria-label="Whose account">
          {(Object.keys(POLICY_LABELS) as CredentialPolicy[]).map((p) => (
            <GatedButton key={p} permission="mcp_server:review" type="button" role="radio" aria-checked={policy === p}
                         data-tip={POLICY_LABELS[p].tip} data-testid={`kit-mcp-policy-${p}`}
                         onClick={() => void setPolicy(p)}>{POLICY_LABELS[p].label}</GatedButton>
          ))}
        </span>
      </div>
      {oauth && policy !== "per_user" && policy !== "per_agent" ? (
        <div className="kit-field" data-testid="kit-mcp-shared-signin">
          <span className="kit-field-label">Shared</span>
          <span className="kit-chip" data-status={status ?? "not_connected"} data-testid="kit-mcp-auth-status">
            {STATUS_WORDS[status ?? "not_connected"]}
          </span>
          {shared?.connected_as ? <span className="kit-faint" data-user-text>as {shared.connected_as}</span> : null}
          <span className="kit-spacer" />
          <GatedButton permission="mcp_server:review" type="button" className="kit-btn kit-btn-sm kit-btn-primary" disabled={busy}
                       data-testid="kit-mcp-signin-shared" onClick={() => void signIn("org")}>
            {status === "connected" ? "Reconnect" : status === "needs_reauth" ? "Reconnect" : "Sign in"}
          </GatedButton>
        </div>
      ) : null}
      {personal ? (
        <div className="kit-field" data-testid="kit-mcp-my-signin">
          <span className="kit-field-label">You</span>
          <span className="kit-chip" data-status={mine?.status ?? "not_connected"}>
            {STATUS_WORDS[mine?.status ?? "not_connected"]}
          </span>
          {mine?.connected_as ? <span className="kit-faint" data-user-text>as {mine.connected_as}</span> : null}
          <span className="kit-spacer" />
          {oauth ? (
            <GatedButton permission="connection:use_own" type="button" className="kit-btn kit-btn-sm" disabled={busy}
                         data-testid="kit-mcp-signin-mine" onClick={() => void signIn("me")}>
              {mine ? "Reconnect" : "Use my own account"}
            </GatedButton>
          ) : null}
          {mine ? (
            <GatedButton permission="connection:use_own" type="button" className="kit-link" onClick={() => void disconnect(mine)}>
              Disconnect
            </GatedButton>
          ) : null}
        </div>
      ) : null}
      {data?.you && !data.you.ok ? (
        <div className="kit-faint" role="note" data-testid="kit-mcp-you-uses" data-tip={data.you.reason}>
          {data.you.needs_reauth ? "Your calls need a reconnect" : "Your calls have no account yet"}
        </div>
      ) : null}
      {policy === "per_agent" && oauth ? (
        <ul className="kit-agent-grid" data-testid="kit-mcp-agent-signins">
          {crew.map((m) => {
            const c = creds.find((x) => x.subject_kind === "agent" && x.subject_id === m.role_key);
            return (
              <li key={m.role_key} className="kit-agent">
                <span className="kit-agent-name" data-tip={m.display_name ?? m.role_key}>{agentName(m.role_key)}</span>
                <span className="kit-chip" data-status={c?.status ?? "not_connected"}>{STATUS_WORDS[c?.status ?? "not_connected"]}</span>
                <GatedButton permission="mcp_server:review" type="button" className="kit-link" disabled={busy}
                             data-testid={`kit-mcp-signin-agent-${m.role_key}`}
                             onClick={() => void signIn("agent", m.role_key)}>{c ? "Reconnect" : "Sign in"}</GatedButton>
              </li>
            );
          })}
        </ul>
      ) : null}
      {creds.filter((c) => c.subject_kind !== "org" && !c.mine).length ? (
        <details className="kit-more" data-testid="kit-mcp-credentials">
          <summary>Signed in ({creds.length})</summary>
          <ul className="kit-used">
            {creds.map((c) => (
              <li key={c.id}>
                <span data-user-text>{who(c)}</span>
                {c.connected_as && c.subject_kind !== "user" ? <span className="kit-faint" data-user-text> · {c.connected_as}</span> : null}
                <span className="kit-chip" data-status={c.status}>{STATUS_WORDS[c.status]}</span>
                <GatedButton permission="mcp_server:review" type="button" className="kit-link"
                             onClick={() => void disconnect(c)}>Disconnect</GatedButton>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/** The tools agents hold on THIS server because a person approved their Grant card. */
export function AccessGrantsList({ projectId, actor, serverId, onChanged }: {
  projectId: string; actor: string; serverId: string; onChanged: () => void;
}) {
  const [grants, setGrants] = useState<AccessGrant[]>([]);
  const reload = useCallback(async () => {
    const r = await mcpApi.accessGrants(projectId, serverId);
    setGrants(r.ok && Array.isArray(r.data?.grants) ? r.data.grants : []);
  }, [projectId, serverId]);
  useEffect(() => { void reload(); }, [reload]);
  if (grants.length === 0) return null;
  const revoke = async (g: AccessGrant) => {
    const r = await mcpApi.revokeGrant(projectId, actor, g.id);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", `Took ${g.tool} from ${agentName(g.role_key)}`);
    await reload();
    onChanged();
  };
  return (
    <div className="kit-granted" data-testid="kit-mcp-access-grants">
      <span className="kit-field-label" data-tip="Tools an agent asked for and a person approved, pinned to the definition they saw">
        Granted on request
      </span>
      <ul className="kit-used">
        {grants.map((g) => (
          <li key={g.id} data-testid={`kit-mcp-grant-${g.role_key}-${g.tool}`}>
            <span>{agentName(g.role_key)}</span>
            <span className="kit-faint"> · {g.tool}</span>
            <span className="kit-chip" data-tip={g.reason ?? undefined}>{g.policy === "allow" ? "Allow" : "Ask"}</span>
            <GatedButton permission="agent_ability:assign" type="button" className="kit-link"
                         onClick={() => void revoke(g)}>Revoke</GatedButton>
          </li>
        ))}
      </ul>
    </div>
  );
}
