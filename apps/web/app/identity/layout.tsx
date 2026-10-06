import type { ReactNode } from "react";

import { RouteShell } from "../../components/identity/route-shell";

export const dynamic = "force-dynamic";

/** Identity: one area for people, groups, access roles, service accounts, agents, sign-in and audit. */
export default async function IdentityLayout({ children }: { children: ReactNode }) {
  return <RouteShell section="identity">{children}</RouteShell>;
}
