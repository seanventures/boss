// @vitest-environment happy-dom
/**
 * Monitoring Phase 1a (BOSSAPP-598) in the console.
 *
 * THE OPERATOR'S SCREENSHOT: Observability › Alerts showed build and workflow cards (the generic
 * Needs-you strip, and untagged cards leaking into Observability). The Alerts tab now shows only
 * alerts and incidents, and only cards the server tagged `observability` reach any Observability
 * tab. An open incident whose alerts have all cleared is shown with its recovered word, never as
 * failing. Plus: the new filters live in the URL, bulk ack, Connect an app, and Notify's preview.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ACKED_ALERT, FIRING_ALERT, NOISE_ALERT, SIGNAL_ALERT, allAlertsWire } from "./_helpers/alerts-fixtures";
import { ATTENTION_APP_DOWN, pulseWire } from "./_helpers/obs-fixtures";
import { $, $$, click, settle, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

async function type(el: Element | null, value: string) {
  const t = el as HTMLInputElement;
  const proto = t instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : t instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")?.set?.call(t, value);
    t.dispatchEvent(new Event(t instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
  await settle();
}

const BUILD_CARD = {
  id: "decision:d_build", kind: "decision", title: "Approve the HelpDesk AI build plan", detail: "Waiting on you",
  role_key: "cto", actions: [], answers: [], decide_inline: false, at: "2026-10-04T11:00:00.000Z",
};
const RECOVERED = {
  id: "inc_rec", title: "invoice (prod): p95 slow", status: "open", lifecycle: "recovered", since: "2026-10-04T10:00:00.000Z",
  firing: 0, happening: false, subject_id: "t_live", urgency: "high", acked: null, response_run_id: "wfr_9",
  response_status: "waiting", word: "Recovered; a response is waiting on you",
  recovered_at: new Date(Date.now() - 3600_000).toISOString(),
};

describe("Observability › Alerts shows only alerts and incidents", () => {
  it("draws no Needs-you cards on the Alerts tab, and no untagged card on any tab", async () => {
    const pulse = pulseWire({ attention: [ATTENTION_APP_DOWN, BUILD_CARD] });
    await openObs("?view=alerts", { pulse });
    expect($("[data-testid='obs-needs']")).toBeNull();
    expect(text($("[data-testid='obs']"))).not.toContain("Approve the HelpDesk AI build plan");
    unmount();
    await openObs("?view=apps", { pulse });
    const needs = text($("[data-testid='obs-needs']"));
    expect(needs).toContain("api is down");
    expect(needs).not.toContain("Approve the HelpDesk AI build plan");
  });

  it("an open incident with nothing firing shows its recovered word, never as failing", async () => {
    await openObs("?view=alerts", { alerts: () => allAlertsWire([], { counts: { firing: 0, pending: 0 }, incidents: [RECOVERED] }) });
    const row = $("[data-testid='obs-incident-inc_rec']")!;
    // the recovery time is drawn in the viewer's clock (it was baked into the words in UTC)
    const hhmm = new Date(Date.parse(RECOVERED.recovered_at)).toTimeString().slice(0, 5);
    expect(text(row.querySelector("[data-testid='obs-incident-word']"))).toBe(`Recovered at ${hhmm}; a response is waiting on you`);
    expect(row.querySelector(".obs-adot")?.getAttribute("data-tone")).toBe("grey");
    expect(text(row)).not.toMatch(/fail/i);
    expect(text($("[data-testid='obs-alerts-empty']"))).toBe("Nothing firing");
  });

  it("source, flapping and acked filters live in the URL and reach the API", async () => {
    const rec = await openObs("?view=alerts");
    await type($("[data-testid='obs-alerts-source']"), "external");
    await type($("[data-testid='obs-alerts-flapping']"), "true");
    await type($("[data-testid='obs-alerts-acked']"), "false");
    expect(params().get("asrc")).toBe("external");
    expect(params().get("aflap")).toBe("true");
    expect(params().get("aack")).toBe("false");
    await settle();
    expect(rec.calls.some((u) => u.includes("source=external&flapping=true&acked=false"))).toBe(true);
  });

  it("badges a row and acks several at once, saying what happened", async () => {
    const flappy = { ...SIGNAL_ALERT, source: "Alertmanager", flapping: true, urgency: "low",
                     links: [{ href: "https://alerts.example.com/x", text: "Alertmanager" }, { href: "javascript:alert(1)", text: "bad" }] };
    const rec = await openObs("?view=alerts", {
      alerts: () => allAlertsWire([FIRING_ALERT, flappy]),
      writes: { "/alerts/ack": { body: { acked: ["alrt_err", "alrt_sig"], until: "2026-10-04T16:00:00.000Z" } } },
    });
    const sig = $("[data-testid='obs-alert-alrt_sig']")!;
    expect(text(sig.querySelector("[data-testid='obs-alert-source']"))).toBe("Alertmanager");
    expect(sig.querySelector("[data-testid='obs-alert-flapping']")).not.toBeNull();
    expect(sig.querySelector("[data-testid='obs-alert-low']")).not.toBeNull();
    expect(sig.querySelectorAll("[data-testid='obs-alert-link']")).toHaveLength(1);
    for (const box of $$("[data-testid='obs-alert-select']")) await click(box);
    expect(text($("[data-testid='obs-alerts-bulk']"))).toContain("2 selected");
    await click($("[data-testid='obs-alerts-ack-selected']"));
    const ack = rec.writes.find((w) => w.url === "/v1/alerts/ack")!;
    expect((ack.body as { alert_ids: string[] }).alert_ids.sort()).toEqual(["alrt_err", "alrt_sig"]);
    expect((ack.body as { actor: string }).actor).toBe("ceo");
  });
});

describe("SEV1–SEV4 and status at a glance", () => {
  it("leads with Needs attention, which is also the filter", async () => {
    const rec = await openObs("?view=alerts", { alerts: () => allAlertsWire([FIRING_ALERT, ACKED_ALERT], { counts: { firing: 2, pending: 0, needs_attention: 1 } }) });
    const lead = $("[data-testid='obs-alerts-attention']")!;
    const zone = $("[data-testid='obs-alerts']")!;
    expect(zone.firstElementChild?.contains(lead)).toBe(true);
    expect(text($("[data-testid='obs-alerts-attention-n']"))).toBe("1");
    expect(lead.getAttribute("title")).toMatch(/nobody has acknowledged/);
    expect(lead.getAttribute("aria-pressed")).toBe("false");
    await click(lead);
    await settle();
    expect(params().get("aatt")).toBe("1");
    expect($("[data-testid='obs-alerts-attention']")?.getAttribute("aria-pressed")).toBe("true");
    expect(rec.calls.some((u) => u.startsWith("/v1/alerts/all?") && u.includes("attention=true"))).toBe(true);
    await click($("[data-testid='obs-alerts-attention']"));
    expect(params().get("aatt")).toBeNull();
  });

  it("every row carries its SEV flag (with what it means) and a status chip saying whether it is open", async () => {
    await openObs("?view=alerts", { alerts: () => allAlertsWire([FIRING_ALERT, ACKED_ALERT, NOISE_ALERT, SIGNAL_ALERT]) });
    const row = (id: string) => $(`[data-testid='obs-alert-${id}']`)!;
    const sev = (id: string) => row(id).querySelector("[data-testid='obs-sev']")!;
    const status = (id: string) => row(id).querySelector("[data-testid='obs-status']")!;
    expect(sev("alrt_err").getAttribute("data-sev")).toBe("sev1");
    expect(sev("alrt_err").getAttribute("title")).toBe("SEV1 Critical: Customer-facing outage. Pages: high urgency, notifies at once, ignores quiet hours");
    expect(sev("alrt_ack").getAttribute("data-sev")).toBe("sev2");
    // An older answer (no sev): the warning label reads as SEV3.
    expect(sev("alrt_sig").getAttribute("data-sev")).toBe("sev3");
    expect(text(status("alrt_err"))).toBe("Firing");
    expect(status("alrt_err").getAttribute("data-tone")).toBe("red");
    expect(row("alrt_err").getAttribute("data-attention")).toBe("1");
    expect(text(row("alrt_err").querySelector("[data-testid='obs-alert-duration']"))).toBe("open 12 min");
    expect(text(status("alrt_ack"))).toBe("Acknowledged · Param");
    expect(status("alrt_ack").getAttribute("data-tone")).toBe("amber");
    expect(status("alrt_ack").getAttribute("title")).toMatch(/Acknowledged by Param/);
    expect(row("alrt_ack").getAttribute("data-attention")).toBeNull();
    expect(text(status("alrt_lat"))).toMatch(/^Resolved .+ · 6 min$/);
    expect(status("alrt_lat").getAttribute("data-tone")).toBe("green");
    expect(row("alrt_lat").getAttribute("data-open")).toBe("0");
    expect(row("alrt_lat").querySelector("[data-testid='obs-alert-fired']")).not.toBeNull();
    expect(row("alrt_lat").querySelector("[data-testid='obs-alert-duration']")).toBeNull();
    // the severity filter offers SEV1–SEV4
    const opts = [...document.querySelectorAll<HTMLOptionElement>("[data-testid='obs-alerts-severity'] option")].map((o) => o.value);
    expect(opts).toEqual(["", "sev1", "sev2", "sev3", "sev4"]);
  });

  it("an incident card shows its SEV and status chip, saying who acknowledged it", async () => {
    const inc = { ...RECOVERED, id: "inc_ack", firing: 1, happening: true, sev: "sev2", acked: true, acked_by: "Param",
                  status_chip: { key: "acknowledged", word: "Acknowledged · Param", tone: "amber", tip: "Acknowledged: someone is on it" } };
    await openObs("?view=alerts", { alerts: () => allAlertsWire([], { incidents: [inc] }) });
    const row = $("[data-testid='obs-incident-inc_ack']")!;
    expect(row.querySelector("[data-testid='obs-sev']")?.getAttribute("data-sev")).toBe("sev2");
    expect(text(row.querySelector("[data-testid='obs-incident-status']"))).toBe("Acknowledged · Param");
  });
});

describe("Connect an app", () => {
  it("asks how it talks, shows the values once, then what arrived", async () => {
    const rec = await openObs("?view=alerts", {
      writes: { "/monitoring/connect": { status: 201, body: {
        path: "otel", service: { id: "svc_1", name: "checkout-api", environment: "prod", onboarding: "waiting" },
        source: { id: "src_1", kind: "otlp", token_hint: "x9" }, shown_once: true, word: "checkout-api is ready for OpenTelemetry",
        paste: { values: [{ name: "OTEL_EXPORTER_OTLP_ENDPOINT", value: "https://boss.example.com/v1/ingest/otlp" },
                          { name: "OTEL_EXPORTER_OTLP_HEADERS", value: "Authorization=Bearer tok" }], block: null },
      } } },
      gets: { "/v1/monitoring/services/svc_1/live": () => ({
        service_id: "svc_1", arrived: true, series: 12, events: 0, rules: [1, 2, 3], hint: null,
        word: "12 series from checkout-api; HTTP server metrics recognised (OpenTelemetry semconv); 3 rules proposed",
      }) },
    });
    await click($("[data-testid='obs-connect']"));
    expect($("[data-testid='obs-connect-sheet']")).not.toBeNull();
    // A URL is not asked for OpenTelemetry; the name is, and Connect waits for it.
    await click($("[data-testid='obs-connect-otel']"));
    expect($("[data-testid='obs-connect-target']")).toBeNull();
    expect(($("[data-testid='obs-connect-go']") as HTMLButtonElement).disabled).toBe(true);
    await type($("[data-testid='obs-connect-name']"), "checkout-api");
    await click($("[data-testid='obs-connect-go']"));
    const sent = rec.writes.find((w) => w.url === "/v1/monitoring/connect")!.body as Record<string, unknown>;
    expect(sent).toMatchObject({ project_id: "p1", actor: "ceo", path: "otel", name: "checkout-api", environment: "prod" });
    expect($("[data-testid='obs-connect-once']")).not.toBeNull();
    expect($$("[data-testid='obs-connect-value']")).toHaveLength(2);
    await settle();
    expect(text($("[data-testid='obs-connect-arrived']"))).toContain("12 series from checkout-api");
  });

  it("a URL check asks for the URL; a refusal is said in the API's words", async () => {
    await openObs("?view=alerts", { writes: { "/monitoring/connect": { status: 422, body: { detail: "That URL is not reachable from BOSS." } } } });
    await click($("[data-testid='obs-connect']"));
    await click($("[data-testid='obs-connect-url']"));
    const url = $("[data-testid='obs-connect-target']");
    expect(url).not.toBeNull();
    await type($("[data-testid='obs-connect-name']"), "checkout");
    await type(url, "https://checkout.example.com/health");
    await click($("[data-testid='obs-connect-go']"));
    expect(text($("[data-testid='obs-connect-error']"))).toBe("That URL is not reachable from BOSS.");
  });
});

describe("Notify", () => {
  it("is a tab: contact points say verified or not, and the preview says where it goes and what is skipped", async () => {
    await openObs("?view=notify", {
      gets: {
        "/v1/contact-points": () => ({ contact_points: [
          { id: "cp_console", name: "Console", kind: "console", shows: "", verified: true },
          { id: "cp_mail", name: "On-call", kind: "email", shows: "on…@example.com", verified: false },
        ] }),
        "/v1/notification-policies": () => ({ policies: [
          { id: "pol_root", parent_id: null, position: 0, name: "Default", matchers: [], contact_point_ids: ["cp_console"], group_wait_s: 30 },
          { id: "pol_co", parent_id: "pol_root", position: 0, name: "Checkout", matchers: [{ name: "service", op: "=", value: "checkout" }],
            contact_point_ids: ["cp_mail"], quiet_hours: { start: "22:00", end: "07:00", timezone: "UTC" } },
        ] }),
        "/v1/silences": () => ({ silences: [], windows: [], inhibit_rules: [] }),
      },
      writes: { "/notification-policies/preview": { body: {
        urgency: "high", word: "Goes to Checkout after 30 s",
        routes: [{ policy_id: "pol_co", path: ["Default", "Checkout"], group_wait_s: 30, repeat_interval_s: 14400, quiet_until: null,
                   contact_points: [{ id: "cp_mail", name: "On-call", kind: "email", skipped: "not verified" }] }],
      } } },
    });
    expect($("[data-testid='obs-tab-notify']")?.getAttribute("title")).toBe("Who is told, how, and when");
    expect($("[data-testid='obs-cp-cp_mail'] [data-testid='obs-cp-unverified']")?.getAttribute("title")).toMatch(/high-urgency/);
    expect(text($("[data-testid='obs-policy-pol_co']"))).toContain('service="checkout"');
    expect(text($("[data-testid='obs-policy-pol_co']"))).toContain("quiet 22:00–07:00");
    await click($("[data-testid='obs-route-go']"));
    expect(text($("[data-testid='obs-route-word']"))).toBe("Goes to Checkout after 30 s");
    expect(text($("[data-testid='obs-route-cp']"))).toBe("On-call: skipped, not verified");
  });

  it("recent sends carry the SEV of what they told", async () => {
    await openObs("?view=notify", {
      gets: {
        "/v1/contact-points": () => ({ contact_points: [{ id: "cp_console", name: "Console", kind: "console", shows: "", verified: true }] }),
        "/v1/notification-policies": () => ({ policies: [] }),
        "/v1/silences": () => ({ silences: [], windows: [], inhibit_rules: [] }),
        "/v1/notifications": () => ({ notifications: [
          { id: "n1", kind: "firing", status: "sent", contact_point: "Console", subject: "[SEV1 FIRING] api error rate", attempts: 1,
            sent_at: "2026-10-04T11:50:00.000Z", sev: "sev1" },
          { id: "n2", kind: "test", status: "sent", contact_point: "Console", subject: "Test", attempts: 1, sent_at: "2026-10-04T11:40:00.000Z", sev: null },
        ] }),
      },
    });
    await click($("[data-testid='obs-sends-toggle']"));
    await settle();
    const rows = $$("[data-testid='obs-send']");
    expect(rows[0].querySelector("[data-testid='obs-sev']")?.getAttribute("data-sev")).toBe("sev1");
    expect(rows[1].querySelector("[data-testid='obs-sev']")).toBeNull();
  });
});
