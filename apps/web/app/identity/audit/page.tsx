import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { AuditPage } from "../../../lib/identity/types";
import { AuditScreen } from "./audit-screen";
import { queryFrom } from "./audit-query";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

/** Identity › Audit (mockup g): every security event, hash-chained, with the chain that allowed it. */
export default async function AuditLogPage({ searchParams }: Props) {
  if (!(await pageAllowed("/identity/audit"))) return null;
  const raw = await searchParams;
  const one = (k: string) => (typeof raw[k] === "string" ? (raw[k] as string) : "");
  const filters = { q: one("q"), outcome: one("outcome"), actor: one("actor"), grant: one("grant") };
  const read = await serverGet<AuditPage>(`/v1/identity/audit?${queryFrom(filters)}`);
  return (
    <AuditScreen
      initial={read.ok ? read.data : null}
      initialFilters={filters}
      initialEvent={one("event") || null}
      verifyOnOpen={one("verify") === "1"}
    />
  );
}
