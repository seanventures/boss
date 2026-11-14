// @vitest-environment happy-dom
/**
 * TICKET SCREENS (BOSSAPP-1047 Phase C), clicked, with the requests they send:
 *   T6  the work drawer's Linked ticket: steps, the "ticket changed" card (Include it / Next
 *       ticket), the "Written to Jira" log and Retry; loading, error in words, denied, no ticket.
 *   PR  the change page's ticket badge.
 *   T4  Ask BOSS's ticket brief: built from the turn's ticket_get, workflow chips, one Start.
 *   Settings › Ticket system: the default project, saved as mappings; mirror read-only; empty.
 *   Account › Ticket accounts: Connect with Atlassian, Disconnect, the email-mismatch confirm.
 */
import React, { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LinkedTicket, TicketSyncBadge } from "../app/projects/linked-ticket";
import { TicketBrief, ticketRefOf } from "../app/tickets/ticket-brief";
import { TicketSystemCard, withDefault } from "../app/projects/ticket-system-settings";
import { TicketAccounts } from "../app/account/ticket-accounts";
import { $$, choose, click, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const settle = async (n = 10) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };

const P = "p1";
const TICKET = {
  provider: "jira", connection_id: "tc_1", key: "LEND-142", id: "10042", url: "https://np.atlassian.net/browse/LEND-142",
  title: "Second reviewer when debt-to-income is over 45%", type: "story", status: "To Do", status_category: "todo", priority: "High",
  labels: ["boss"], parent_key: "LEND-120", screen: { verdict: "clean", reasons: [] }, untrusted: false, in_boss: null,
  body: "Loans with DTI above 45% must not be approved by one person.",
  acceptance_criteria: ["DTI > 45% → Needs second review", "The second reviewer can't be the first approver", "Audit log records both"],
  comments: [{ author: "a", at: "", body: "x" }, { author: "b", at: "", body: "y" }], attachments: [{ name: "dti-policy.pdf", url: "" }],
};
const CONNECTION = {
  id: "tc_1", project_id: P, provider: "jira", label: "Jira", icon: "jira", status: "connected", base_url: "https://np.atlassian.net",
  fields: { email: "bot@np.example" }, account: "BOSS Bot", last4: "abcd", fingerprint: "f", capabilities: { read: true },
  settings: { mappings: [{ external_project: "OPS", boss_project_id: "p2", default: true }, { external_project: "LEND", boss_project_id: P, default: true }],
              write_back: { comment: true, transition: false, link_pr: true }, status_map: { todo: "To Do", doing: "Doing", review: "In Review", done: "Done" },
              pickup: { label: "boss", mode: "ask" }, inbound: { mode: "webhook", poll_minutes: 5 }, project_keys: ["LEND", "OPS", "CARD"] },
  managed_from: null, mirrors: [], bindings: { total: 2, paused: 0 },
};
const TIMELINE = {
  work_item_id: "wi_1", ticket: { provider: "jira", key: "LEND-142", url: "https://np.atlassian.net/browse/LEND-142", connection_id: "tc_1" },
  sync_status: "failed", sync_error: null, last_synced_at: "2026-11-13T14:02:00Z", failed: 1,
  entries: [
    { receipt_id: "r1", at: "2026-11-13T14:02:00Z", action: "started", op: "transition", label: "Moved", summary: "Moved to In Progress", status: "succeeded" },
    { receipt_id: "r2", at: "2026-11-13T14:02:00Z", action: "started", op: "comment", label: "Commented", summary: "BOSS started a minor feature.", status: "succeeded" },
    { receipt_id: "r3", at: "2026-11-13T14:20:00Z", action: "pr_opened", op: "link", label: "Linked", summary: "Linked https://github.com/x/y/pull/9", status: "failed", reason: "Jira said 403", retryable: true },
  ],
};
const CHANGED = { id: "dec_9", status: "pending", question: "LEND-142 changed while BOSS was building it.",
                  recommendation: { action: "ticket_changed_mid_run", work_item_id: "wi_1", run_workflow_id: "wfr_1", added: ["email the branch manager"] } };

