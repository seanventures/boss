import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

/**
 * A subscription is not a key.
 *
 * The wizard used to tell an operator to go and mint a headless token themselves and paste it
 * back — and for anything that was not Claude Code it said, honestly, that BOSS had no step
 * for it at all. That was true when the only way in was a credential.
 *
 * It is not true any more. A Codex or Cursor plan is an OAuth session the CLI already holds,
 * so BOSS can look for the CLI on the machine and read whether it is signed in. Community
 * records eligible subscriptions without a live model turn until proof is budget governed.
 *
 * The earlier comment in this file is worth keeping in mind: a previous version promised a
 * "connection session" that did nothing, producing a green tick over nothing. Hence the
 * wizard distinguishes a proved connection from one recorded without live proof.
 */

const UI = readFileSync(new URL("../app/setup-ui.tsx", import.meta.url), "utf8");
const ROUTE = readFileSync(new URL("../app/api/setup/route.ts", import.meta.url), "utf8");

describe("the wizard asks the machine", () => {
  it("looks for the CLI instead of asking for a credential", () => {
    // The project id joined it later, so the recorded connection could travel back with the
    // detection: a saved connection had been showing as an untouched Connect button.
    expect(UI).toContain("/api/setup?view=coding-agents&project_id=");
    expect(UI).toContain("ASK THE MACHINE, DO NOT ASK THE OPERATOR");
  });

  it("only does that for a subscription provider", () => {
    // An API-key provider has a key to type; detection would be noise.
    expect(UI).toMatch(/if \(!subscription\) \{ setAgents\(null\); return; \}/);
  });

  it("offers a Connect button once the CLI is signed in", () => {
    expect(UI).toContain('data-testid="setup-agent-connect"');
    expect(UI).toContain('step: "coding-agent"');
  });

  it("shows the next COMMAND when it is not ready", () => {
    // "Not configured" sends somebody to search. A command does not.
    expect(UI).toContain('data-testid="setup-agent-next-step"');
    expect(UI).toContain("detected.next_step");
  });

  it("reports what it found, so the operator can see why", () => {
    expect(UI).toContain('data-testid="setup-agent-state"');
    expect(UI).toContain("detected.version");
    expect(UI).toContain("detected.auth_detail");
  });
});

describe("it does not repeat the green-tick-over-nothing mistake", () => {
  it("shows whether live proof is available before claiming it ran", () => {
    expect(UI).toContain("liveProofAvailable");
    expect(UI).toContain("Connected without a live model check");
    expect(UI).toContain("Community pauses live proof");
  });

  it("uses the server's proof capability in the connection request", () => {
    expect(UI).toContain("status.live_proof_available === true");
    expect(UI).toContain("prove: liveProofAvailable");
  });

  it("keeps the manual instructions for anything undetected", () => {
    // Detection can fail (no CLI, an unreachable API). Falling back to the old text beats
    // showing nothing, and beats implying a connection that does not exist.
    expect(UI).toContain('data-testid="setup-model-subscription"');
    expect(UI).toContain("subscription && detected");
  });
});

describe("the route", () => {
  it("reaches both coding-agent endpoints", () => {
    expect(ROUTE).toContain("/v1/setup/coding-agents?project_id=");
    expect(ROUTE).toContain('"/v1/setup/coding-agents/connect"');
  });

  it("still names its upstreams rather than taking a path from the client", () => {
    // Otherwise this route is a way to reach any API endpoint with the console's credentials.
    expect(ROUTE).toContain('step === "coding-agent" ? "/v1/setup/coding-agents/connect"');
    expect(ROUTE).not.toMatch(/\$\{API_URL\}\$\{body\./);
  });
});

describe("the steps survive success", () => {
  it("shows install and sign-in commands even when it is already working", () => {
    // A wizard that hides the instructions the moment it is happy makes the SECOND visit
    // worse than the first — and the second visit is the common one: a new machine, an
    // expired login, a colleague being shown how.
    expect(UI).toContain('data-testid="setup-agent-steps"');
    expect(UI).toContain("detected.install_hint");
    expect(UI).toContain("detected.signin_hint");
    expect(UI).toContain("THE STEPS STAY ON SCREEN EVEN WHEN IT IS ALREADY WORKING");
  });

  it("marks what is already done rather than hiding it", () => {
    expect(UI).toContain("already installed");
    expect(UI).toContain("already signed in");
  });

  it("lets you press it again after re-authorising", () => {
    // Re-connecting is how you confirm a new login, so it must not be a no-op or disabled.
    expect(UI).toContain("Re-check");
    expect(UI).toContain("pressing this again after re-authorising");
  });

  it("only disables the button when there is no binary to run", () => {
    expect(UI).toContain("disabled={connecting || !detected.installed}");
  });
});
