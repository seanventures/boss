// @vitest-environment happy-dom
/**
 * The app drawer's Alerts tab (BOSSAPP-598): rules, signal sources and history in one place;
 * the edit-rule sheet and the silence sheet; every fired alert's PromQL, value at breach,
 * Explore link, runbook and the agent's words; Respond automatically per rule, asking consent.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CONSENT_EVENT, type ConsentRequest } from "../lib/identity/consent";
import { parseAlert, parseRule, parseRulesView } from "../lib/app-alerts";
import { FIRING_ALERT, NO_MODEL_ALERT, NOISE_ALERT, SIGNAL_ALERT, historyWire, ruleWire, rulesWire } from "./_helpers/alerts-fixtures";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

async function type(el: Element | null, value: string) {
  const input = el as HTMLInputElement | HTMLTextAreaElement;
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await settle();
}

describe("the wire", () => {
  it("parses rules and alerts as the server sends them", () => {
    const r = parseRule(ruleWire("error_rate"));
    expect(r.condition).toBe("> 5% for 5 min (≥ 20 req)");
    expect(r.autoRespond).toBe(true);
    expect(r.runbook.kind).toBe("builtin");
    const a = parseAlert(FIRING_ALERT);
    expect(a.expr).toContain("http_requests_total");
    expect(a.triage.reason).toContain("release 6bf2673");
    expect(parseRulesView(rulesWire()).sources[0].reportsToday).toBe(7);
  });
});

describe("the Alerts tab", () => {
  it("opens from the URL (Factory ⋯ › Alerts) and shows rules, sources and history", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts");
    expect($("[data-testid='obs-app-tab-alerts']")?.getAttribute("aria-selected")).toBe("true");
    const rules = $$("[data-testid^='obs-rule-'][data-state]");
    expect(rules.map((r) => r.dataset.testid)).toEqual(["obs-rule-error_rate", "obs-rule-latency_p95", "obs-rule-health_check",
                                                        "obs-rule-no_traffic", "obs-rule-deploy_health"]);
    const er = $("[data-testid='obs-rule-error_rate']")!;
    expect(text(er)).toContain("Error rate");
    expect(text(er)).toContain("> 5% for 5 min (≥ 20 req)");
    expect(text(er.querySelector("[data-testid='obs-rule-state']"))).toBe("Firing");
    expect(er.querySelector("[data-testid='obs-rule-respond']")?.getAttribute("aria-checked")).toBe("true");
    expect($("[data-testid='obs-rule-no_traffic'] [data-testid='obs-rule-respond']")?.hasAttribute("disabled")).toBe(true);
    expect(text($("[data-testid='obs-rules-source']"))).toContain("Evaluated by BOSS's own monitoring");
    expect(text($("[data-testid='obs-rules-source']"))).toContain("changed by Param");
    expect(text($("[data-testid='obs-source-t_desk']"))).toContain("HelpDesk AI");
    expect(text($("[data-testid='obs-source-t_desk']"))).toContain("7 today");
    const hist = $$("[data-testid^='obs-alert-alrt_']");
    expect(hist).toHaveLength(3);
    expect(text(hist[0])).toContain("14.4%");
    expect(text(hist[0])).toContain("6bf2673");
    expect(text(hist[0])).toContain("Agent: Opened an incident");
  });

  it("switching tabs keeps the drawer and writes atab to the URL", async () => {
    await openObs("?view=apps&app=t_down");
    expect($("[data-testid='obs-app-alerts']")).toBeNull();
    await click($("[data-testid='obs-app-tab-alerts']"));
    await settle();
    expect(params().get("atab")).toBe("alerts");
    expect($("[data-testid='obs-app-alerts']")).not.toBeNull();
    await click($("[data-testid='obs-app-tab-overview']"));
    expect(params().get("atab")).toBeNull();
  });

  it("a fired alert carries its PromQL, value at breach, Explore link, runbook and the agent's words", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts");
    const row = $("[data-testid='obs-alert-alrt_err']")!;
    await click(row.querySelector("[data-testid='obs-alert-open']"));
    const d = row.querySelector("[data-testid='obs-alert-detail']")!;
    expect(text(d.querySelector("[data-testid='obs-alert-expr']"))).toBe(FIRING_ALERT.expr);
    expect(text(d.querySelector("[data-testid='obs-alert-value']"))).toBe("14.4%");
    expect(d.querySelector("[data-testid='obs-alert-explore']")?.getAttribute("href")).toBe(FIRING_ALERT.explore_url);
    expect(text(d.querySelector("[data-testid='obs-alert-triage']"))).toContain(FIRING_ALERT.triage.reason);
    await click(d.querySelector("[data-testid='obs-alert-runbook']"));
    expect(text(d.querySelector("[data-testid='obs-alert-runbook-md']"))).toContain("Which release is serving?");
    // an alert without a reachable Grafana says why instead of linking to a dead page
    const noise = $("[data-testid='obs-alert-alrt_lat']")!;
    await click(noise.querySelector("[data-testid='obs-alert-open']"));
    const why = noise.querySelector("[data-testid='obs-alert-explore']")!;
    expect(why.tagName).toBe("SPAN");
    expect(why.getAttribute("title")).toBe("Grafana isn't answering on its host");
  });

  it("no model: said plainly, with Ask again", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", { history: () => historyWire([NO_MODEL_ALERT]) });
    const row = $("[data-testid='obs-alert-alrt_nomodel']")!;
    expect(text(row.querySelector("[data-testid='obs-alert-triage-word']"))).toBe("Agent: Not triaged: no model is answering");
    await click(row.querySelector("[data-testid='obs-alert-open']"));
    await click(row.querySelector("[data-testid='obs-alert-triage-again']"));
    await settle();
    expect(rec.writes.find((w) => w.url === "/v1/apps/t_down/alerts/alrt_nomodel/triage")?.body).toEqual({ project_id: "p1", actor: "ceo" });
  });

  it("a report from a signal source is in the history under its app", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts", { history: () => historyWire([SIGNAL_ALERT]) });
    expect(text($("[data-testid='obs-alert-alrt_sig']"))).toContain("Agent: Joined the incident");
  });

  it("Respond automatically is per rule, and asks for consent once", async () => {
    let puts = 0;
    const consent = { error: "consent_required", scope: "schedule", task: "Respond automatically when AppErrorRate fires",
                      task_run_id: null, agents: [{ id: "agt_sre", name: "SRE", role_key: "sre" }], permissions: ["workflow_run:start"],
                      projects: ["p1"], durations: [{ kind: "standing", label: "Standing", days: 30 }], max_days: 30 };
    const asked: ConsentRequest[] = [];
    const onConsent = (e: Event) => { const d = (e as CustomEvent<ConsentRequest>).detail; asked.push(d); d.onDecided?.("dlg_1"); };
    window.addEventListener(CONSENT_EVENT, onConsent);
    try {
      const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
        rules: (id) => rulesWire(id, { rules: [ruleWire("error_rate", { auto_respond: false }), ruleWire("latency_p95")] }),
      });
      const fetchFn = globalThis.fetch as unknown as (i: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
      vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input) === "/v1/apps/t_down/alert-rules" && init?.method === "PUT" && puts++ === 0) {
          rec.writes.push({ url: String(input), method: "PUT", body: JSON.parse(String(init.body)) });
          return new Response(JSON.stringify(consent), { status: 409, headers: { "content-type": "application/json" } });
        }
        return fetchFn(input, init);
      }));
      await click($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-respond']"));
      await settle();
      expect(asked.map((a) => a.task)).toEqual(["Respond automatically when AppErrorRate fires"]);
      const writes = rec.writes.filter((w) => w.url === "/v1/apps/t_down/alert-rules");
      expect(writes).toHaveLength(2);
      expect(writes[1].body).toEqual({ project_id: "p1", actor: "ceo", rules: [{ id: "arule_error_rate", auto_respond: true }] });
    } finally {
      window.removeEventListener(CONSENT_EVENT, onConsent);
    }
  });

  it("Rotate token asks once more, then rotates, and never shows the token", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
      writes: { "/rotate": { body: { target_id: "t_desk", rotated_at: "x", word: "Rotated. Redeploy HelpDesk AI so it has the new token." } } },
    });
    const btn = () => $("[data-testid='obs-source-rotate']");
    await click(btn());
    expect(text(btn())).toBe("Confirm rotate");
    expect(rec.writes).toHaveLength(0);
    await click(btn());
    await settle();
    expect(rec.writes[0].url).toBe("/v1/apps/t_down/signal-sources/t_desk/rotate");
  });
});

describe("the edit-rule sheet", () => {
  it("changes a built-in rule by its settings; the next save carries the new threshold", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts");
    await click($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-edit']"));
    const sheet = $("[data-testid='obs-rule-sheet']")!;
    expect(sheet.getAttribute("aria-label")).toBe("Error rate alert");
    expect(text(sheet)).toContain("errors (5xx) are above");
    expect(text(sheet.querySelector("[data-testid='obs-rule-promql']"))).toBe(FIRING_ALERT.expr);
    // Notify: who is told is chosen from the contact points (Observability › Notify adds them).
    expect(text(sheet.querySelector("[data-testid='obs-rule-notify-none']"))).toMatch(/^Console/);
    await type(sheet.querySelector("[data-testid='obs-rule-threshold']"), "3");
    await type(sheet.querySelector("[data-testid='obs-rule-reason']"), "checkout matters");
    await click(sheet.querySelector("[data-testid='obs-rule-sev-sev3']"));
    await click(sheet.querySelector("[data-testid='obs-rule-save']"));
    await settle();
    const put = rec.writes.find((w) => w.method === "PUT");
    expect(put?.url).toBe("/v1/apps/t_down/alert-rules");
    expect(put?.body).toEqual({
      project_id: "p1", actor: "ceo", reason: "checkout matters",
      rules: [{ id: "arule_error_rate", params: { threshold: 3, window_minutes: 5, min_requests: 20 }, for: "0m", keep_firing_for: "0m", severity: "sev3",
                enabled: true, triage: true, auto_respond: true, runbook_url: "boss:runbook/app-serving" }],
    });
    expect($("[data-testid='obs-rule-sheet']")).toBeNull();
  });

  it("says the server's refusal in words and stays open", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts", {
      writes: { "/alert-rules": { status: 422, body: { detail: "threshold must be between 0.1 and 100" } } },
    });
    await click($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-edit']"));
    await type($("[data-testid='obs-rule-threshold']"), "400");
    await click($("[data-testid='obs-rule-save']"));
    await settle();
    expect(text($("[data-testid='obs-rule-error']"))).toBe("threshold must be between 0.1 and 100");
    expect($("[data-testid='obs-rule-sheet']")).not.toBeNull();
  });

  it("shows SEV1–SEV4 to choose from, each with what it means", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts");
    await click($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-edit']"));
    const sheet = $("[data-testid='obs-rule-sheet']")!;
    expect(["sev1", "sev2", "sev3", "sev4"].map((k) => !!sheet.querySelector(`[data-testid='obs-rule-sev-${k}']`))).toEqual([true, true, true, true]);
    expect((sheet.querySelector("[data-testid='obs-rule-sev-sev1']") as HTMLInputElement).checked).toBe(true);
    expect(sheet.querySelector("[data-testid='obs-rule-keep_firing_for']")).not.toBeNull();
    expect(sheet.querySelector("[data-testid='obs-rule-facts']")).not.toBeNull();
  });
});

const METRICS = {
  selector: 'app="api",environment="prod"', word: "12 series names from the last scrape", evaluator: "Prometheus (bundled)",
  metrics: [
    { name: "orders_created_total", type: "counter", labels: ["result"], help: "Orders created" },
    { name: "http_request_duration_seconds", type: "histogram", labels: ["method", "route"], help: null },
    { name: "app_uptime_seconds", type: "gauge", labels: [], help: null },
  ],
};

describe("New alert rule (the builder)", () => {
  const wait = () => new Promise((r) => setTimeout(r, 700));

  it("picks a metric the app reports, writes the PromQL, previews 24 h, and saves a rule of your own", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
      gets: { "/v1/apps/t_down/metric-names": () => METRICS },
      writes: { "/alert-rules/preview": { body: { fired: 3, word: "Would have fired 3 times in the last 24 h", series_now: 0 } } },
    });
    await click($("[data-testid='obs-rule-add']"));
    const sheet = $("[data-testid='obs-rule-builder']")!;
    expect(sheet.getAttribute("aria-label")).toBe("New alert rule");
    expect(rec.calls.some((u) => u.startsWith("/v1/apps/t_down/metric-names?project_id=p1"))).toBe(true);
    expect(sheet.querySelectorAll("#obs-builder-metrics option")).toHaveLength(3);
    await type(sheet.querySelector("[data-testid='obs-builder-metric']"), "orders_created_total");
    expect(text(sheet.querySelector("[data-testid='obs-builder-type']"))).toBe("counter");
    await type(sheet.querySelector("[data-testid='obs-builder-filter']"), 'result="error"');
    await type(sheet.querySelector("[data-testid='obs-builder-threshold']"), "0.5");
    expect(text(sheet.querySelector("[data-testid='obs-builder-promql']")))
      .toBe('sum(rate(orders_created_total{app="api",environment="prod",result="error"}[5m])) > 0.5');
    expect((sheet.querySelector("[data-testid='obs-builder-name']") as HTMLInputElement).value).toBe("OrdersCreatedTotalHigh");
    await wait();
    await settle();
    expect(text(sheet.querySelector("[data-testid='obs-builder-preview-word']"))).toBe("Would have fired 3 times in the last 24 h");
    const pv = rec.writes.filter((w) => w.url === "/v1/alert-rules/preview").at(-1)!;
    expect(pv.body).toMatchObject({ project_id: "p1", subject_id: "t_down", for: "2m",
                                    expr: 'sum(rate(orders_created_total{app="api",environment="prod",result="error"}[5m])) > 0.5' });
    await click(sheet.querySelector("[data-testid='obs-builder-sev-sev2']"));
    await click(sheet.querySelector("[data-testid='obs-builder-save']"));
    await settle();
    const put = rec.writes.find((w) => w.method === "PUT")!;
    expect(put.url).toBe("/v1/apps/t_down/alert-rules");
    expect((put.body as { rules: unknown[] }).rules[0]).toMatchObject({
      alert: "OrdersCreatedTotalHigh", for: "2m", labels: { severity: "sev2" }, enabled: true,
      expr: 'sum(rate(orders_created_total{app="api",environment="prod",result="error"}[5m])) > 0.5' });
  });

  it("offers p95 for a histogram, and Edit PromQL hands the query to an expert", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
      gets: { "/v1/apps/t_down/metric-names": () => METRICS },
      writes: { "/alert-rules/preview": { status: 422, body: { detail: "Only Prometheus evaluates predict_linear" } } },
    });
    await click($("[data-testid='obs-rule-add']"));
    const sheet = $("[data-testid='obs-rule-builder']")!;
    await type(sheet.querySelector("[data-testid='obs-builder-metric']"), "http_request_duration_seconds");
    const aggs = [...sheet.querySelectorAll<HTMLOptionElement>("[data-testid='obs-builder-agg'] option")].map((o) => o.value);
    expect(aggs).toEqual(["p95", "rate"]);
    await type(sheet.querySelector("[data-testid='obs-builder-threshold']"), "0.8");
    expect(text(sheet.querySelector("[data-testid='obs-builder-promql']")))
      .toBe('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{app="api",environment="prod"}[5m]))) > 0.8');
    await click(sheet.querySelector("[data-testid='obs-builder-expert']"));
    const ta = sheet.querySelector("[data-testid='obs-builder-expr']") as HTMLTextAreaElement;
    expect(ta.value).toContain("histogram_quantile(0.95");
    expect(sheet.querySelector("[data-testid='obs-builder-metric']")).toBeNull();
    await type(ta, 'predict_linear(x{app="api"}[1h], 3600) > 1');
    await wait();
    await settle();
    expect(text(sheet.querySelector("[data-testid='obs-builder-preview-word']"))).toBe("Only Prometheus evaluates predict_linear");
    expect(rec.writes.filter((w) => w.url === "/v1/alert-rules/preview").at(-1)?.body)
      .toMatchObject({ expr: 'predict_linear(x{app="api"}[1h], 3600) > 1' });
  });
});

describe("rules as queries", () => {
  it("every rule shows its PromQL and what decides when it fires, labelled with tooltips", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts");
    const er = $("[data-testid='obs-rule-error_rate']")!;
    expect(text(er.querySelector("[data-testid='obs-rule-query']"))).toBe(FIRING_ALERT.expr);
    const f = (id: string) => er.querySelector(`[data-testid='obs-fact-${id}']`)!;
    expect(text(f("every"))).toBe("Every1 min");
    expect(f("every").getAttribute("title")).toMatch(/how often/i);
    expect(text(f("window"))).toBe("Window5m");
    expect(text(f("for"))).toBe("Fornone");
    expect(f("for").getAttribute("title")).toMatch(/stay true before it fires/);
    expect(text(f("keep"))).toBe("Keep firingnone");
    expect(text(f("threshold"))).toBe("Threshold> 5");
    expect(text(f("minreq"))).toBe("Min req20");
    expect(text(f("routes"))).toBe("Routes toConsole · On-call email");
    expect(f("sev").querySelector("[data-sev='sev1']")).not.toBeNull();
  });

  it("a sample rule says so, and Remove samples asks once more before deleting them", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
      rules: (id) => rulesWire(id, { has_samples: true, rules: [ruleWire("error_rate"),
        ruleWire("latency_p95", { kind: "custom", alert: "SampleP95Latency", label: "SampleP95Latency", sample: true, sev: "sev3" })] }),
      writes: { "/samples/remove": { body: { removed: 1, word: "Removed 1 sample rule" } } },
    });
    expect($("[data-testid='obs-rule-SampleP95Latency'] [data-testid='obs-rule-sample']")).not.toBeNull();
    expect($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-sample']")).toBeNull();
    const btn = () => $("[data-testid='obs-rules-remove-samples']");
    await click(btn());
    expect(text(btn())).toBe("Confirm remove");
    expect(rec.writes).toHaveLength(0);
    await click(btn());
    await settle();
    expect(rec.writes[0]).toMatchObject({ url: "/v1/apps/t_down/alert-rules/samples/remove", body: { project_id: "p1", actor: "ceo" } });
  });
});

describe("the silence sheet", () => {
  it("needs a reason, then silences for the chosen time", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts");
    await click($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-silence']"));
    const sheet = $("[data-testid='obs-silence-sheet']")!;
    expect(sheet.getAttribute("aria-label")).toBe("Silence Error rate");
    const save = () => sheet.querySelector<HTMLButtonElement>("[data-testid='obs-silence-save']")!;
    expect(save().disabled).toBe(true);
    const sel = sheet.querySelector<HTMLSelectElement>("[data-testid='obs-silence-for']")!;
    sel.value = "240";
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    await type(sheet.querySelector("[data-testid='obs-silence-reason']"), "load test until 9");
    expect(save().disabled).toBe(false);
    await click(save());
    await settle();
    const w = rec.writes.find((x) => x.url.endsWith("/silence"));
    expect(w?.url).toBe("/v1/apps/t_down/alert-rules/arule_error_rate/silence");
    expect(w?.body).toEqual({ project_id: "p1", actor: "ceo", minutes: 240, reason: "load test until 9" });
  });

  it("a silenced rule says so and offers End silence", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
      rules: (id) => rulesWire(id, { rules: [ruleWire("error_rate", { silence: { id: "asil_1", ends_at: "2026-10-04T13:00:00Z",
                                                                                   reason: "load test", by: "Param" } })] }),
    });
    expect(text($("[data-testid='obs-rule-error_rate'] [data-testid='obs-rule-state']"))).toBe("Silenced");
    await click($("[data-testid='obs-rule-unsilence']"));
    await settle();
    expect(rec.writes[0].url).toBe("/v1/apps/t_down/alert-silences/asil_1/end");
  });
});

describe("Dashboard ↗ in the drawer", () => {
  it("links only once Grafana answers; otherwise says why, with Check and Deploy stack", async () => {
    const rec = await openObs("?view=apps&app=t_down", {
      writes: { "/grafana/check": { body: { grafana: { state: "not_answering", reason: "Grafana isn't answering on its host" } } } },
    });
    const off = $("[data-testid='obs-app-dashboard']")!;
    expect(off.tagName).toBe("SPAN");
    expect(off.dataset.state).toBe("not_answering");
    expect($("[data-testid='obs-app-dashboard-why']")?.getAttribute("title")).toBe("Grafana isn't answering on its host (connection refused)");
    await click($("[data-testid='obs-app-dashboard-check']"));
    await settle();
    expect(rec.writes.find((w) => w.url === "/v1/observability/grafana/check")?.body).toEqual({ project_id: "p1", actor: "ceo" });
    await click($("[data-testid='obs-app-dashboard-deploy']"));
    expect(params().get("view")).toBe("sources");
    unmount();
    await openObs("?view=apps&app=t_live");
    expect($("[data-testid='obs-app-dashboard']")?.getAttribute("href")).toBe("http://grafana.local/d/invoice");
  });
});

describe("NOISE_ALERT", () => {
  it("is resolved with the agent's reason", () => {
    expect(parseAlert(NOISE_ALERT).triage.outcome).toBe("noise");
  });
});