// The decision inbox is read through its POST twin (`/load`): a read, not a press.
const posts = (calls: Call[]) => calls.filter((c) => c.method !== "GET" && c.path !== "/v1/decisions/inbox/load");

beforeEach(() => { try { window.localStorage.clear(); } catch { /* none */ } });
afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("T6 · the work drawer's linked ticket", () => {
  function engine(over: { sync?: unknown; syncStatus?: number; decisions?: unknown[] } = {}) {
    return stubFetch((c) => {
      if (c.path === "/v1/workflow-runs/wfr_1") return { body: { id: "wfr_1", input: { work_item_id: "wi_1" } } };
      if (c.path === "/v1/work-items/wi_1/ticket-sync" && c.method === "GET") return { status: over.syncStatus ?? 200, body: over.sync ?? TIMELINE };
      if (c.path === "/v1/decisions/inbox/load") return { body: { project_id: P, items: over.decisions ?? [CHANGED] } };
      if (c.path === "/v1/work-items/wi_1/ticket-sync/retry") return { body: { ok: true, retried: [{ status: "succeeded" }] } };
      if (c.path.startsWith("/v1/decisions/dec_9/")) return { body: { ok: true } };
      return undefined;
    });
  }

  it("shows steps, the changed card and the log; Retry, Include it and Next ticket send their one write", async () => {
    const calls = engine();
    await mountAs(<LinkedTicket projectId={P} runId="wfr_1" />, "owner");
    await settle();
    expect(tid("tk-linked-key")!.textContent).toContain("LEND-142");
    expect(tid("tk-step-started")!.getAttribute("data-state")).toBe("done");
    expect(tid("tk-step-pr_opened")!.getAttribute("data-state")).toBe("now");
    expect(tid("tk-linked-status")!.textContent).toBe("Write-back failed");
    expect($$('[data-testid="tk-log-line"]')).toHaveLength(3);
    expect($$('[data-testid="tk-log-line"]')[2].textContent).toContain("failed: Jira said 403");
    expect(tid("tk-changed")!.textContent).toContain("“email the branch manager”");

    await click(tid("tk-linked-retry"));
    expect(posts(calls).map((c) => c.path)).toEqual(["/v1/work-items/wi_1/ticket-sync/retry"]);
    expect(tid("tk-linked-said")!.textContent).toBe("Sent again.");

    await click(tid("tk-changed-include"));
    expect(posts(calls)[1]).toMatchObject({ method: "POST", path: "/v1/decisions/dec_9/approve" });
    expect(tid("tk-linked-said")!.textContent).toContain("include the new criteria");
  });

  it("Next ticket rejects the card", async () => {
    const calls = engine();
    await mountAs(<LinkedTicket projectId={P} runId="wfr_1" />, "owner");
    await settle();
    await click(tid("tk-changed-next"));
    expect(posts(calls).map((c) => c.path)).toEqual(["/v1/decisions/dec_9/reject"]);
  });

  it("a viewer sees the log but cannot press Retry or answer the card", async () => {
    const calls = engine();
    await mountAs(<LinkedTicket projectId={P} runId="wfr_1" />, "viewer");
    await settle();
    expect(tid("tk-linked-retry")!.getAttribute("aria-disabled")).toBe("true");
    await click(tid("tk-linked-retry"));
    await click(tid("tk-changed-include"));
    expect(posts(calls)).toEqual([]);
  });

  it("a refused read says why in words; a typed error says its message", async () => {
    engine({ syncStatus: 403, sync: { detail: "Needs “View work items”, ask an Admin." } });
    await mountAs(<LinkedTicket projectId={P} runId="wfr_1" />, "owner");
    await settle();
    expect(tid("tk-linked-denied")!.textContent).toContain("Needs “View work items”");
    unmount();
    engine({ syncStatus: 424, sync: { detail: { error: "auth_failed", message: "" } } });
    await mountAs(<LinkedTicket projectId={P} runId="wfr_1" />, "owner");
    await settle();
    expect(tid("tk-linked-error")!.textContent).toContain("refused BOSS's sign-in");
    stubFetch((c) => (c.path === "/v1/work-items/wi_1/ticket-sync" ? { body: TIMELINE } : undefined));
    await click(tid("tk-linked-retry"));
    expect(tid("tk-linked-key")).not.toBeNull();
  });

  it("work that did not come from a ticket shows nothing; loading shows a skeleton", async () => {
    engine({ sync: { ...TIMELINE, ticket: null, entries: [] }, decisions: [] });
    const host = await mountAs(<LinkedTicket projectId={P} runId="wfr_1" />, "owner");
    await settle();
    expect(host.textContent).toBe("");
    unmount();
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    await mountAs(<LinkedTicket projectId={P} runId="wfr_1" />, "owner");
    expect(tid("tk-linked-loading")).not.toBeNull();
  });

  it("the change page's badge names the ticket and its sync", async () => {
    engine({ sync: { ...TIMELINE, sync_status: "synced", failed: 0 } });
    await mountAs(<TicketSyncBadge runId="wfr_1" />, "owner");
    await settle();
    expect(tid("tk-pr-badge")!.textContent).toContain("LEND-142");
    expect(tid("tk-pr-badge")!.textContent).toContain("Jira: In Progress ✓");
    expect(tid("tk-pr-badge")!.getAttribute("href")).toBe(TIMELINE.ticket.url);
  });
});

