/**
 * THE ONE WORD FOR "WHAT AM I HERE" — the sidebar and the account menu both say it.
 *
 * /v1/identity/me answers `access_roles` sorted by key, so an Owner who also holds Admin (or the
 * Operator binding a claimed install starts with) read "Admin · Owner" or "Operator · Owner" in
 * the sidebar: the first word, the one people read, was not their role. The highest built-in role
 * wins; a custom role is named only when no built-in one is held.
 */
const RANK = ["owner", "admin", "operator", "auditor", "viewer"] as const;

const word = (key: string) => key.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export function primaryRoleLabel(roles: readonly string[] | null | undefined): string {
  const held = [...new Set(roles ?? [])];
  const top = RANK.find((r) => held.includes(r)) ?? held.slice().sort()[0];
  return top ? word(top) : "No access role";
}

/** Every role, highest first — for a tooltip that explains the one word. */
export function allRoleLabels(roles: readonly string[] | null | undefined): string {
  const held = [...new Set(roles ?? [])];
  const rank = (r: string) => {
    const i = (RANK as readonly string[]).indexOf(r);
    return i < 0 ? RANK.length : i;
  };
  return held.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b)).map(word).join(" · ");
}
