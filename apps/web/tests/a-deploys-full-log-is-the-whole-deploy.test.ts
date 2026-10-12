import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { deployLogsHref } from "../lib/infra-view";
import { codeOnly } from "./_helpers/source";

// "Open full log" on a deploy showed "0 lines in 24h": the link limited the Log to the Deploys
// stream and the server compared the deploy REF to row ids. The server now reads the ref
// (infra_view.split_deploy_ref) and a deploy's steps, job, approval and receipt are its log.
describe("a deploy's full log", () => {
  it("carries the deploy ref as the subject, keeps the org, and does not narrow the stream", () => {
    expect(deployLogsHref("delivery:deprun_60553ea2600f40b39730", "?org=org_boss&area=infra"))
      .toBe("?org=org_boss&area=activity&subject=delivery%3Adeprun_60553ea2600f40b39730");
    expect(deployLogsHref("stack:sdep_1", "")).toBe("?area=activity&subject=stack%3Asdep_1");
  });

  it("is the link the deploy drawer's Open full log uses", () => {
    const drawer = codeOnly("app/infra/deploy-drawer.tsx");
    expect(drawer).toContain("href={deployLogsHref(row.ref)}");
    expect(drawer).not.toMatch(/logsHref\("deployment",\s*row\.ref\)/);
  });

  it("the server reads the same ref shape the console hands it", () => {
    const reader = readFileSync("../../engine/boss_engine/services/infra_view.py", "utf8");
    expect(reader).toMatch(/DEPLOY_REF_TABLES = \{"stack": "stack_deployments", "registry": "deployment_runs",\s*"delivery": "deployment_runs"\}/);
  });
});