describe("T4 · Ask BOSS's ticket brief", () => {
  it("is built from the turn's ticket_get, never from a key alone when calls are given", () => {
    expect(ticketRefOf([{ tool: "ticket_get", ok: true, args: { ref: "LEND-142", connection_id: "tc_1" } }])).toEqual({ ref: "LEND-142", connectionId: "tc_1" });
    expect(ticketRefOf([{ tool: "ticket_get", ok: false, args: { ref: "LEND-1" } }])).toBeNull();
    expect(ticketRefOf([], "Implement LEND-7 next")).toEqual({ ref: "LEND-7", connectionId: "" });
  });

  it("reads the ticket, picks the workflow from its type, and Start imports then starts it", async () => {
    const calls = stubFetch((c) => {
      if (c.path === "/v1/tickets/resolve") return { body: { project_id: P, tickets: [TICKET], errors: [], refs: [{}] } };
      if (c.path === "/v1/ticket-systems") return { body: { project_id: P, connections: [CONNECTION] } };
      if (c.path === `/v1/projects/${P}/tickets/import`) return { body: { imported: [{ key: "LEND-142", work_item_id: "wi_7" }], already_in_boss: [] } };
      if (c.path === "/v1/work-items/wi_7/start-from-ticket") return { body: { work_item_id: "wi_7", workflow_key: "major_feature", run_id: "wfr_7" } };
      return undefined;
    });
    await mountAs(<TicketBrief projectId={P} calls={[{ tool: "ticket_get", ok: true, args: { ref: "LEND-142" } }]} />, "owner");
    await settle();
    expect(tid("tk-brief-card")!.textContent).toContain("Second reviewer");
    expect(tid("tk-brief-card-ac")!.textContent).toContain("Acceptance criteria · 3");
    expect(tid("tk-brief-card-also")!.textContent).toBe("Also read: 2 comments · 1 attachment (dti-policy.pdf) · parent epic LEND-120");
    expect(tid("tk-brief-flow-minor_feature")!.getAttribute("aria-pressed")).toBe("true");
    // The write-back chips say what the connection does: comments on, moves off here.
    expect(tid("tk-brief-wb-comment")!.getAttribute("aria-pressed")).toBe("true");
    expect(tid("tk-brief-wb-move")!.getAttribute("aria-pressed")).toBe("false");
    expect(tid("tk-brief-wb-move")!.textContent).toContain("Move to Doing");
    await click(tid("tk-brief-flow-fast_fix"));
    expect(tid("tk-brief-flow-fast_fix")!.getAttribute("aria-pressed")).toBe("true");
    await click(tid("tk-brief-flow-major_feature"));
    expect(tid("tk-brief-start")!.textContent).toContain("Start major feature");
    await click(tid("tk-brief-start"));
    expect(posts(calls).map((c) => [c.path, c.body])).toEqual([
      [`/v1/projects/${P}/tickets/import`, { connection_id: "tc_1", items: [{ key: "LEND-142", with_children: false }] }],
      ["/v1/work-items/wi_7/start-from-ticket", { project_id: P, workflow_key: "major_feature" }],
    ]);
    expect(tid("tk-brief-said")!.textContent).toBe("Started major feature from LEND-142.");
  });

  it("a ticket the system refuses says so in words, with Try again; a viewer's Start is greyed", async () => {
    let refuse = true;
    const calls = stubFetch((c) => {
      if (c.path === "/v1/tickets/resolve") return refuse ? { status: 429, body: { detail: { error: "rate_limited", message: "" } } } : { body: { tickets: [TICKET], errors: [] } };
      if (c.path === "/v1/ticket-systems") return { body: { connections: [CONNECTION] } };
      return undefined;
    });
    await mountAs(<TicketBrief projectId={P} text="Implement LEND-142" />, "viewer");
    await settle();
    expect(tid("tk-brief-error")!.textContent).toContain("slow down");
    refuse = false;
    await click(tid("tk-brief-retry"));
    expect(tid("tk-brief-start")!.getAttribute("aria-disabled")).toBe("true");
    await click(tid("tk-brief-start"));
    expect(posts(calls)).toEqual([]);
  });
});

