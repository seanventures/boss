/**
 * A PERSON'S AVATAR COLOUR, one rule everywhere: the colour they chose in My account
 * (`avatar_seed`, "0"–"7", PATCH /v1/identity/me), else a stable colour from their id. Every
 * Face of a person — lists, profiles, the account menu, group members, sign-in — goes through
 * here; tests/identity-avatar-is-one-rule.test.ts fails on a private copy of the hash.
 */
export function avatarTone(id: string, seed?: string | null): number {
  if (typeof seed === "string" && /^[0-7]$/.test(seed)) return Number(seed);
  let h = 0;
  for (let i = 0; i < String(id ?? "").length; i += 1) h = (h * 31 + String(id).charCodeAt(i)) >>> 0;
  return h % 8;
}
