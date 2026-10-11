// @vitest-environment happy-dom
import React, { act, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectStrip } from "../app/project-strip";
import { ReadinessPanel, type Readiness } from "../app/workflow-readiness-ui";
import { $, click, mount, rerender, settle, unmount } from "./_helpers/live-render";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

describe("the secondary build row", () => {
  it("disappears only after the server confirms an empty complete list, and returns for new work", async () => {
    let body: unknown = { projects: [], total: 0 };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(body)));
    await mount(<ProjectStrip projectId="p1" />);
    expect($("[data-testid=bx-strip]")).toBeNull();
    body = { projects: [{ id: "1", title: "Review", status: "GATED", stage: "Review", pct: 50, latest_run_id: "run1" }], total: 1 };
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await settle();
    expect($("[data-testid=bx-strip]")?.textContent).toContain("1 need attention");
    await click($(".bx-project-trigger"));
    expect($("[data-testid=bx-strip-1]")?.textContent).toContain("Review");
  });


  it("shows a failed refresh even when the last successful list was empty", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(Response.json({ projects: [], total: 0 }))
      .mockResolvedValue(Response.json({ error: "Offline" }, { status: 503 }));
    vi.stubGlobal("fetch", fetch);
    await mount(<ProjectStrip projectId="p1" />);
    expect($("[data-testid=bx-strip]")).toBeNull();
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    await settle();
    expect($("[role=status]")?.textContent).toContain("Could not refresh — Offline");
  });

  it.each(["empty", "error", "reject"] as const)(
    "shows the new project's pending load and ignores the old refresh: %s", async (late) => {
      let finishOld!: (response: Response) => void;
      let rejectOld!: (error: Error) => void;
      let finishNew!: (response: Response) => void;
      const oldRefresh = new Promise<Response>((resolve, reject) => { finishOld = resolve; rejectOld = reject; });
      const newLoad = new Promise<Response>((resolve) => { finishNew = resolve; });
      const fetch = vi.fn().mockResolvedValueOnce(Response.json({ projects: [], total: 0 }))
        .mockReturnValueOnce(oldRefresh).mockReturnValueOnce(newLoad);
      vi.stubGlobal("fetch", fetch);
      await mount(<ProjectStrip projectId="A" />);
      expect($("[data-testid=bx-strip]")).toBeNull();
      await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
      await rerender(<ProjectStrip projectId="B" />);
      expect(fetch.mock.calls[2][0]).toContain("project_id=B");
      expect($("[data-state=loading]")?.textContent).toContain("Loading");
      await act(async () => {
        if (late === "reject") rejectOld(new Error("Old scope offline"));
        else finishOld(late === "error" ? Response.json({ error: "Old scope offline" }, { status: 503 })
          : Response.json({ projects: [], total: 0 }));
      });
      await settle();
      expect($("[data-state=loading]")?.textContent).toContain("Loading");
      expect(document.body.textContent).not.toContain("Old scope offline");
      await act(async () => { finishNew(Response.json({ error: "B offline" }, { status: 503 })); });
      await settle();
      expect($("[data-state=failed]")?.textContent).toContain("B offline");
    },
  );

  it("does not replace the new project's work with a late empty response", async () => {
    let finishOld!: (response: Response) => void;
    const oldLoad = new Promise<Response>((resolve) => { finishOld = resolve; });
    vi.stubGlobal("fetch", vi.fn().mockReturnValueOnce(oldLoad).mockResolvedValueOnce(Response.json({
      projects: [{ id: "b", title: "B review", status: "GATED", stage: "Review", pct: 50, latest_run_id: "runB" }], total: 1,
    })));
    await mount(<ProjectStrip projectId="A" />);
    await rerender(<ProjectStrip projectId="B" />);
    await act(async () => { finishOld(Response.json({ projects: [], total: 0 })); });
    await settle();
    expect($("[data-testid=bx-strip]")?.textContent).toContain("1 need attention");
    await click($(".bx-project-trigger"));
    expect($("[data-testid=bx-strip-b]")?.textContent).toContain("B review");
  });

  it("retains loading, failure, and an unconfirmed empty list", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    await mount(<ProjectStrip projectId="p1" />);
    expect($("[data-state=loading]")?.textContent).toContain("Loading");
    unmount();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "Unavailable" }, { status: 503 })));
    await mount(<ProjectStrip projectId="p1" />);
    expect($("[data-state=failed]")?.textContent).toContain("Unavailable");
    unmount();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ projects: [] })));
    await mount(<ProjectStrip projectId="p1" />);
    expect($("[data-testid=bx-strip]")).not.toBeNull();
  });
});

it("keeps missing requirements and acceptance visible while setup instructions can expand", async () => {
  const need = { key: "deployment_target", label: "A verified deployment host", fix: "Add and verify a host, then select it for the app.",
    area: "delivery", href: "/?area=delivery", ok: false, detail: "No verified host", needed_by: [{ step_id: "deploy", label: "Deploy" }],
    first_step_label: "Deploy", after_a_person: true };
  const readiness: Readiness = { ready: false, missing: [need], checked: [need] };
  function Form() {
    const [accepted, setAccepted] = useState<string[]>([]);
    return <ReadinessPanel readiness={readiness} accepted={accepted} onAccept={setAccepted} />;
  }
  await mount(<Form />);
  const row = $("[data-testid=dw-readiness-missing-deployment_target]")!;
  expect(row.querySelector("strong")?.textContent).toBe(need.label);
  const details = row.querySelector("details")!;
  expect(details.open).toBe(false);
  expect(details.querySelector("summary")?.textContent).toBe("Setup details");
  expect(details.textContent).toContain(need.fix);
  expect(row.querySelector("a")?.closest("details")).toBeNull();
  expect($("[data-testid=dw-readiness-start-anyway]")?.closest("details")).toBeNull();
  await click($("[data-testid=dw-readiness-start-anyway]"));
  expect(row.dataset.accepted).toBe("true");
  await click($("[data-testid=dw-readiness-start-anyway]"));
  expect(row.dataset.accepted).toBeUndefined();
});
