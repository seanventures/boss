// @vitest-environment happy-dom
/**
 * Setup › Repo and models › Models is the ORG's (org_models, banking case study 2026-10-24: new
 * projects had no model). A job chosen for the org reads "All projects"; a job pinned for this
 * project reads "This project" with "Use org's", which removes only the pin (scope=project); an
 * org connection reads "All projects" too.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { click, fakeApi, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; vi.restoreAllMocks(); });

const open = "?area=setup&focus=routing";

describe("Setup › Models are the org's", () => {
  it("an org choice reads All projects; a pin reads This project and is removable", async () => {
    const view = board();
    view.jobs = view.jobs.map((j) => j.job === "build"
      ? { ...j, choice: { engine: "claude_code", model: "sonnet" }, scope: "org" as const }
      : j.job === "review" ? { ...j, scope: "project" as const } : j);
    const api = fakeApi(view);
    mounted = await mountBoard(view, open);
    expect(q(document.body, "su-job-scope-build")!.textContent).toBe("All projects");
    expect(q(document.body, "su-job-scope-review")!.textContent).toBe("This project");
    expect(q(document.body, "su-job-unpin-build")).toBeNull();
    await click(q(document.body, "su-job-unpin-review"));
    const sent = api.calls.filter((c) => c.op === "job").at(-1)?.body;
    expect(sent).toMatchObject({ job: "review", choice: null, scope: "project" });
  });

  it("choosing a model for a job writes the org's choice (no scope sent)", async () => {
    const view = board();
    const api = fakeApi(view);
    mounted = await mountBoard(view, open);
    const select = q(document.body, "su-job-select-chat") as HTMLSelectElement;
    select.value = "claude_code::sonnet";
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const sent = api.calls.filter((c) => c.op === "job").at(-1)?.body;
    expect(sent).toMatchObject({ job: "chat", choice: { engine: "claude_code", model: "sonnet" } });
    expect(sent && "scope" in sent).toBe(false);
  });

  it("an org connection reads All projects", async () => {
    const view = board();
    view.models = view.models.map((m, i) => (i === 0 ? { ...m, scope: "org" as const } : m));
    fakeApi(view);
    mounted = await mountBoard(view, "?area=setup&focus=models");
    expect(q(document.body, "su-model-scope")?.textContent).toBe("All projects");
  });
});
