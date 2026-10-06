"use client";

/**
 * ACCESS DENIED, NEVER A BLANK PAGE (IDENTITY.md §6.5, §9.10): names the missing permission, who
 * can grant it, and offers "Request access", which asks the admins through a BOSS decision.
 * Rendered by a section's layout BEFORE the page fetches anything, so nothing 403s after load.
 */
import React, { useEffect, useState } from "react";
import { LockSimple } from "@phosphor-icons/react";

import { IdentityError, idGet, idPost } from "../../lib/identity/api";
import { PERMISSION_META, type Permission } from "../../lib/identity/permissions.gen";
import type { Grantor } from "../../lib/identity/types";
import { showToast } from "../../app/client-forms";

export function AccessDenied({ permission, page, home = "/" }: { permission: Permission; page: string; home?: string }) {
  const [grantors, setGrantors] = useState<Grantor[] | null>(null);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    idGet<{ grantors: Grantor[] }>(`/v1/identity/grantors?permission=${encodeURIComponent(permission)}`)
      .then((r) => { if (live) setGrantors(r.grantors); })
      .catch(() => { if (live) setGrantors([]); });
    return () => { live = false; };
  }, [permission]);

  const who = grantors === null ? "…" : grantors.length
    ? grantors.slice(0, 3).map((g) => g.name).join(", ")
    : "an Owner";
  const role = grantors?.[0]?.role ? `${grantors[0].role}s` : "Admins";

  return (
    <div className="idn-denied" data-testid="access-denied" data-permission={permission}>
      <div className="idn-denied-card">
        <div className="idn-lock" aria-hidden><LockSimple size={24} weight="duotone" /></div>
        <h1>You can't open {page}</h1>
        <div className="idn-why-line">
          <span>Needs</span> <code className="idn-perm" title={PERMISSION_META[permission].title}>{permission}</code>
        </div>
        <div className="idn-why-line idn-dim" data-testid="access-denied-grantors">
          {grantors && grantors.length ? `${role}: ${who}` : `Ask ${who}`}
        </div>
        <div className="idn-acts">
          <button type="button" className="idn-btn primary" disabled={sent || busy} data-testid="request-access"
            title="Asks the admins through an approval"
            onClick={async () => {
              setBusy(true);
              try {
                await idPost("/v1/identity/access-requests", { permission, page });
                setSent(true);
                showToast("success", "Request sent");
              } catch (e) {
                showToast("error", e instanceof IdentityError ? e.message : "Not sent");
              } finally {
                setBusy(false);
              }
            }}>
            {sent ? "Requested" : "Request access"}
          </button>
          <a className="idn-btn" href={home}>Go back</a>
        </div>
      </div>
    </div>
  );
}
