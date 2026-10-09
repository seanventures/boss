/**
 * A link's alias (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.2): the stem of the env vars the consumer
 * reads, `BOSS_LINK_<ALIAS>_URL` and `_USERS`. The API's rule ([A-Z][A-Z0-9_]{1,30}), said in
 * words before a press, and a default made from the provider's name.
 */
export const ALIAS_RE = /^[A-Z][A-Z0-9_]{1,30}$/;

export function aliasProblem(alias: string): string {
  if (!alias) return "Give it an alias.";
  return ALIAS_RE.test(alias) ? "" : "An alias is 2 to 31 of A–Z, 0–9 and _, starting with a letter.";
}

/** An alias from an app's name: BANKING, SHIFT_SWAP_BOARD. */
export function aliasFor(name: string): string {
  const up = name.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^[^A-Z]+/, "").replace(/_+$/, "");
  const cut = up.slice(0, 31).replace(/_+$/, "");
  return cut.length >= 2 ? cut : "APP";
}
