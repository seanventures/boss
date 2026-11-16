// @vitest-environment happy-dom
/**
 * PORTABILITY ON THE CONSOLE (BOSSAPP-836). A person exports a project (secrets stay empty
 * placeholders), imports it (dry run, then apply), reads backup health, and rolls an upgrade
 * back. The API scrubs secrets; the console refuses to show a value that still looks like one.
 * Rollback is `boss rollback` on the host: the API has no Docker socket.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
  usePathname: () => "/",
}));

import { PortabilityCard } from "../app/projects/portability-card";
import { backupHealth, rollbackCommand, secretLeaks, secretPlaceholders } from "../lib/portability-console";
import { click, mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SECRET = "sk-portability-NEVER-LEAVES-9f3c7d21";
const PROJECT = "proj_src";
const ACTOR = "ceo";

const CLEAN = {
  format: "boss.portability-bundle",
  schema_version: 1,
  projects: [{
    ref: { slug: "help", name: "Help" },
    sections: {
      mcp_servers: [{
        name: "stripe",
        auth: { secret: { $secret_placeholder: { kind: "api_token", name: "stripe", scope_type: "project", value: null } } },
      }],
    },
  }],
  secrets: [{ kind: "api_token", name: "stripe", scope_type: "project", value: null, placeholder: true }],
};

const DIRTY = {
  format: "boss.portability-bundle",
  projects: [{ sections: { mcp_servers: [{ auth: { api_key: SECRET } }] } }],
  secrets: [],
};

const VERIFIED = {
  backups: [{
    id: "bkp_ok", kind: "scheduled", status: "verified", error: null,
    verified_at: "2026-11-14T01:00:00+00:00", started_at: "2026-11-14T01:00:00+00:00",
  }],
};

afterEach(() => { unmount(); });

function answerFor(bundle: unknown, backups: unknown = VERIFIED) {
  return (c: Call) => {
    if (c.method === "GET" && c.path === `/v1/projects/${PROJECT}/export`) return { body: bundle };
    if (c.method === "GET" && c.path === "/v1/instance/backups") return { body: backups };
    if (c.method === "POST" && c.path === `/v1/projects/${PROJECT}/import` && (c.body as { mode?: string })?.mode === "dry_run") {
      return { body: { import_id: "pim_1", status: "planned", summary: { model_settings: { update: 1 } }, errors: [] } };
    }
    if (c.method === "POST" && c.path === `/v1/projects/${PROJECT}/import` && (c.body as { mode?: string })?.mode === "apply") {
      return { body: { import_id: "pim_1", status: "applied" } };
    }
    return undefined;
  };
}

async function open(bundle: unknown = CLEAN, backups: unknown = VERIFIED) {
  const calls = stubFetch(answerFor(bundle, backups));
  await mountAs(<PortabilityCard projectId={PROJECT} actor={ACTOR} />);
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return calls;
}

describe("portability on the console", () => {
  it("reads placeholders and a leak, backup health, and the host rollback command", () => {
    expect(secretPlaceholders(CLEAN).map((p) => p.name)).toEqual(["stripe", "stripe"]);
    expect(secretLeaks(CLEAN)).toEqual([]);
    expect(secretLeaks(DIRTY)).toEqual(["projects[0].sections.mcp_servers[0].auth.api_key"]);
    expect(JSON.stringify(CLEAN)).not.toContain(SECRET);
    expect(backupHealth([])).toEqual({ state: "none", sentence: "No backup yet." });
    expect(backupHealth(VERIFIED.backups).state).toBe("ok");
    expect(backupHealth(VERIFIED.backups).sentence).toContain("bkp_ok");
    expect(backupHealth([{ id: "bkp_bad", status: "failed", error: "pg_dump failed", verified_at: null }])).toMatchObject({ state: "failed" });
    expect(rollbackCommand()).toBe("boss rollback");
    expect(rollbackCommand("/var/lib/boss/backups/pre")).toBe("boss rollback --restore-backup /var/lib/boss/backups/pre");
    expect(rollbackCommand("  ")).toBe("boss rollback");
    expect(rollbackCommand("/tmp/x; rm -rf /")).toBeNull();
  });

  it("exports a project and shows secrets as empty placeholders, never the value", async () => {
    const calls = await open();
    expect(tid("pc-backup-health")!.textContent).toContain("bkp_ok");
    await click(tid("pc-export"));
    const exp = calls.find((c) => c.path.endsWith("/export"));
    expect(exp?.method).toBe("GET");
    expect(exp?.search).toContain(`actor=${ACTOR}`);
    expect(tid("pc-secrets")!.textContent).toContain("stripe");
    expect(tid("pc-secrets")!.textContent).toContain("empty");
    expect(document.body.textContent).not.toContain(SECRET);
    expect(tid("pc-download")).not.toBeNull();
  });

  it("refuses an export that still carries a secret value", async () => {
    await open(DIRTY);
    await click(tid("pc-export"));
    expect(tid("pc-export-refused")!.textContent).toMatch(/secret/i);
    expect(document.body.textContent).not.toContain(SECRET);
    expect(tid("pc-download")).toBeNull();
  });

  it("imports by a dry run, then apply", async () => {
    const calls = await open();
    const box = tid("pc-import-bundle") as HTMLTextAreaElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      set.call(box, JSON.stringify(CLEAN));
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(tid("pc-import-check"));
    const dry = calls.filter((c) => c.path.endsWith("/import") && (c.body as { mode?: string }).mode === "dry_run");
    expect(dry).toHaveLength(1);
    expect((dry[0].body as { bundle: { format: string } }).bundle.format).toBe("boss.portability-bundle");
    expect(tid("pc-import-plan")!.textContent).toMatch(/1 update/);
    await click(tid("pc-import-apply"));
    const applied = calls.filter((c) => c.path.endsWith("/import") && (c.body as { mode?: string }).mode === "apply");
    expect(applied).toHaveLength(1);
    expect(applied[0].body).toMatchObject({ import_id: "pim_1", mode: "apply", actor: ACTOR });
    expect(tid("pc-import-done")!.textContent).toMatch(/Imported/);
  });

  it("rolls an upgrade back as boss rollback, and does not ask the API to run Docker", async () => {
    const calls = await open();
    const before = calls.length;
    await click(tid("pc-rollback"));
    expect(tid("px-confirm")).not.toBeNull();
    await click(tid("px-confirm-go"));
    expect(tid("pc-rollback-command")!.textContent).toBe("boss rollback");
    expect(calls.slice(before).some((c) => /rollback|upgrade/.test(c.path))).toBe(false);

    const path = tid("pc-restore-path") as HTMLInputElement;
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(path, "/var/lib/boss/backups/pre");
      path.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(tid("pc-rollback"));
    await click(tid("px-confirm-go"));
    expect(tid("pc-rollback-command")!.textContent).toBe("boss rollback --restore-backup /var/lib/boss/backups/pre");
  });
});
