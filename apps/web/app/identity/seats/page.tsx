import { pageAllowed } from "../../../components/identity/route-shell";
import { loadDirectory } from "../../../lib/identity/directory";
import type { SeatsResponse } from "../../../lib/identity/seats";
import { serverGet } from "../../../lib/identity/server";
import { SeatsScreen } from "./seats-screen";

export const dynamic = "force-dynamic";

/** Identity › Approval seats (IDENTITY.md §6.13): who approves each risk tier's decisions. */
export default async function SeatsPage() {
  if (!(await pageAllowed("/identity/seats"))) return null;
  const [seats, dir] = await Promise.all([serverGet<SeatsResponse>("/v1/identity/seats"), loadDirectory({ groupMembers: false })]);
  const people = dir.people.filter((p) => p.status === "active" && !p.network_bound)
    .map((p) => ({ id: p.id, name: p.display_name || p.email || p.id }));
  return (
    <SeatsScreen data={seats.ok ? seats.data : null} people={people}
      failure={seats.ok ? null : { status: seats.status, code: seats.code, permission: seats.permission }} />
  );
}
