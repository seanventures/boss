"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { OrgSummary } from "../lib/orgs";
import { onboardOrgAction } from "./actions";

const ORG_KEY = "boss.org";

/** Shown instead of the org picker when the URL names an org but the control plane
 * didn't answer. Falling back to the picker on a transient API blip silently threw
 * the operator out of their session (the "console resets" bug) — this keeps the URL
 * (org/area/deep links intact) and retries until the API answers again. */
export function OrgReconnect({ orgId, reason }: { orgId: string; reason?: string }) {
  const router = useRouter();
  useEffect(() => {
    const id = setInterval(() => router.refresh(), 5000);
    return () => clearInterval(id);
  }, [router]);
  return (
    <div className="org-gate">
      <div className="org-gate-inner">
        <div className="org-gate-brand">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/BOSS-logo.png" alt="BOSS" className="org-gate-logo" />
          <span className="org-gate-tag">Autonomous System</span>
        </div>
        <h1 className="org-gate-title">Reconnecting to the control plane…</h1>
        <p className="org-gate-sub">
          The API didn&apos;t answer just now{reason ? ` (${reason})` : ""}. Your session for{" "}
          <span className="mono">{orgId}</span> is preserved — this page retries automatically
          and will drop you back exactly where you were.
        </p>
      </div>
    </div>
  );
}

export function OrgGate({ orgs }: { orgs: OrgSummary[] }) {
  const router = useRouter();
  const params = useSearchParams();
  const [showOnboard, setShowOnboard] = useState(orgs.length === 0);

  // Returning users skip the picker (unless they explicitly chose to switch).
  useEffect(() => {
    if (params.get("pick") === "1") return;
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(ORG_KEY) : null;
    if (stored && orgs.some((o) => o.org_id === stored)) {
      router.replace(`/?org=${encodeURIComponent(stored)}`);
    }
  }, [orgs, params, router]);

  const enter = (orgId: string) => {
    try {
      window.localStorage.setItem(ORG_KEY, orgId);
    } catch {}
    router.push(`/?org=${encodeURIComponent(orgId)}`);
  };

  return (
    <div className="org-gate">
      <div className="org-gate-inner">
        <div className="org-gate-brand">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/BOSS-logo.png" alt="BOSS" className="org-gate-logo" />
          <span className="org-gate-tag">Autonomous System</span>
        </div>
        <h1 className="org-gate-title">Select your organization</h1>
        <p className="org-gate-sub">
          Every organization has its own workspace and data. Shared capabilities (skills, tools, MCP
          servers) come pre-loaded; everything you create stays within the org you pick.
        </p>

        <div className="org-gate-grid">
          {orgs.map((o) => (
            <button key={o.org_id} type="button" className="org-card" onClick={() => enter(o.org_id)}>
              <span className="org-card-mark">{o.org_name.slice(0, 2).toUpperCase()}</span>
              <span className="org-card-body">
                <span className="org-card-name">{o.org_name}</span>
                <span className="org-card-meta">
                  {o.task_count} task{o.task_count === 1 ? "" : "s"} · {o.meeting_count} meeting{o.meeting_count === 1 ? "" : "s"}
                </span>
                <span className="org-card-proj mono">{o.project_slug}</span>
              </span>
              <span className="org-card-go">Enter →</span>
            </button>
          ))}
          <button type="button" className="org-card org-card-add" onClick={() => setShowOnboard((v) => !v)}>
            <span className="org-card-mark add">＋</span>
            <span className="org-card-body">
              <span className="org-card-name">Onboard a new organization</span>
              <span className="org-card-meta">Admin · creates a fresh workspace with the common setup</span>
            </span>
          </button>
        </div>

        {showOnboard ? (
          <form action={onboardOrgAction} className="org-onboard">
            <label>
              Organization name
              <input name="name" placeholder="e.g. Northwind Robotics" required autoFocus={orgs.length > 0} />
            </label>
            <button type="submit" className="cap-btn primary">Onboard organization</button>
            <span className="org-onboard-note">
              Creates the org → workspace → project and seeds the common defaults (budget policy, agent
              modules, packs, MCP servers). It then appears above to enter.
            </span>
          </form>
        ) : null}
      </div>
    </div>
  );
}