describe("Project settings › Ticket system", () => {
  it("lists the connection and saves this project's default as mappings", async () => {
    const calls = stubFetch((c) => {
      if (c.path === "/v1/ticket-systems" && c.method === "GET") return { body: { connections: [CONNECTION] } };
      if (c.method === "PATCH") return { body: { connection: CONNECTION } };
      return undefined;
    });
    await mountAs(<TicketSystemCard projectId={P} />, "owner");
    await settle();
    expect(tid("px-set-tickets-manage")!.getAttribute("href")).toBe("?area=setup&focus=ticket_systems");
    expect((tid("px-set-ticket-pick-tc_1") as HTMLSelectElement).value).toBe("LEND");
    await choose(tid("px-set-ticket-pick-tc_1"), "CARD");
    await click(tid("px-set-ticket-save-tc_1"));
    const patch = posts(calls)[0];
    expect(patch).toMatchObject({ method: "PATCH", path: "/v1/ticket-systems/tc_1" });
    expect(patch.body).toEqual({ mappings: [
      { external_project: "OPS", boss_project_id: "p2", default: true },
      { external_project: "LEND", boss_project_id: P, default: false },
      { external_project: "CARD", boss_project_id: P, default: true },
    ] });
    expect(tid("px-set-ticket-said-tc_1")!.textContent).toContain("CARD is this project's default");
  });

  it("a mirror is read-only; none says how to connect; a refusal is said", async () => {
    stubFetch(() => ({ body: { connections: [{ ...CONNECTION, managed_from: { connection_id: "tc_0", project_id: "home" } }] } }));
    await mountAs(<TicketSystemCard projectId={P} />, "owner");
    await settle();
    expect(tid("px-set-ticket-managed-tc_1")!.textContent).toContain("Managed from home");
    expect(tid("px-set-ticket-save-tc_1")).toBeNull();
    unmount();
    stubFetch(() => ({ body: { connections: [] } }));
    await mountAs(<TicketSystemCard projectId={P} />, "owner");
    await settle();
    expect(tid("px-set-tickets-empty")).not.toBeNull();
    expect(tid("px-set-tickets-connect")!.getAttribute("href")).toBe("?area=setup&focus=ticket_systems");
    unmount();
    stubFetch(() => ({ status: 403, body: { detail: "Needs “View integrations”, ask an Admin." } }));
    await mountAs(<TicketSystemCard projectId={P} />, "owner");
    await settle();
    expect(tid("px-set-tickets-denied")!.textContent).toContain("View integrations");
  });

  it("withDefault keeps other projects' mappings and adds a new key", () => {
    expect(withDefault([{ external_project: "A", boss_project_id: "x", default: true }], "p", "B"))
      .toEqual([{ external_project: "A", boss_project_id: "x", default: true }, { external_project: "B", boss_project_id: "p", default: true }]);
  });
});

