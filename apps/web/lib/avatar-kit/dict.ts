/**
 * Lookup tables that only answer for their own keys.
 *
 * A plain object literal inherits from Object.prototype, so `TABLE["constructor"]`,
 * `TABLE["toString"]` and `TABLE["__proto__"]` are hits. Role keys and part ids arrive from
 * the API (a custom role_key, a persona's avatar), so every kit table is built here, with no
 * prototype, and read through `own()` where the table is a literal.
 */

/** A table with no prototype: only the keys it was built from are hits. */
export function dict<T>(entries: Iterable<readonly [string, T]>): Readonly<Record<string, T>> {
  const out: Record<string, T> = Object.create(null);
  for (const [k, v] of entries) out[k] = v;
  return out;
}

/** `table[key]` when the table itself holds the key; never an inherited member. */
export function own<T>(table: Readonly<Record<string, T>>, key: string | null | undefined): T | undefined {
  return key != null && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}
