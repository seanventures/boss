/**
 * NEEDS YOUR CONSENT IS A QUESTION, NOT A FAILURE, in every server action.
 *
 * `act()` (lib/server-action.ts) turns a 409 consent_required into `{ok: false, data: {consent}}`,
 * which callAction answers by opening the consent sheet and sending again. A write action with its
 * own try/catch bypassed that: Change it's Start swallowed the consent request into an error line
 * the person could not answer (enterprise validation, Change it sheet). Every hand-written catch in
 * a write action now goes through consentRefusal (or consentOfThrown) before it says an error.
 * Reads (get/list/load/fetch/search) never ask for consent and are left alone.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(join(__dirname, "../app/actions.ts"), "utf8");
const READ = /^(get|list|load|fetch|search|read)[A-Z]/;

/** Each exported action's name and body (up to the next exported action). */
function actions(): { name: string; body: string }[] {
  const out: { name: string; body: string }[] = [];
  const re = /\nexport async function (\w+)\(/g;
  const hits = [...SRC.matchAll(re)];
  hits.forEach((m, i) => {
    out.push({ name: m[1], body: SRC.slice(m.index!, i + 1 < hits.length ? hits[i + 1].index : SRC.length) });
  });
  return out;
}

/** The text of each `catch (x) { ... }` block, brace-matched. */
function catches(body: string): string[] {
  const out: string[] = [];
  for (const m of body.matchAll(/catch \(\w+\) \{/g)) {
    let depth = 1;
    let i = m.index! + m[0].length;
    for (; i < body.length && depth; i += 1) {
      if (body[i] === "{") depth += 1;
      else if (body[i] === "}") depth -= 1;
    }
    out.push(body.slice(m.index!, i));
  }
  return out;
}

describe("every write action's catch answers consent", () => {
  it("finds the actions it holds to the rule", () => {
    const writes = actions().filter((a) => !READ.test(a.name) && catches(a.body).length);
    expect(writes.length).toBeGreaterThan(20);
  });

  it("no write action turns a consent request into a plain error", () => {
    const offenders = actions()
      .filter((a) => !READ.test(a.name))
      .filter((a) => catches(a.body).some((c) =>
        /return\s*\{[^}]*\berror\b/.test(c) && !/consentRefusal\(|consentOfThrown\(|\bthrow\b/.test(c)))
      .map((a) => a.name);
    expect(offenders).toEqual([]);
  });

  it("the refusal is the shape callAction opens the consent sheet from", () => {
    expect(SRC).toMatch(/return consent \? \{ ok: false, error: "Needs your consent", data: \{ consent \} \} : null;/);
  });
});
