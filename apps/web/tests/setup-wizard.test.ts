import { describe, expect, it } from "vitest";

import { lastSavedProvider } from "../app/setup-ui";
import { codeOnly } from "./_helpers/source";

/**
 * The one screen a first-time self-hosted operator sees.
 *
 * Setup was documentation: set two environment variables on the API container, restart it, then
 * call four endpoints in the right order. Every endpoint existed; the order did not.
 */
const UI = codeOnly("app/setup-ui.tsx");
const CSS = codeOnly("app/globals.css");
const ROUTE = codeOnly("app/api/setup/route.ts");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const EDITION = codeOnly("lib/edition.ts");

describe("setup is reachable before anything else makes sense", () => {
  it("it is an area, in Settings", () => {
    expect(SHELL).toContain('id: "setup"');
    expect(SHELL).toContain('| "setup"');
  });

  it("and the community edition shows it — that is who it is for", () => {
    expect(EDITION).toContain('"setup"');
  });
});

describe("connecting a repository", () => {
  it("takes the token itself, not the name of an env var", () => {
    expect(UI).toContain('data-testid="setup-github-token"');
    expect(UI).toContain("token: token.trim()");
  });

  it("the field is a password field and is wiped after the attempt", () => {
    // A token left in a form survives every screenshot and session restore after it.
    const at = UI.indexOf('data-testid="setup-github-token"');
    expect(UI.slice(at - 200, at)).toContain('type="password"');
    expect(UI).toContain("setToken(\"\")");
  });

  it("and the connection is tested rather than assumed", () => {
    expect(UI).toContain("test_after_link: true");
    expect(UI).toContain("Connect and test");
  });

  it("it says which permissions the token needs", () => {
    // "It didn't work" over a token missing one scope is a support ticket.
    expect(UI).toContain("Contents: read and write");
    expect(UI).toContain("Pull requests: read and write");
  });
});

describe("connecting a model", () => {
  it("offers keys, anything OpenAI-compatible, and subscriptions", () => {
    expect(UI).toContain('data-testid="setup-model-provider"');
    expect(UI).toContain('data-testid="setup-model-key"');
    expect(UI).toContain('data-testid="setup-model-base"');
  });

  it("a subscription gets an explanation, not a key box that would be rejected", () => {
    expect(UI).toContain('data-testid="setup-model-subscription"');
    expect(UI).toContain(") : subscription ? null : (");
  });

  it("and it describes what actually happens, not a flow that does not exist", () => {
    // The first version promised a connection session "once the provider's login flow is wired
    // up", which reads as though BOSS shares a live session with your CLI. It does not, and
    // /connect would not give it one — that endpoint RECORDS a connection from a reference the
    // caller already holds. Pointing the wizard at it would be a green tick over nothing.
    expect(UI).toContain("claude setup-token");
    expect(UI).toContain("CLAUDE_CODE_OAUTH_TOKEN");
    expect(UI).not.toContain("once the provider");
  });

  it("and says which half of BOSS the subscription actually powers", () => {
    // It reaches the builder agents in the sandbox. The chat uses whatever model is configured
    // above — somebody who connects a subscription and sees no change in the chat is not
    // looking at a bug.
    expect(UI).toContain("It powers builds, not this chat");
  });

  it("and the key is wiped after the attempt too", () => {
    expect(UI).toContain("setApiKey(\"\")");
  });
});

