// @vitest-environment happy-dom
/**
 * A TYPED QUESTION'S FORM (BOSSAPP-832): the console builds exactly the answer the engine
 * validates (typed_forms.validate_answer), says what is missing in words, and Send answer is the
 * card's approve with `answer`.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TypedDecisionForm } from "../app/typed-form-ui";
import { answerLines, emptyDraft, missing, parseForm, toAnswer } from "../lib/typed-form";
import { $, $$, mount, settle, unmount } from "./_helpers/live-render";

const FORM = {
  fields: [
    { key: "db", type: "select", label: "Database", required: true,
      options: [{ value: "postgres", label: "Postgres" }, { value: "mysql", label: "MySQL" }] },
    { key: "why", type: "text", label: "Why", required: false },
    { key: "urgent", type: "checkbox", label: "Urgent", required: false },
    { key: "findings", type: "item_verdicts", label: "Findings", required: true,
      items: [{ id: "f1", label: "SQL injection" }, { id: "f2", label: "Weak hash" }], verdicts: ["accept", "reject"] },
    { key: "next", type: "task_suggestions", label: "Follow-ups", required: false,
      suggestions: [{ title: "Add rate limit" }, { title: "Rotate keys" }] },
    { key: "bogus", type: "slider", label: "Dropped" },
  ],
};

afterEach(() => { unmount(); vi.restoreAllMocks(); });

describe("the typed form contract", () => {
  it("reads the form, says what is missing and builds the engine's answer", () => {
    const form = parseForm(FORM)!;
    expect(form.fields.map((f) => f.key)).toEqual(["db", "why", "urgent", "findings", "next"]);
    const draft = emptyDraft(form);
    expect(missing(form, draft)).toBe("Database is needed");
    draft.db = "postgres";
    expect(missing(form, draft)).toBe("Findings: give every item a verdict");
    draft.findings = { f1: "accept", f2: "reject" };
    draft.next = [true, false];
    expect(missing(form, draft)).toBeNull();
    const answer = toAnswer(form, draft);
    expect(answer).toEqual({
      db: "postgres", urgent: false, findings: { f1: "accept", f2: "reject" },
      next: [{ index: 0, accepted: true }, { index: 1, accepted: false }],
    });
    expect(answerLines(form, { ...answer, _created_work_items: [{ id: "w" }] })).toEqual([
      "Database: Postgres", "Urgent: no", "Findings: 1 accept, 1 reject", "Follow-ups: 1 accepted",
      "1 work item created"]);
    expect(parseForm({ fields: [] })).toBeNull();
    expect(parseForm(null)).toBeNull();
  });
});

describe("the form on a card", () => {
  it("sends the typed answer as the approve, and only once it is complete", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
      return new Response(JSON.stringify({ id: "dec_1", status: "approved" }), { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    const answered = vi.fn();
    await mount(<TypedDecisionForm decisionId="dec_1" form={parseForm(FORM)!} projectId="p1" actorId="ceo" onAnswered={answered} />);
    const send = $('[data-testid="typed-send"]') as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect($('[data-testid="typed-gap"]')?.textContent).toBe("Database is needed");
    ($$('[data-testid="typed-field-db"] input[type="radio"]')[0] as HTMLInputElement).click();
    const verdicts = $$('[data-testid="typed-field-findings"] input[type="radio"]') as HTMLInputElement[];
    verdicts[0].click(); // f1 accept
    verdicts[3].click(); // f2 reject
    ($$('[data-testid="typed-field-next"] input[type="checkbox"]')[1] as HTMLInputElement).click(); // untick Rotate keys
    await settle();
    expect(($('[data-testid="typed-send"]') as HTMLButtonElement).disabled).toBe(false);
    ($('[data-testid="typed-send"]') as HTMLButtonElement).click();
    await settle();
    const sent = calls.find((c) => c.url.includes("/v1/decisions/dec_1/approve"));
    expect(sent?.body).toEqual({ actor: "ceo", answer: {
      db: "postgres", urgent: false, findings: { f1: "accept", f2: "reject" },
      next: [{ index: 0, accepted: true }, { index: 1, accepted: false }] } });
    expect(answered).toHaveBeenCalledWith("approved");
  });

  it("declines with a reason the agent reads", async () => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    await mount(<TypedDecisionForm decisionId="dec_2" form={parseForm(FORM)!} projectId="p1" actorId="ceo" />);
    ($('[data-testid="typed-decline"]') as HTMLButtonElement).click();
    await settle();
    expect(($('[data-testid="typed-decline-send"]') as HTMLButtonElement).disabled).toBe(true);
    const box = $('[data-testid="typed-decline-reason"]') as HTMLTextAreaElement;
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(box, "not my call");
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await settle();
    ($('[data-testid="typed-decline-send"]') as HTMLButtonElement).click();
    await settle();
    expect(calls.find((c) => c.url.includes("/v1/decisions/dec_2/reject"))?.body).toEqual({ actor: "ceo", note: "not my call" });
  });
});
