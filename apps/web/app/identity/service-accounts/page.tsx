import { pageAllowed } from "../../../components/identity/route-shell";
import { ErrorState } from "../../../components/identity/states";
import { loadDirectory, loadServiceAccounts } from "../../../lib/identity/directory";
import { serverGet } from "../../../lib/identity/server";
import type { SecurityPolicy } from "../../../lib/identity/types";
import { ServiceAccounts } from "./service-accounts";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ sa?: string }> };

/** Identity › Service accounts (mockup f): accounts, their keys, and keys shown exactly once. */
export default async function ServiceAccountsPage({ searchParams }: Props) {
  if (!(await pageAllowed("/identity/service-accounts"))) return null;
  const { sa } = await searchParams;
  const [dir, policy, listed] = await Promise.all([
    loadDirectory({ groupMembers: false }),
    serverGet<SecurityPolicy>("/v1/identity/policy"),
    serverGet<unknown>("/v1/identity/service-accounts"),
  ]);
  // A failed read is said, never drawn as "no service accounts".
  if (!listed.ok) return <ErrorState what="service accounts" failure={listed} />;
  const list = await loadServiceAccounts(dir);
  return (
    <ServiceAccounts
      initial={(list ?? []).filter((x) => x.status !== "revoked")}
      roles={dir.roles}
      people={dir.people.filter((p) => p.status === "active").map((p) => ({ id: p.id, name: p.display_name, hue: p.hue, email: p.email }))}
      noExpiryAllowed={policy.ok ? policy.data.key_no_expiry_for_service_accounts : false}
      maxDays={policy.ok ? policy.data.key_max_days : 365}
      selected={sa ?? null}
    />
  );
}
