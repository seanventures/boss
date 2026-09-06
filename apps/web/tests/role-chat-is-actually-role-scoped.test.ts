import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * The dock said "ENGINEERING CAPTAIN — answering from this role's own work here", and the
 * request that went to the server had no role in it.
 *
 * `agent-ui` set it on every turn (`fd.set("role", roleName)`), deliberately, with a comment
 * explaining why it must be every turn and not just the first. `askBossAction` then built the
 * JSON body field by field and simply never read it. So the entire role-addressed path — read
 * this role's record, answer from its lens, the grounding guard that re-prompts a role which
 * skipped its own history — was unreachable from the console. It worked when called directly
 * with curl, which is exactly why it survived: the backend was right the whole time.
 *
 * The specific test is below; the general one — every field the composer sets is forwarded —
 * is what stops the next field from being dropped the same way.
 */
const UI = codeOnly("app/agent-ui.tsx");
const ACTIONS = codeOnly("app/actions.ts");

/** The body literal of one exported action, from its `postJsonReturn(` to the closing `})`. */
function bodyOf(name: string): string {
  const start = ACTIONS.indexOf(`export async function ${name}(`);
  expect(start, `${name} not found`).toBeGreaterThan(-1);
  const post = ACTIONS.indexOf("/v1/engine/agent/chat", start);
  expect(post, `${name} does not post to the chat endpoint`).toBeGreaterThan(-1);
  return ACTIONS.slice(post, ACTIONS.indexOf("});", post));
}

/** Every `fd.set("x", …)` the chat component performs, excluding the ones it sets and clears. */
function fieldsTheComposerSets(): string[] {
  const found = new Set<string>();
  for (const m of UI.matchAll(/fd\.set\("([a-z_]+)"/g)) found.add(m[1]);
  return [...found];
}

describe("the role reaches the server", () => {
  it("askBossAction forwards it — this is the bug", () => {
    expect(bodyOf("askBossAction")).toContain("role:");
  });

  it("so does the other chat action, which posts to the same endpoint", () => {
    expect(bodyOf("agentChatAction")).toContain("role:");
  });

  it("every field the composer sets is forwarded by the action that sends it", () => {
    // The general form. A field set on the FormData and not read by the action is silently
    // dropped: no type error, no runtime error, and a feature that looks implemented from
    // both ends. `message` and `confirm_plan_id` are read into locals first, so they count
    // as forwarded by name appearing anywhere in the body.
    const body = bodyOf("askBossAction");
    const missing = fieldsTheComposerSets().filter((f) => !body.includes(f));
    expect(missing, `set by the composer, never sent: ${missing.join(", ")}`).toEqual([]);
  });
});

describe("a role's conversation survives closing the panel", () => {
  it("a role turn is scoped to a thread of its own", () => {
    // Without a scope key a role chat falls into the general bucket with a null thread id, so
    // every open starts a stranger who cannot refer to what it just told you.
    expect(UI).toContain("const roleRef = roleName ? `role:${roleName}` : \"\"");
    expect(UI).toContain('else if (roleRef) fd.set("external_ref", roleRef)');
  });

  it("the history is loaded when the panel opens, not after the first message", () => {
    // `codeOnly` strips comments, so anchor on code: the mount effect runs from the workspace
    // load to the starters effect.
    const mount = UI.slice(UI.indexOf('view: "workspace", project_id: projectId, actor'),
                           UI.indexOf('view: "starters"'));
    expect(mount).toContain("ths.find((t) => (t.external_ref ?? null) === roleRef)");
    expect(mount).toContain('view: "messages"');
  });

  it("a role with no history opens clean rather than borrowing the general thread", () => {
    const mount = UI.slice(UI.indexOf('view: "workspace", project_id: projectId, actor'),
                           UI.indexOf('view: "starters"'));
    expect(mount).toContain("if (roleRef) { setLoadingThread(false); return; }");
  });

  it("the window is the thread list's own — a week", () => {
    // "Preserved for a week" has to be true of what is LOADED, not just of what is stored:
    // reviving a month-old argument as if it were this morning's is its own kind of wrong.
    expect(ACTIONS).toContain("since_days=10");   // the list the dock searches
    expect(UI).toContain("for a week");
  });
});

describe("the starters are about the role", () => {
  it("they come from the server, which grounds them in that role's record", () => {
    // Read through the route handler, not a Server Action: Next serialises those against
    // router.refresh(), and the dock's reads were queueing behind an 8-20s console re-render —
    // which is what "Loading conversation… is stuck" actually was.
    expect(UI).toContain('view: "starters"');
    const ROUTE = codeOnly("app/api/agent/route.ts");
    expect(ROUTE).toContain("/v1/agent/role-starters");
  });

  it("the fixed status prompts are not what a named role offers", () => {
    const effect = UI.slice(UI.indexOf('view: "starters"'),
                            UI.indexOf("const refreshThreads"));
    expect(effect).toContain("setSuggestions(list as string[])");
    // The generic three remain as the fallback for the GENERAL agent, which is a dashboard-ish
    // surface and where they are honest. They must not be what a role shows.
    expect(effect).not.toContain("What changed today?");
  });

  it("the panel stops calling a named role 'BOSS Agent'", () => {
    // Two identities in one column — the header naming the Engineering Captain and the body
    // introducing the general assistant — reads as a template nobody adapted.
    expect(UI).toContain('<span className="eyebrow">{roleName ?? "BOSS Agent"}</span>');
    expect(UI).toContain("`Ask ${roleName}`");
  });
});
