import { pageAllowed } from "../../../components/identity/route-shell";
import { serverGet } from "../../../lib/identity/server";
import type { AccessRole, SecurityPolicy, ServiceAccount } from "../../../lib/identity/types";
import { ServiceAccounts } from "./service-accounts";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ sa?: string }> };

/** Identity › Service accounts (mockup f): accounts, their keys, and keys shown exactly once. */
export default async function ServiceAccountsPage({ searchParams }: Props) {
  if (!(await pageAllowed("/identity/service-accounts"))) return null;
  const { sa } = await searchParams;
  const [list, roles, policy] = await Promise.all([
    serverGet<{ service_accounts: ServiceAccount[] }>("/v1/identity/service-accounts"),
    serverGet<{ roles: AccessRole[] }>("/v1/identity/access-roles"),
    serverGet<SecurityPolicy>("/v1/identity/policy"),
  ]);
  return (
    <ServiceAccounts
      initial={list.ok ? list.data.service_accounts : []}
      roles={roles.ok ? roles.data.roles : []}
      noExpiryAllowed={policy.ok ? policy.data.key_no_expiry_for_service_accounts : false}
      maxDays={policy.ok ? policy.data.key_max_days : 365}
      selected={sa ?? null}
    />
  );
}
