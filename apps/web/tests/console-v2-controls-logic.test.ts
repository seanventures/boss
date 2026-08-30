import { describe, expect, it } from "vitest";

import {
  controlsState,
  deployRequest,
  outcomeMessage,
  type Binding,
} from "../lib/v2/deploy-controls";

/**
 * The decisions behind Set target / Unbind / Deploy / Retry.
 *
 * Every case below is a bug this console has actually shipped. They are all the same shape:
 * the UI said something it did not know.
 */

const ready = { deployable: true, deployableReason: "" };

describe("what we know vs what we say", () => {
  it("a lookup that has not returned is 'checking', not 'no destination'", () => {
    // "No verified host yet" was stated before the effect resolved, and there WAS a verified
    // host the whole time. On a page this slow, the false claim is what the operator reads.
    expect(controlsState(null, ready).kind).toBe("checking");
  });

  it("a failed lookup is unreadable, not empty", () => {
    // "We could not check" and "there is nothing" render identically as an empty state and
    // mean opposite things. This is how a deployed app displayed as never deployed.
    const s = controlsState({ error: "HTTP 502" }, ready);
    expect(s.kind).toBe("unreadable");
    expect(s.kind === "unreadable" && s.reason).toBe("HTTP 502");
  });

  it("nothing is offered on a state we could not read", () => {
    // Acting on an unknown state is how the wrong app gets deployed — which happened.
    const s = controlsState({ error: "timeout" }, ready);
    expect(s).not.toHaveProperty("canDeploy");
  });

  it("a genuinely unbound app is reported as unbound, once we have looked", () => {
    const s = controlsState({ linked_target_id: null, deployments: [] }, ready);
    expect(s.kind).toBe("ready");
    expect(s.kind === "ready" && s.bound).toBe(false);
  });
});

describe("Deploy only promises what the pipeline can honour", () => {
  const bound: Binding = { linked_target_id: "tgt_1", linked_host: "host-a", deployments: [] };

  it("bound and reviewed can deploy", () => {
    const s = controlsState(bound, ready);
    expect(s.kind === "ready" && s.canDeploy).toBe(true);
    expect(s.kind === "ready" && s.blockedReason).toBe("");
  });

  it("no destination blocks it, and says so", () => {
    const s = controlsState({ linked_target_id: null }, ready);
    expect(s.kind === "ready" && s.canDeploy).toBe(false);
    expect(s.kind === "ready" && s.blockedReason).toMatch(/no destination/i);
  });

  it("nothing reviewed blocks it, with the reason from the server", () => {
    // A Deploy offered here would clone, build, transfer, and only then fail at the health
    // gate — promising something the pipeline cannot honour.
    const s = controlsState(bound, {
      deployable: false,
      deployableReason: "nothing reviewed yet — a run must complete and open a PR",
    });
    expect(s.kind === "ready" && s.canDeploy).toBe(false);
    expect(s.kind === "ready" && s.blockedReason).toMatch(/nothing reviewed yet/);
  });

  it("a deployment already running blocks a second one", () => {
    // The v1 button re-enabled as soon as the POST returned, before any deployment_run
    // existed, so a second click started a second deployment of the same commit.
    const s = controlsState(
      { ...bound, deployments: [{ id: "dep_1", status: "running" }] },
      ready,
    );
    expect(s.kind === "ready" && s.canDeploy).toBe(false);
    expect(s.kind === "ready" && s.live).toBe(true);
    expect(s.kind === "ready" && s.blockedReason).toMatch(/already running/);
  });

  it("a blocked Deploy always carries a reason", () => {
    // A disabled control with no reason is what sends people to the API to find out.
    const cases: Array<[Binding, typeof ready]> = [
      [{ linked_target_id: null }, ready],
      [bound, { deployable: false, deployableReason: "no reviewed build" }],
      [{ ...bound, deployments: [{ id: "d", status: "deploying" }] }, ready],
    ];
    for (const [b, o] of cases) {
      const s = controlsState(b, o);
      expect(s.kind === "ready" && s.canDeploy).toBe(false);
      expect(s.kind === "ready" && s.blockedReason.length, JSON.stringify(b)).toBeGreaterThan(0);
    }
  });
});

