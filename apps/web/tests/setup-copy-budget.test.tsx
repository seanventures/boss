// @vitest-environment happy-dom
/**
 * Tooltips, not paragraphs — Repo and models held to the Studio's copy budget, by rendering.
 * No visible run over six words outside a tooltip, placeholder or the operator's own text; no
 * <p> inside the board; none of the words that narrate BOSS's plumbing instead of the
 * operator's state.
 */
import { afterEach, describe, expect, it } from "vitest";

import { overBudget } from "./_helpers/copy-budget";
import { click, fakeApi, mountBoard, q, type Mounted } from "./_helpers/setup-board";
import { board, everyRed, firstRun, repoDone, tryStep } from "./_helpers/setup-fixtures";

const BANNED = ["environment variable", " env ", "harness", "entitlement", "binding", "governed",
                "verification state", " lane", "inherited", "deployment default", "BOSS_",
                "--connect", "container"];

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

function audit(root: HTMLElement) {
  expect(overBudget(root)).toEqual([]);
  // Tooltips have a budget too: fourteen words (the "Other ways" tip was fifteen).
  const long = [...root.querySelectorAll("[data-tip]")]
    .map((el) => el.getAttribute("data-tip") ?? "")
    .filter((tip) => tip.split(/\s+/).filter(Boolean).length > 14);
  expect(long).toEqual([]);
  expect(root.querySelectorAll(".su-board p, .su-drawer p").length).toBe(0);
  // A placeholder fits a phone's field (the Quick PR one was cut to "…fix a READM" at 390px).
  const wide = [...root.querySelectorAll("[placeholder]")]
    .map((el) => el.getAttribute("placeholder") ?? "").filter((p) => p.length > 32);
  expect(wide).toEqual([]);
  const visible = ` ${root.textContent ?? ""} `;
  for (const word of BANNED) expect(visible.toLowerCase()).not.toContain(word.toLowerCase());
}

describe("the board stays within the copy budget", () => {
  for (const [name, make] of [["first run", firstRun], ["model step", repoDone],
                              ["try step", tryStep], ["board", board],
                              ["every row red", everyRed]] as const) {
    it(name, async () => {
      fakeApi(make());
      mounted = await mountBoard(make());
      audit(document.body);
    });
  }

  it("with the embedding model open, unset, working and failed", async () => {
    const providers = [
      { provider: "azure", label: "Azure OpenAI", needs_key: true, fields: ["url", "api_key"],
        more: ["deployment", "api_version", "dimension"] },
      { provider: "ollama", label: "Ollama (local)", needs_key: false, fields: ["url", "model"], more: ["dimension"] },
    ];
    const none = { key: "knowledge.embedding_model", configured: false, permission: "model_provider:manage",
                   budget_category: "knowledge", search: false, providers,
                   reason: "meaning-based search is off: no embedding model is set (Setup › Repo and models › Embedding model)" };
    const failed = { ...none, configured: true, provider: "azure", label: "Azure OpenAI", model: "embed-small",
                     dimension: 1536, has_key: true,
                     check: { state: "failed" as const, error: "AuthenticationError: 401 the key was refused by the provider" },
                     reason: "meaning-based search is off: the embedding model check failed: AuthenticationError: 401 the key was refused by the provider" };
    const working = { ...failed, search: true, reason: "", reembed_pending: 40,
                      check: { state: "ok" as const, reply: "1536 numbers from embed-small" } };
    for (const slot of [none, failed, working]) {
      const view = { ...board(), embedding_model: slot };
      fakeApi(view);
      mounted = await mountBoard(view, "?area=setup&focus=embedding_model");
      if (slot.configured) await click(q(document.body, "su-embedding-change"));
      await click(q(document.body, "su-embedding-provider-azure"));
      audit(document.body);
      await click(q(document.body, "su-embedding-more"));             // and with More open
      audit(document.body);
      mounted.unmount();
      mounted = null;
    }
  });

  it("a model that does every job", async () => {
    const view = board();
    view.models = [{ ...view.models[0], used_for: ["Build", "Review", "Chat", "Decisions",
                                                   "Hard problems", "Routine work"] }];
    fakeApi(view);
    mounted = await mountBoard(view);
    audit(document.body);
    const used = q(document.body, "su-model-used")!;
    expect(used.textContent).toBe("Used for Build · Review · Chat +3");
    expect(used.getAttribute("data-tip")).toContain("Hard problems, Routine work");
  });

  it("with More jobs open and the budget being edited", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "su-more-jobs"));
    await click(q(document.body, "su-budget-edit"));
    audit(document.body);
  });

  it("with Add a model open on every tile and provider", async () => {
    fakeApi(board());
    mounted = await mountBoard(board());
    await click(q(document.body, "su-add-model-open"));
    for (const tile of ["subscription", "api_key", "local"]) {
      await click(q(document.body, `su-tile-${tile}`));
      const providers = [...document.body.querySelectorAll('[data-testid^="su-provider-"]')];
      for (const chip of providers) {
        await click(chip);
        audit(document.body);
      }
    }
  });

  it("with Connect GitHub open, and when consent is needed", async () => {
    const view = { ...board(), repository: { connection: null, repos: [] },
                   secrets: { backend: "database", needs_consent: true } };
    fakeApi(view);
    mounted = await mountBoard(view);
    await click(q(document.body, "su-connect-github"));
    expect(q(document.body, "su-consent")).not.toBeNull();
    audit(document.body);
  });
});
