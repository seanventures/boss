// @vitest-environment happy-dom
/**
 * The apply board shows isolated copies. The shared head stays put until a
 * finished copy is applied. An unfinished copy says so, and a dead letter stays
 * off the rail until retry or rebase (BOSSAPP-1252).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("../lib/identity/api", () => ({
  idGet: vi.fn(),
  idPost: vi.fn(),
  refusalSentence: (error: unknown) => (error instanceof Error ? error.message : "That didn't work"),
}));

const { idGet, idPost } = await import("../lib/identity/api");
const { ApplyBoard } = await import("../app/builder/apply-board");

const UNFINISHED =
  "This copy is not finished. Finish it before it can touch the shared branch.";

const board = {
  id: "tcb_1",
  project_id: "p1",
  shared_branch: "main",
  shared_head: "head-main-100",
};

function copy(over: Record<string, string>) {
  return {
    id: "tcp_1",
    task_key: "LATCH",
    title: "Fix the latch",
    teammate: "Ada",
    copy_branch: "copy/main/LATCH/tcp_1",
    base_head: "head-main-100",
    copy_head: "head-main-100",
    status: "running",
    note: "",
    failure_reason: "",
    failure_kind: "",
    ...over,
  };
}

let host: HTMLDivElement | null = null;
let root: Root | null = null;

beforeEach(() => {
  vi.mocked(idGet).mockReset();
  vi.mocked(idPost).mockReset();
});

afterEach(() => {
  act(() => { root?.unmount(); });
  host?.remove();
  host = null;
  root = null;
});

async function render(boardId: string | null) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => {
    root!.render(<ApplyBoard projectId="p1" boardId={boardId} actor="Ada" />);
  });
  await act(async () => {
    await Promise.resolve();
  });
}

async function setInput(selector: string, value: string) {
  const input = host!.querySelector(selector) as HTMLInputElement;
  const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
  await act(async () => {
    proto!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("the apply board", () => {
  it("keeps a running copy off the shared head", async () => {
    vi.mocked(idGet).mockResolvedValue({ board, copies: [copy({})] });
    await render("tcb_1");
    expect(host!.querySelector("[data-testid='shared-head']")!.textContent).toBe("head-main-100");
    expect(host!.querySelector("[data-testid='shared-branch']")!.textContent).toBe("main");
    expect(host!.querySelector("[data-testid='copy-status-tcp_1']")!.textContent).toBe("Running");
    expect(host!.querySelector("[data-testid='unfinished-tcp_1']")!.tagName).toBe("P");
    expect(host!.querySelector("[data-testid='unfinished-tcp_1']")!.textContent).toBe(UNFINISHED);
    expect(host!.querySelector("[data-testid='apply-tcp_1']")).toBeNull();
    expect(idPost).not.toHaveBeenCalled();
  });

  it("applies a finished copy and the plate takes that head", async () => {
    const finished = copy({ status: "finished", note: "Latch holds.", copy_head: "head-ada-200" });
    vi.mocked(idGet).mockResolvedValue({ board, copies: [finished] });
    vi.mocked(idPost).mockResolvedValue({
      board: { ...board, shared_head: "head-ada-200" },
      copy: { ...finished, status: "applied" },
    });
    await render("tcb_1");
    const button = host!.querySelector("[data-testid='apply-tcp_1']") as HTMLButtonElement;
    expect(button.getAttribute("title")!.split(/\s+/).length).toBeLessThanOrEqual(14);
    await act(async () => { button.click(); });
    await act(async () => { await Promise.resolve(); });
    expect(idPost).toHaveBeenCalledWith(
      "/v1/projects/p1/task-copies/tcp_1/apply",
      { actor: "Ada" },
    );
    expect(host!.querySelector("[data-testid='shared-head']")!.textContent).toBe("head-ada-200");
    expect(host!.querySelector(".ab-stamp")!.textContent).toBe("Applied");
  });

  it("shows a failed run as a dead letter and retries without moving the head", async () => {
    const dead = copy({
      status: "dead",
      failure_kind: "run",
      failure_reason: "Tests failed on the copy.",
      copy_head: "head-ada-200",
    });
    vi.mocked(idGet).mockResolvedValue({ board, copies: [dead] });
    vi.mocked(idPost).mockResolvedValue({
      board,
      copy: { ...dead, status: "running", failure_kind: "", failure_reason: "" },
    });
    await render("tcb_1");
    const letter = host!.querySelector("[data-testid='dead-letter-tcp_1']")!;
    expect(letter.tagName).toBe("P");
    expect(letter.textContent).toBe("Tests failed on the copy.");
    expect(host!.querySelector("[data-testid='apply-tcp_1']")).toBeNull();
    const retry = host!.querySelector("[data-testid='retry-tcp_1']") as HTMLButtonElement;
    await act(async () => { retry.click(); });
    await act(async () => { await Promise.resolve(); });
    expect(idPost).toHaveBeenCalledWith(
      "/v1/projects/p1/task-copies/tcp_1/retry",
      { actor: "Ada" },
    );
    expect(host!.querySelector("[data-testid='shared-head']")!.textContent).toBe("head-main-100");
  });

  it("rebases a conflict instead of retrying it", async () => {
    const dead = copy({
      id: "tcp_bao",
      teammate: "Bao",
      status: "dead",
      failure_kind: "conflict",
      failure_reason: "The shared branch moved. This copy was left as a dead letter. Rebase it, then apply again.",
      copy_head: "head-bao-300",
    });
    vi.mocked(idGet).mockResolvedValue({
      board: { ...board, shared_head: "head-ada-200" },
      copies: [dead],
    });
    vi.mocked(idPost).mockResolvedValue({
      board: { ...board, shared_head: "head-ada-200" },
      copy: { ...dead, status: "finished", failure_kind: "", copy_head: "head-bao-300-on-ada", base_head: "head-ada-200" },
    });
    await render("tcb_1");
    expect(host!.querySelector("[data-testid='retry-tcp_bao']")).toBeNull();
    expect(host!.querySelector("[data-testid='shared-head']")!.textContent).toBe("head-ada-200");
    await setInput("[data-testid='rebase-head-tcp_bao']", "head-bao-300-on-ada");
    const rebase = host!.querySelector("[data-testid='rebase-tcp_bao']") as HTMLButtonElement;
    expect(rebase.getAttribute("title")!.split(/\s+/).length).toBeLessThanOrEqual(14);
    await act(async () => { rebase.click(); });
    await act(async () => { await Promise.resolve(); });
    expect(idPost).toHaveBeenCalledWith(
      "/v1/projects/p1/task-copies/tcp_bao/rebase",
      { actor: "Ada", copy_head: "head-bao-300-on-ada" },
    );
    expect(host!.querySelector("[data-testid='shared-head']")!.textContent).toBe("head-ada-200");
  });

  it("opens a board and starts a copy without changing the shared head", async () => {
    vi.mocked(idPost).mockImplementation(async (path: string) => {
      if (String(path).endsWith("/task-copy-boards")) {
        return { board, copies: [] };
      }
      return {
        board,
        copy: copy({ note: "", copy_head: "head-main-100" }),
      };
    });
    vi.mocked(idGet).mockResolvedValue({ board, copies: [] });
    await render(null);
    expect(host!.querySelector("[data-testid='shared-head']")).toBeNull();
    await setInput("[aria-label='Shared branch']", "main");
    await setInput("[aria-label='Starting head']", "head-main-100");
    await act(async () => {
      (host!.querySelector("[data-testid='open-board']") as HTMLButtonElement).click();
    });
    await act(async () => { await Promise.resolve(); });
    expect(host!.querySelector("[data-testid='shared-head']")!.textContent).toBe("head-main-100");
    await setInput("[aria-label='Task']", "LATCH");
    await setInput("[aria-label='Title']", "Fix the latch");
    await setInput("[aria-label='Teammate']", "Ada");
    await act(async () => {
      (host!.querySelector("[data-testid='start-copy']") as HTMLButtonElement).click();
    });
    await act(async () => { await Promise.resolve(); });
    expect(idPost).toHaveBeenCalledWith("/v1/projects/p1/task-copies", {
      actor: "Ada",
      board_id: "tcb_1",
      task_key: "LATCH",
      title: "Fix the latch",
      teammate: "Ada",
    });
    expect(host!.querySelector("[data-testid='shared-head']")!.textContent).toBe("head-main-100");
    expect(host!.querySelector("[data-testid='copy-tcp_1']")).not.toBeNull();
  });
});