describe("Retry is for a failure, not for a success", () => {
  it("a failed deployment can be retried", () => {
    const s = controlsState(
      { linked_target_id: "t", deployments: [{ id: "dep_9", status: "failed" }] },
      ready,
    );
    expect(s.kind === "ready" && s.canRetry).toBe(true);
    expect(s.kind === "ready" && s.retryDeploymentId).toBe("dep_9");
  });

  it("a succeeded deployment offers no retry", () => {
    // Retrying a success starts a second deployment of the same commit for no reason.
    const s = controlsState(
      { linked_target_id: "t", deployments: [{ id: "dep_9", status: "succeeded" }] },
      ready,
    );
    expect(s.kind === "ready" && s.canRetry).toBe(false);
    expect(s.kind === "ready" && s.retryDeploymentId).toBe(null);
  });

  it("a running deployment offers no retry", () => {
    const s = controlsState(
      { linked_target_id: "t", deployments: [{ id: "dep_9", status: "running" }] },
      ready,
    );
    expect(s.kind === "ready" && s.canRetry).toBe(false);
  });

  it("a failure with no id cannot be retried", () => {
    // Without an id the request has nothing to address, and a Retry that posts undefined is
    // worse than no Retry.
    const s = controlsState({ linked_target_id: "t", deployments: [{ status: "failed" }] }, ready);
    expect(s.kind === "ready" && s.canRetry).toBe(false);
  });
});

describe("Unbind", () => {
  it("is only possible when there is a target to unbind", () => {
    const bound = controlsState({ linked_target_id: "tgt_1", linked_host: "h" }, ready);
    const unbound = controlsState({ linked_target_id: null }, ready);
    expect(bound.kind === "ready" && bound.targetId).toBe("tgt_1");
    expect(unbound.kind === "ready" && unbound.targetId).toBe(null);
  });

  it("boundness comes from the binding, not from deployment history", () => {
    // The v1 card decided boundness from whether deployments existed, so an Unbind that HAD
    // worked still showed the app as bound. History does not answer this question.
    const s = controlsState(
      { linked_target_id: null, deployments: [{ id: "d", status: "succeeded" }] },
      ready,
    );
    expect(s.kind === "ready" && s.bound).toBe(false);
  });
});

describe("what gets reported afterwards", () => {
  it("success reports the thing that was actually promised", () => {
    expect(outcomeMessage({ ok: true, status: 200 }, {}, "deployment started"))
      .toBe("deployment started");
  });

  it("a failure reports the API's reason, not the hoped-for message", () => {
    // "retry started" printed regardless of the response is how a control lies about work
    // that never began.
    expect(outcomeMessage({ ok: false, status: 400 }, { error: "actor is required" }, "retry started"))
      .toBe("actor is required");
  });

  it("a failure with no reason still gives something actionable", () => {
    // An operator can act on a 409. Nobody can act on "something went wrong".
    expect(outcomeMessage({ ok: false, status: 409 }, {}, "started")).toBe("HTTP 409");
  });

  it("an empty error string does not become the whole message", () => {
    expect(outcomeMessage({ ok: false, status: 500 }, { error: "   " }, "started")).toBe("HTTP 500");
  });
});

describe("the requests these controls issue", () => {
  it("deploy addresses the run and its target", () => {
    expect(deployRequest("deploy", {
      projectId: "boss-dogfood", actor: "executive_console", runId: "crun_1", targetId: "tgt_1",
    })).toEqual({
      op: "deploy",
      project_id: "boss-dogfood",
      actor: "executive_console",
      run_id: "crun_1",
      target_id: "tgt_1",
      environment: "staging",
    });
  });

  it("retry addresses the deployment, not the run", () => {
    // Redeploy is idempotent by receipt, addressed by deployment_run_id. Sending run_id would
    // start a new deployment rather than retrying the failed one.
    expect(deployRequest("retry", {
      projectId: "p", actor: "a", deploymentRunId: "dep_9",
    })).toEqual({ op: "retry", project_id: "p", actor: "a", deployment_run_id: "dep_9" });
  });

  it("the actor is always sent and never invented", () => {
    // Defaulting to "operator" got "Authenticated principal 'ceo' is not allowed to act as
    // 'operator'" — right, because claiming a principal you are not is impersonation.
    const r = deployRequest("deploy", { projectId: "p", actor: "executive_console", runId: "r" });
    expect(r.actor).toBe("executive_console");
    expect(r.actor).not.toBe("operator");
  });

  it("an absent target is omitted rather than sent empty", () => {
    // An empty target_id would be a claim about a destination that does not exist.
    const r = deployRequest("deploy", { projectId: "p", actor: "a", runId: "r", targetId: null });
    expect(r.target_id).toBeUndefined();
  });
});