describe("Account › Ticket accounts", () => {
  const MINE = { credential_id: "cc_1", project_id: P, provider: "jira", connection_id: "tc_1", display_name: "Jira", account: "param@gmail.com",
                 email: "param@gmail.com", email_matches: false, needs_confirmation: true, status: "active", linked_at: "2026-11-10T00:00:00Z" };
  function engine(identities: unknown[] = [MINE], connections: unknown[] = [CONNECTION, { ...CONNECTION, id: "tc_2", label: "Jira (Cards)" }]) {
    let confirmed = false;
    return stubFetch((c) => {
      // "It's mine" is the API's record: after the confirm, the read says so.
      if (c.path === "/v1/me/ticket-identities/tc_1/confirm" && c.method === "POST") {
        confirmed = true;
        return { body: { identity: { ...MINE, confirmed_by_person: true, needs_confirmation: false } } };
      }
      if (c.path === "/v1/me/ticket-identities") {
        return { body: { identities: confirmed ? identities.map((i) => ({ ...(i as object), confirmed_by_person: true, needs_confirmation: false })) : identities } };
      }
      if (c.path === "/v1/ticket-systems") return { body: { connections } };
      if (c.path === "/v1/ticket-systems/catalog") return { body: { items: [{ provider: "jira", label: "Jira", auth_kinds: [{ kind: "api_token" }, { kind: "oauth2" }] }] } };
      if (c.path === "/v1/ticket-systems/tc_2/oauth/start/load") return { body: { authorize_url: "https://auth.atlassian.com/authorize?x=1" } };
      if (c.path === "/v1/ticket-systems/tc_1/oauth/me") return { body: { disconnected: true } };
      return undefined;
    });
  }

  it("Connect with Atlassian opens the provider; Disconnect, It's mine and Not mine each do their one thing", async () => {
    const calls = engine();
    const opened = vi.spyOn(window, "open").mockImplementation(() => null);
    await mountAs(<TicketAccounts projectId={P} />, "owner");
    await settle();
    expect(tid("ticket-account-cc_1")!.textContent).toContain("param@gmail.com");
    // tc_1 is linked already: only tc_2 offers Connect.
    expect(tid("ticket-account-connect-tc_1")).toBeNull();
    expect(tid("ticket-account-connect-btn-tc_2")!.textContent).toContain("Connect with Atlassian");
    await click(tid("ticket-account-connect-btn-tc_2"));
    expect(posts(calls).map((c) => c.path)).toEqual(["/v1/ticket-systems/tc_2/oauth/start/load"]);
    expect(opened).toHaveBeenCalledWith("https://auth.atlassian.com/authorize?x=1", "_blank", "noopener");

    expect(tid("ticket-account-mismatch-cc_1")!.textContent).toContain("not your BOSS email");
    await click(tid("ticket-account-mine-cc_1"));
    await settle();
    expect(posts(calls)[1]).toMatchObject({ method: "POST", path: "/v1/me/ticket-identities/tc_1/confirm" });
    expect(tid("ticket-account-mismatch-cc_1")).toBeNull();
    // Never browser storage: the confirmation is the API's.
    expect(Object.keys(window.localStorage)).toEqual([]);

    await click(tid("ticket-account-off-cc_1"));
    expect(posts(calls)[2]).toMatchObject({ method: "DELETE", path: "/v1/ticket-systems/tc_1/oauth/me" });
    expect(tid("ticket-accounts-said")!.textContent).toContain("Disconnected");
  });

  it("Not mine disconnects; empty says what linking does; an API without it shows no card", async () => {
    const calls = engine();
    await mountAs(<TicketAccounts projectId={P} />, "owner");
    await settle();
    await click(tid("ticket-account-notmine-cc_1"));
    expect(posts(calls).map((c) => [c.method, c.path])).toEqual([["DELETE", "/v1/ticket-systems/tc_1/oauth/me"]]);
    unmount();
    engine([], []);
    await mountAs(<TicketAccounts projectId={P} />, "owner");
    await settle();
    expect(tid("ticket-accounts-empty")).not.toBeNull();
    unmount();
    stubFetch(() => undefined);
    const host = await mountAs(<TicketAccounts projectId={P} />, "owner");
    await settle();
    expect(host.textContent).toBe("");
  });
});
