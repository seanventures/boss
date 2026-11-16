import { RouteShell } from "../../../components/identity/route-shell";

import { DeviceApprove } from "./device-approve";

export const dynamic = "force-dynamic";

/** Where a terminal sends the signed-in person to approve its device code. */
export default async function DeviceCodePage({ searchParams }: {
  searchParams: Promise<{ user_code?: string | string[] }>;
}) {
  const q = await searchParams;
  const raw = Array.isArray(q.user_code) ? q.user_code[0] : q.user_code;
  return (
    <RouteShell section="account">
      <DeviceApprove initialCode={typeof raw === "string" ? raw : ""} />
    </RouteShell>
  );
}
