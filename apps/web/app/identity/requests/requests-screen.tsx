"use client";

import React from "react";
import { HandWaving } from "@phosphor-icons/react";

import type { ReadFailure } from "../../../components/identity/states";
import { EmptyState } from "../../../components/identity/states";
import type { AccessRole } from "../../../lib/identity/types";
import type { AccessRequestRow } from "../../../lib/identity/types-people";
import { AccessRequests } from "../people/access-requests";

export type QueueView = { project: string | null; requests: AccessRequestRow[] | null; failure: ReadFailure | null };

/** The queues, one card each (the org's, or one per project the viewer assigns roles in). */
export function RequestsScreen({ queues, roles }: { queues: QueueView[]; roles: AccessRole[] }) {
  const waiting = queues.reduce((n, q) => n + (q.requests ?? []).filter((r) => r.status === "open").length, 0);
  const failed = queues.some((q) => q.failure);
  return (
    <>
      <div className="idn-sec-h">
        <h1 className="idn-h1">Access requests</h1>
        <span className="idn-sub" title={queues.some((q) => q.project) ? "The projects where you assign roles" : "Every request in the org"}>
          {waiting} waiting
        </span>
      </div>
      {queues.map((q) => (
        <AccessRequests key={q.project ?? "org"} requests={q.requests} roles={roles} failure={q.failure} project={q.project} />
      ))}
      {!waiting && !failed ? (
        <EmptyState icon={<HandWaving size={16} />} title="Nothing waiting" testId="requests-empty">
          When someone asks for access you can grant, it appears here.
        </EmptyState>
      ) : null}
    </>
  );
}
