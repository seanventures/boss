"use client";

/**
 * ACCESS DENIED, NEVER A BLANK PAGE (IDENTITY.md §6.5, §9.10): names the missing permission, who
 * can grant it, and offers "Request access", which asks the admins through a BOSS decision.
 * Rendered by a section's layout BEFORE the page fetches anything, so nothing 403s after load.
 */
import React, { useEffect, useState } from "react";
import { LockSimple } from "@phosphor-icons/react";

import { IdentityError, idGet, idPost } from "../../lib/identity/api";
import type { Permission } from "../../lib/identity/permissions.gen";
import { Perm } from "./kit";
import type { Grantor } from "../../lib/identity/types";
import { adaptGrantor } from "../../lib/identity/adapt-people";
import { showToast } from "../../app/client-forms";

export function AccessDenied({ permission, page, home = "/" }: { permission: Permission; page: string; home?: string }) {
  const [grantors, setGrantors] = useState<Grantor[] | null>(null);
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    idGet<{ grantors: unknown[] }>(`/v1/identity/grantors?permission=${encodeURIComponent(permission)}`)
      .then((r) => { if (live) setGrantors((r.grantors ?? []).map(adaptGrantor)); })
      .catch(() => { if (live) setGrantors([]); });
    return () => { live = false; };
  }, [permission]);

  const who = grantors === null ? "…" : grantors.length
    ? grantors.slice(0, 3).map((g) => g.name).join(", ")
    : "an Owner";
  const roles = new Set((grantors ?? []).map((g) => g.role).filter(Boolean));
  const role = roles.size === 1 ? `${[...roles][0]}s` : "Admins";

  return (
    <div className="idn-denied" data-testid="access-denied" data-permission={permission}>
      <div className="idn-denied-card">
        <div className="idn-lock" aria-hidden><LockSimple size={24} weight="duotone" /></div>
        <h1>You can't open {page}</h1>
        <div className="idn-why-line">
          <span>Needs</span> <Perm p={permission} />
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
                await idPost("/v1/identity/access-requests", { permission, reason: `From ${page}` });
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