describe("the route does not become a way to reach any endpoint", () => {
  it("every step is named server-side", () => {
    // Taking a path from the client would let this reach any API endpoint with the console's
    // credentials. The INVARIANT is that each step maps to a literal path here — not the
    // wording of the rejection, which this used to pin and which broke the moment a third
    // step was added (connecting a coding-agent subscription) without weakening anything.
    expect(ROUTE).toContain('step === "github" ? "/v1/github/easy-connect"');
    expect(ROUTE).toContain('step === "model" ? "/v1/setup/model"');
    expect(ROUTE).toContain('step === "coding-agent" ? "/v1/setup/coding-agents/connect"');
    // An unknown step is refused rather than forwarded.
    expect(ROUTE).toMatch(/if \(!path\) \{/);
    expect(ROUTE).toContain("step must be");
    // And no path is ever built from the request body.
    expect(ROUTE).not.toMatch(/\$\{API_URL\}\$\{body\./);
  });

  it("and the token is posted, never put in a query string", () => {
    expect(ROUTE).toContain("method: \"POST\"");
    expect(ROUTE).not.toContain("token=");
  });
});

describe("what the install is already running on", () => {
  it("is shown, with a live verdict rather than a variable being non-empty", () => {
    expect(UI).toContain('data-testid="setup-configured"');
    expect(UI).toContain('data-testid="setup-env-github-fact"');
    expect(UI).toContain('data-testid="setup-env-model-fact"');
    expect(UI).toContain("Can push");
    expect(UI).toContain("Answering");
  });

  it("and says it was checked just now, not read from a variable", () => {
    expect(UI).toContain("Checked just now");
  });

  it("the card stays out of the way when nothing is configured", () => {
    // On a genuinely fresh install this is noise above the two forms that matter.
    expect(UI).toContain("if (!github?.configured && !model?.configured) return null;");
  });

  it("it shows the last four of a credential, never the credential", () => {
    expect(UI).toContain("token_last4");
    expect(UI).toContain("key_last4");
    expect(UI).not.toContain("github.token}");
    expect(UI).not.toContain("model.api_key");
  });

  it("and says what connecting something new would do to it", () => {
    expect(UI).toContain("replaces what is here");
  });
});

/**
 * "Even when it is connected, after refreshing or deployment it is not getting saved.. can you
 * pick up the one that's last saved?"
 *
 * Nothing was being lost. The step rendered live detection and a Connect button, so a finished
 * connection was indistinguishable from a fresh install — and the picker always opened on its
 * first entry, which read as the connection having been forgotten.
 */
const SAVED = (wizard_provider: string, saved_at: string, kind = "subscription") => ({
  source_id: `src-${wizard_provider}`, provider: "x", wizard_provider,
  auth_method: "official_cli_oauth", kind, connected: true, verified: false, saved_at,
});
const OFFERED = [
  { provider: "openrouter", label: "OpenRouter", kind: "api_key", api: "openai" },
  { provider: "codex", label: "Codex", kind: "subscription", api: "cli" },
];

describe("the picker opens on what was last saved", () => {
  it("picks the newest saved source — the API returns them newest-first", () => {
    expect(lastSavedProvider(
      [SAVED("codex", "2026-09-18T02:15:00Z"), SAVED("openrouter", "2026-09-01T00:00:00Z")],
      OFFERED)).toBe("codex");
  });

  it("nothing saved means nothing to restore, and the caller keeps its own default", () => {
    expect(lastSavedProvider([], OFFERED)).toBe("");
  });

  it("and it never selects an option that is not in the list", () => {
    // A source recorded by an older build, or one behind an edition flag. Selecting it would
    // leave a <select> showing a value it has no <option> for.
    expect(lastSavedProvider([SAVED("gemini", "2026-09-18T02:15:00Z")], OFFERED)).toBe("");
    // Falls through to the next saved one it CAN offer rather than giving up.
    expect(lastSavedProvider(
      [SAVED("gemini", "2026-09-18T02:15:00Z"), SAVED("codex", "2026-09-01T00:00:00Z")],
      OFFERED)).toBe("codex");
  });

  it("a source with no resolved dropdown key is skipped, not selected as blank", () => {
    expect(lastSavedProvider([SAVED("", "2026-09-18T02:15:00Z")], OFFERED)).toBe("");
  });

  it("but the operator's own choice wins over it", () => {
    // Otherwise every status refresh would drag the dropdown back while they were reading it.
    expect(UI).toContain("if (touched || !startOn) return;");
    expect(UI).toContain("setTouched(true);");
  });
});

describe("a connection that was made says so", () => {
  it("the step shows what is connected before it shows the form", () => {
    // It used to be one run-on sentence — "Saved here: OpenRouter — date, Claude Code
    // subscription (subscription) — date, …" — a list pretending to be prose, with no state
    // and no way to see which model does which job. Reported as: three models connected and
    // no way to tell they were connected.
    expect(UI).toContain('data-testid="setup-connected-models"');
    expect(UI).toContain("Connected models");
  });

  it("each one says what it is and which jobs it holds", () => {
    expect(UI).toContain("setup-models-kind");
    expect(UI).toContain("setup-models-jobs");
    expect(UI).toContain('"no role yet"');
  });

  it("and a green dot means BOSS can use it now", () => {
    expect(UI).toContain('className="setup-dot"');
    expect(CSS).toContain("--green");
  });

  it("the role picker is visible, not hidden behind a disclosure", () => {
    // Twice reported: "I cant figure out where that button", then "this is hiding somewhere
    // which I cannot find it". A <details> that defaults shut answers the question only for
    // people who already suspect it exists — and which model does the implementing is the
    // second thing anybody asks after connecting one, not an advanced setting.
    const picker = UI.slice(UI.indexOf("function RoleModels"));
    const body = picker.slice(0, picker.indexOf("\n}"));
    expect(body).not.toContain("<details");
    expect(body).toContain('data-testid="setup-role-models"');
    expect(body).toContain('className="stack-card"');   // the same card as every other section
    // The heading element is the contract; its type treatment is a design choice.
    expect(body).toMatch(/<h2[^>]*>Who does what<\/h2>/);
  });

  it("a connected CLI is reported from the record, not only from a probe", () => {
    expect(UI).toContain('data-testid="setup-agent-connected"');
    expect(UI).toContain("connected_at");
    // Detection and the record answer different questions; both are kept.
    expect(UI).toContain("detected.connected");
  });

  it("labels a connected subscription without promising a paused live check", () => {
    expect(UI).toContain('detected.connected ? `${liveProofAvailable ? "Re-check" : "Reconnect"}');
  });

  it("the recorded state is re-read after connecting, not assumed from the reply", () => {
    expect(UI).toContain("await loadAgents(); onDone();");
  });

  it("the record is per project, so the project id travels with the request", () => {
    expect(ROUTE).toContain("/v1/setup/coding-agents?project_id=");
    expect(UI).toContain("view=coding-agents&project_id=");
  });
});

/**
 * "I choose claude code for few roles.. But looks like I could connect only one model in the
 * UI.. I want multiple models and choose only those active models to be assigned in the role."
 */
describe("a subscription whose credential travels gets a box to travel in", () => {
  it("takes the token, rather than naming an environment variable to go and set", () => {
    // The old answer was: set CLAUDE_CODE_OAUTH_TOKEN on the API container and restart it —
    // which is the step the wizard exists to remove.
    expect(UI).toContain('data-testid="setup-agent-token"');
    expect(UI).toContain('data-testid="setup-agent-token-input"');
    expect(UI).toContain('detected.credential_kind === "token"');
  });

  it("the field is a password field and is wiped as soon as BOSS has the value", () => {
    const at = UI.indexOf('data-testid="setup-agent-token-input"');
    expect(UI.slice(at - 200, at)).toContain('type="password"');
    expect(UI).toContain("setToken(\"\")");
  });

  it("it is shown even when a token is already stored, so an expired one can be replaced", () => {
    expect(UI).toContain("pasting replaces it");
  });

  it("storing goes through the credential route, then proves the connection", () => {
    expect(UI).toContain('step: "coding-agent-credential"');
    expect(ROUTE).toContain('"/v1/setup/coding-agents/credential"');
  });

  it("and a store with nowhere good to put it is a question, not a failure", () => {
    // 409: nothing is wrong with the request, so resending it unchanged fails identically.
    expect(UI).toContain("secret_store_fallback_not_accepted");
    expect(UI).toContain("Store in the database and connect");
    // The decision object survives the proxy rather than being flattened to a sentence.
    expect(ROUTE).toContain("decision: detail");
  });

  it("a file-route CLI gets no paste box — there is nothing an operator can type", () => {
    // codex: the whole auth.json travels, and asking somebody to paste one by hand is how a
    // live credential ends up in a screenshot.
    expect(UI).not.toContain('data-testid="setup-agent-file-input"');
  });
});

describe("a role picker offers what is connected", () => {
  const KNOBS = codeOnly("app/settings-knobs-ui.tsx");

  it("the connected list comes from the API, with the knobs", () => {
    expect(KNOBS).toContain("connected_engines");
    expect(KNOBS).toContain('data-testid="settings-connected-engines"');
  });

  it("and says what to do when there is only one", () => {
    // A one-option picker with no explanation reads as a limit of the product.
    expect(KNOBS).toContain("Connect another above and it appears here");
    expect(KNOBS).toContain("Nothing is connected yet");
  });

  it("only on the model panel — a number knob has nothing to connect", () => {
    expect(KNOBS).toContain('only === "choice" ? (');
  });
});

describe("the page does not contradict itself", () => {
  it("does not say there is nothing to paste above a paste box", () => {
    // It said exactly that, in the same card, two lines apart — and the operator believes the
    // sentence rather than the form.
    const at = UI.indexOf("There is nothing to paste");
    expect(at).toBeGreaterThan(-1);
    expect(UI.slice(at - 400, at)).toContain('detected.credential_kind === "token"');
  });

  it("and the next step is the button on this page, not a shell command", () => {
    expect(UI).toContain("Paste the token above and press");
  });
});

/**
 * "If you can provide me simple steps on how to get the token for git hub… or if it may change
 * then we should probably use an Agent in the setup."
 *
 * Both, in that order of reliance. The steps are exact, free and offline; the agent has no
 * browser, so for "where do I click" it is the less reliable source — it earns its place on
 * the things a list cannot anticipate.
 */
describe("getting a GitHub token is written out, not searched for", () => {
  it("the steps are on the page, behind an expander", () => {
    expect(UI).toContain('data-testid="setup-github-help"');
    expect(UI).toContain('data-testid="setup-github-help-steps"');
    expect(UI).toContain("How do I get a token?");
  });

  it("collapsed by default — the second visit does not need them", () => {
    const at = UI.indexOf('data-testid="setup-github-help-toggle"');
    expect(UI.slice(at - 300, at)).toContain("useState(false)");
  });

  it("the link goes to the create form, not to a settings index", () => {
    // The address that has outlived several reshuffles of the surrounding UI, which is why it
    // is here instead of a screenshot.
    expect(UI).toContain("https://github.com/settings/personal-access-tokens/new");
  });

  it("it names the two permissions exactly as GitHub labels them", () => {
    expect(UI).toContain("Contents");
    expect(UI).toContain("Pull requests");
    expect(UI).toContain("Read and write");
  });

  it("and the three things people actually get wrong", () => {
    // Measured against the failures this step produces: an org-owned repo with a
    // personally-owned token, a repo that does not exist yet, and an expiry nobody noted.
    expect(UI).toContain("Resource owner");
    expect(UI).toContain("It must already exist");
    expect(UI).toContain("pick a date you will not be surprised by");
  });

  it("including the organisation that blocks fine-grained tokens", () => {
    expect(UI).toContain("blocks fine-grained tokens");
    expect(UI).toContain("<code>repo</code>");
  });
});

describe("and an agent is one click away, carrying the error", () => {
  it("both steps offer it", () => {
    expect(UI).toContain('data-testid="setup-ask-boss"');
    expect((UI.match(/<AskBoss/g) ?? []).length).toBe(2);
  });

  it("it opens the console's own dock rather than a second chat surface", () => {
    expect(UI).toContain('url.searchParams.set("agent", "general")');
    expect(UI).toContain('url.searchParams.set("dock_ask", question)');
  });

  it("and clears the role, so it cannot inherit whoever was addressed last", () => {
    expect(UI).toContain('url.searchParams.delete("dock_role")');
  });

  it("and starts a NEW conversation rather than reopening the last one", () => {
    // Measured: a question about connecting a repository opened under a week-old Deployment
    // Manager thread, banner and all — "Continuing with the Deployment Manager". Clearing the
    // role is not enough, because the resumed thread carries the role it was had with.
    expect(UI).toContain('url.searchParams.set("dock_fresh", "1")');
  });

  it("the question carries what failed, because that is the whole value of asking", () => {
    expect(UI).toContain("It failed with: ${problem}");
    expect(UI).toContain("Ask BOSS about this error");
  });
});
