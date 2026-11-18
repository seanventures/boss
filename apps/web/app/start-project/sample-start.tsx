"use client";

/**
 * TRY A SAMPLE, ONE PATH FOR EVERY SHEET (stranger test, rc10.27). Add a project's chip and the
 * older Start sheet each had their own copy, and both:
 *
 *   · listed the needs one at a time ("a connected model", then after a 409 a repository and a
 *     deployment host): the list is now the start's own check, whole (lib/start-project
 *     `sampleNeeds`), asked before anything is created;
 *   · ignored the tick: Start anyway did nothing until the button was pressed again. The tick is
 *     the answer: ticking it starts the sample with exactly what the list showed;
 *   · demanded a deployment host to build and open a PR. A need the run stops for a person
 *     before (`after_a_person`: the release step) is accepted for the sample and said so, with
 *     where to add this machine as a host when it is time to deploy.
 *
 * The sample keeps its code on this machine: the server gives it a local repository when it is
 * made (POST /v1/projects with the sample key), the way Start a new app › This machine does.
 */
import React, { useState } from "react";

import { consoleHref } from "../console-href";
import { useGate } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError } from "../../lib/identity/api";
import { withConsent } from "../../lib/identity/consent";
import { ensureSample, laterNeeds, projectPageHref, sampleNeeds } from "../../lib/start-project";
import { templatesApi } from "../../lib/templates";
import { ReadinessPanel, asReadiness, unaccepted, type Readiness } from "../workflow-readiness-ui";

/** The sample, as the project page's view names it (services/start_project.py). */
export const SAMPLE = { template: "hello-world", name: "Hello World", stack: "node", workflow_key: "minor_feature" };

/** The readiness a refused start carried, whatever wrapped it (`detail` or the body). */
function readinessOf(e: unknown): Readiness | null {
  const b = e instanceof IdentityError ? e.body : {};
  const detail = (b.detail && typeof b.detail === "object" ? b.detail : {}) as Record<string, unknown>;
  return asReadiness(b.readiness ?? detail.readiness);
}

const union = (a: readonly string[], b: readonly string[]) => [...new Set([...a, ...b])];

export function useSampleStart({ homeId, go, setBusy, setError, words }: {
  homeId: string;
  go: (href: string) => void;
  /** The sheet's own busy and error lines: the sample shows in them like any other press. */
  setBusy: (busy: boolean) => void;
  setError: (error: string | null) => void;
  /** How the sheet says a refusal in words. */
  words: (e: unknown) => string;
}) {
  const guarded = useGuardedCall();
  const canCreate = useGate("project:create");
  const [project, setProject] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [accepted, setAccepted] = useState<string[]>([]);

  const run = async (acceptedNow: string[] = accepted, retried = false): Promise<void> => {
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy(true); setError(null);
    let anyway = acceptedNow;
    try {
      let pid = project;
      if (!pid) {
        // EVERY NEED FIRST, before the project is made: a refusal leaves nothing behind.
        const needs = asReadiness(await sampleNeeds(homeId, SAMPLE.workflow_key));
        if (needs) {
          anyway = union(acceptedNow, laterNeeds(needs));
          if (unaccepted(needs, anyway).length) { setRefusal(needs); setAccepted(anyway); setBusy(false); return; }
        }
        const made = await guarded("Create the Hello World project.", () => ensureSample(SAMPLE.name));
        pid = made.id;
        setProject(pid);
      }
      const body = { project_id: pid, new_app: { name: SAMPLE.name }, stack: SAMPLE.stack, workflow_key: SAMPLE.workflow_key,
                     dedupe_key: `sample:${pid}`, ...(anyway.length ? { start_anyway: anyway } : {}) };
      await guarded("Start the sample.", () => withConsent(() => templatesApi.apply(SAMPLE.template, body)));
      go(projectPageHref(pid, homeId));
    } catch (e) {
      const ready = readinessOf(e);
      if (ready) {
        const now = union(anyway, laterNeeds(ready));
        // Only needs the run stops for a person before: nothing for the person to decide now.
        if (!retried && !unaccepted(ready, now).length) { await run(now, true); return; }
        setRefusal(ready); setAccepted(now); setBusy(false);
        return;
      }
      setError(words(e));
      setBusy(false);
    }
  };

  /** The tick is the answer: accepting every item starts the sample, no second press. */
  const accept = (keys: string[]) => {
    setAccepted(keys);
    if (refusal && keys.length && !unaccepted(refusal, keys).length) void run(keys);
  };

  return { run: () => run(), refusal, accepted, accept, reset: () => { setRefusal(null); setAccepted([]); } };
}

/** The sample's checklist: every need, the later ones said as later, and the one tick. */
export function SampleNeeds({ refusal, accepted, onAccept, testid }: {
  refusal: Readiness | null; accepted: string[]; onAccept: (keys: string[]) => void; testid: string;
}) {
  if (!refusal) return null;
  const later = laterNeeds(refusal);
  // NOT NEEDED IS NOT LISTED AS NEEDED (stranger test r3, 2026-11-18): the deploy host sat in
  // "Setup needed" as "(later, before deploy)" though the sample's PR path never deploys. The
  // checklist holds only what the PR needs; a later need is accepted with the one tick and said
  // once, below, as optional.
  const now = { ...refusal, missing: (refusal.missing ?? []).filter((m) => !later.includes(m.key)) };
  const accept = (keys: string[]) => onAccept(keys.length ? [...new Set([...keys, ...later])] : []);
  return (
    <div data-testid={testid}>
      {now.missing.length ? <ReadinessPanel readiness={now} accepted={accepted} onAccept={accept} /> : null}
      {later.includes("deployment_target") ? (
        <p className="dw-ready dw-ready-note" data-testid="sample-later-deploy">
          <strong>Optional: a server to deploy to.</strong> It is not needed to build and open the PR.
          To deploy on this machine later, <a href={consoleHref({ area: "infra", view: "fleet", add: "host", new_project: undefined })} data-testid="sample-add-host">add it as a host in Infra</a>.
        </p>
      ) : null}
    </div>
  );
}
