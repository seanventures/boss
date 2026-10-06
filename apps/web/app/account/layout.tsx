import type { ReactNode } from "react";

import { RouteShell } from "../../components/identity/route-shell";

export const dynamic = "force-dynamic";

/** My account: your sign-in and security, sessions, the agents acting for you, your API keys. */
export default async function AccountLayout({ children }: { children: ReactNode }) {
  return <RouteShell section="account">{children}</RouteShell>;
}
