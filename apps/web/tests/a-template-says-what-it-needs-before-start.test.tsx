import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { composerFields } from "../app/delivery-workflows-ui";
import {
  ReadinessPanel, WIZARD_COLLECTS, unaccepted, type Readiness, type ReadinessNeed,
} from "../app/workflow-readiness-ui";
import { codeOnly } from "./_helpers/source";

(globalThis as Record<string, unknown>).React = React;

/**
 * What a template needs is on screen BEFORE Start — boss-1 map, 2026-10-02.
 *
 * Every catalogue template but one ends in `deploy`, and an install with no deployment host
 * started them anyway: research, a panel, a build and a repair were paid for, a person approved
 * the release, and the run failed at its last step. The email examples failed the same way at
 * an approved send with no mail server. The engine's preflight now refuses the start; this is
 * the form drawing that answer, with the fix for each thing, before anything is pressed.
 */

const UI = codeOnly("app/delivery-workflows-ui.tsx");
const ROUTE = codeOnly("app/api/delivery-workflows/route.ts");
const TRACKERS = codeOnly("app/work-orchestration-ui.tsx");

function need(over: Partial<ReadinessNeed>): ReadinessNeed {
  return { key: "deployment_target", label: "Somewhere to deploy: a verified deployment host",
           fix: "Add and verify a host in Infra → Where apps install.", area: "delivery",
           ok: false, detail: "this project has no verified deployment host and no active target",
           needed_by: [{ step_id: "deploy", label: "Deploy" }], first_step_label: "Deploy",
           after_a_person: true, ...over };
}

const NOT_READY: Readiness = {
  ready: false,
  checked: [need({}), need({ key: "model", label: "A connected model", ok: true,
                             detail: "a model is connected" })],
  missing: [need({}), need({ key: "email", label: "Outgoing email (SMTP)", area: null,
                             fix: "Configure SMTP: POST /v1/email/config …",
                             needed_by: [{ step_id: "send", label: "Email the newsletter" }],
                             after_a_person: false, first_step_label: "Email the newsletter" })],
};

describe("the start form shows what is missing, with the fix", () => {
  const html = renderToStaticMarkup(
    <ReadinessPanel readiness={NOT_READY} accepted={[]} onAccept={() => {}} />);

  it("names each missing thing, the step that needs it and how to fix it", () => {
    expect(html).toContain("This cannot run here yet");
    expect(html).toContain("Somewhere to deploy: a verified deployment host");
    expect(html).toContain("needed by Deploy");
    expect(html).toContain("Add and verify a host in Infra");
    expect(html).toContain("Outgoing email (SMTP)");
    expect(html).toContain("POST /v1/email/config");
  });

  it("a fix that lives in the console links to that area", () => {
    expect(html).toMatch(/href="\/\?area=delivery"/);
  });

  it("says when the run stops for a person first, so it can be added on the way", () => {
    expect(html).toContain("The run stops for you before Deploy");
  });

  it("offers starting anyway as an explicit choice", () => {
    expect(html).toContain('data-testid="dw-readiness-start-anyway"');
  });

  it("a ready project says so instead of drawing a list", () => {
    const ok = renderToStaticMarkup(
      <ReadinessPanel readiness={{ ready: true, checked: [need({ ok: true })], missing: [] }}
                      accepted={[]} onAccept={() => {}} />);
    expect(ok).toContain("Ready to run here");
    expect(ok).not.toContain("This cannot run here yet");
  });

  it("only what the operator accepted stops blocking", () => {
    expect(unaccepted(NOT_READY, []).map((m) => m.key)).toEqual(["deployment_target", "email"]);
    expect(unaccepted(NOT_READY, ["deployment_target"]).map((m) => m.key)).toEqual(["email"]);
    expect(unaccepted(NOT_READY, ["deployment_target", "email"])).toEqual([]);
  });
});

describe("Start is blocked by what is missing, and says start-anyway out loud", () => {
  it("the composer asks before Start and blocks on what was not accepted", () => {
    expect(UI).toContain("useWorkflowReadiness(projectId, draft.workflowKey, inputsToSend)");
    expect(UI).toContain("notReady.length");
    expect(UI).toContain("start_anyway: startAnyway");
  });

  it("a field required on a later step blocks Start, never Next on the step before it", () => {
    // Deprecation / Sunset requires "Who to tell", drawn on the path step; blocking Next on
    // the brief step for it would be a form nobody can reach the end of.
    expect(UI).toContain('data-testid="dw-wiz-next"\n                    disabled={!!stepBlocked}');
    expect(UI).toContain('data-testid="dw-new-start"\n                  disabled={busy || !!blocked}');
  });

  it("a refused start draws the same checklist instead of [object Object]", () => {
    expect(ROUTE).toContain("readiness: d.readiness");
    expect(UI).toContain("setRefusal(r.readiness)");
  });

  it("the readiness view is proxied", () => {
    expect(ROUTE).toContain('view === "readiness"');
    expect(ROUTE).toContain("/readiness`");
  });
});

describe("a catalogue template is not asked for the same thing twice", () => {
  const catalogueSchema = {
    required: ["directive", "title"],
    properties: {
      directive: { title: "What should it do?", format: "textarea" },
      title: { title: "Name" },
      repository: { title: "Repository (optional)" },
      notify_channel: { title: "Who to tell" },
    },
  };

  it("the wizard's own steps collect the brief, the name and the repository", () => {
    const names = composerFields(catalogueSchema, true).map((f) => f.name);
    expect(names).toEqual(["notify_channel"]);
    for (const field of ["title", "directive", "repository"]) {
      expect(WIZARD_COLLECTS.has(field)).toBe(true);
    }
  });

  it("but a workflow somebody made keeps every field — its title may be a YouTube title", () => {
    const names = composerFields(catalogueSchema, false).map((f) => f.name);
    expect(names).toEqual(["directive", "title", "repository", "notify_channel"]);
  });
});

describe("a tracker says whether it is connected", () => {
  it("the row reads connected or not connected, not just default or available", () => {
    expect(TRACKERS).toContain('provider.connected ? "connected" : "not connected"');
  });
});
