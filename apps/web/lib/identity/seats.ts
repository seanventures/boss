/**
 * APPROVAL SEATS (IDENTITY.md §6.13), read into what Identity › Approval seats shows: each seat,
 * who holds it and which risk tiers it approves. GET /v1/identity/seats is the source; the API
 * re-checks every write (an active person, a tier never left with nobody).
 */
export const SEATS_PATH = "/identity/seats";

export type SeatRow = { seat_key: string; principal_id: string; display_name?: string | null;
  assigned_by?: string | null; assigned_at?: string | null };

export type SeatsResponse = {
  seats: SeatRow[];
  known_seats: string[];
  tiers: Record<string, string[]>;
  two_person_tiers: number[];
  vacant_tiers: number[];
  single_operator_waiver?: { effective?: boolean; setting?: boolean | null; active_people?: number } | null;
};

export type SeatHolder = { id: string; name: string };
export type SeatCard = {
  key: string;
  label: string;
  /** Tiers this seat approves, ascending. */
  tiers: number[];
  holders: SeatHolder[];
  /** Approves a tier but nobody holds it: an active Owner stands in. */
  vacant: boolean;
};

const LABELS: Record<string, string> = {
  ceo: "CEO", cto: "CTO", cao: "CAO", vp_agents: "VP Agents", release_manager: "Release manager",
};

/** "ceo" → "CEO", "vp_agents" → "VP Agents", anything else Title Case. */
export function seatLabel(key: string): string {
  return LABELS[key] ?? key.split(/[_-]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ");
}

/** One card per seat: the seats a tier needs first (lowest tier first), then the others. */
export function seatCards(data: SeatsResponse): SeatCard[] {
  const tiersOf = new Map<string, number[]>();
  for (const [tier, seats] of Object.entries(data.tiers ?? {})) {
    for (const s of seats ?? []) tiersOf.set(s, [...(tiersOf.get(s) ?? []), Number(tier)].sort((a, b) => a - b));
  }
  const keys = [...new Set([...(data.known_seats ?? []), ...tiersOf.keys(), ...(data.seats ?? []).map((r) => r.seat_key)])];
  const cards = keys.map((key) => {
    const holders = (data.seats ?? []).filter((r) => r.seat_key === key)
      .map((r) => ({ id: r.principal_id, name: r.display_name || r.principal_id }));
    const tiers = tiersOf.get(key) ?? [];
    return { key, label: seatLabel(key), tiers, holders, vacant: tiers.length > 0 && holders.length === 0 };
  });
  const first = (c: SeatCard) => (c.tiers.length ? c.tiers[0] : 99);
  return cards.sort((a, b) => first(a) - first(b) || a.label.localeCompare(b.label));
}

/** "Tier 2 · Tier 3 (two people)", or "No tier" for a seat no tier needs. */
export function tiersWords(tiers: number[], twoPerson: number[] = []): string {
  if (!tiers.length) return "No tier";
  return tiers.map((t) => `Tier ${t}${twoPerson.includes(t) ? " (two people)" : ""}`).join(" · ");
}

/** The holder list after adding or removing one person (no duplicates, order kept). */
export function nextHolders(current: string[], change: { add?: string; remove?: string }): string[] {
  const out = current.filter((id) => id !== change.remove);
  if (change.add && !out.includes(change.add)) out.push(change.add);
  return out;
}
