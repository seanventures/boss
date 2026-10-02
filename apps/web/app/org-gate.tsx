"use client";

import { useActionState, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import type { OrgSummary } from "../lib/orgs";
import { onboardOrgFormAction, type OnboardOrgResult } from "./org-gate-actions";
import { callAction } from "../lib/call-action";
import { SubmitButton } from "./submit-button";

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
          <img src="/boss-mark.png" alt="BOSS" className="org-gate-logo" />
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

/**
 * A FULL PAGE LOAD INTO THE CONSOLE, NOT A CLIENT TRANSITION.
 *
 * `router.push` keeps the picker on screen until the whole console has rendered client-side —
 * the URL does not even change until then — so pressing Enter looked like nothing happening for
 * a long time, then the console appeared. Measured on a fresh install: a full load of
 * /?org=org_boss is ~1.5s with the console's loading screen from the first moment; the
 * transition from the picker had not finished after 55s in a background tab. A hard navigation
 * streams the page and shows loading.tsx immediately.
 */
function openOrg(orgId: string, replace = false) {
  const url = `/?org=${encodeURIComponent(orgId)}`;
  if (replace) window.location.replace(url);
  else window.location.assign(url);
}

export function OrgGate({ orgs }: { orgs: OrgSummary[] }) {
  const params = useSearchParams();
  const [showOnboard, setShowOnboard] = useState(orgs.length === 0);
  const [opening, setOpening] = useState<string | null>(null);
  const [onboarded, onboard] = useActionState<OnboardOrgResult | null, FormData>(
    callAction(onboardOrgFormAction), null,
  );

  // Returning users skip the picker (unless they explicitly chose to switch).
  useEffect(() => {
    if (params.get("pick") === "1") return;
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(ORG_KEY) : null;
    if (stored && orgs.some((o) => o.org_id === stored)) {
      setOpening(stored);
      openOrg(stored, true);
    }
  }, [orgs, params]);

  const enter = (orgId: string) => {
    try {
      window.localStorage.setItem(ORG_KEY, orgId);
    } catch {}
    setOpening(orgId);
    openOrg(orgId);
  };

  // DONE MEANS OPENED. A successful onboard left the form open and filled, which read as though
  // nothing had happened; the new organization is entered straight away instead.
  useEffect(() => {
    if (onboarded?.ok) enter(onboarded.orgId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onboarded]);

  return (
    <div className="org-gate">
      <div className="org-gate-inner">
        <div className="org-gate-brand">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/boss-mark.png" alt="BOSS" className="org-gate-logo" />
          <span className="org-gate-tag">Autonomous System</span>
        </div>
        <h1 className="org-gate-title">Select your organization</h1>
        <p className="org-gate-sub">
          Every organization has its own workspace and data. Shared capabilities (skills, tools, MCP
          servers) come pre-loaded; everything you create stays within the org you pick.
        </p>

        <div className="org-gate-grid">
          {orgs.map((o) => (
            <button key={o.org_id} type="button" className="org-card" onClick={() => enter(o.org_id)}
                    disabled={opening !== null} aria-busy={opening === o.org_id}>
              <span className="org-card-mark">{o.org_name.slice(0, 2).toUpperCase()}</span>
              <span className="org-card-body">
                <span className="org-card-name">{o.org_name}</span>
                <span className="org-card-meta">
                  {o.task_count} task{o.task_count === 1 ? "" : "s"} · {o.meeting_count} meeting{o.meeting_count === 1 ? "" : "s"}
                </span>
                <span className="org-card-proj mono">{o.project_slug}</span>
              </span>
              <span className="org-card-go">{opening === o.org_id ? "Opening…" : "Enter →"}</span>
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

        {onboarded?.ok ? (
          <p className="org-onboard-note" role="status" data-testid="org-onboard-done">
            Created {onboarded.name}. Opening its workspace…
          </p>
        ) : showOnboard ? (
          <form action={onboard} className="org-onboard">
            <label>
              Organization name
              <input name="name" placeholder="e.g. Northwind Robotics" required autoFocus={orgs.length > 0} />
            </label>
            <SubmitButton type="submit" className="cap-btn primary">Onboard organization</SubmitButton>
            {onboarded && !onboarded.ok ? (
              <p className="error-banner" role="alert" data-testid="org-onboard-error">{onboarded.error}</p>
            ) : null}
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
