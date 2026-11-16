/**
 * PORTABILITY ON THE CONSOLE (BOSSAPP-836). What the person sees of a project bundle, of the
 * install's backups, and of an upgrade rollback. Secrets are the engine's empty placeholders
 * (services/portability.py); a value still sitting under a secret-shaped key is a leak and is
 * not shown. Rollback is `boss rollback` on the host (BOSSAPP-839): the API has no Docker socket.
 */

export const PLACEHOLDER_KEY = "$secret_placeholder";

const SECRET_KEY = /(^|_)(secret|secrets|password|passwd|token|api_?key|private_?key|access_?key|bearer|authorization|client_secret|session_token)($|_)/i;
const NOT_A_VALUE = /_(kind|name|type|header|count|limit|budget|order|label|id_hint)$/i;
const SAFE_BACKUP_PATH = /^[\w./:-]+$/;

export type SecretPlaceholder = { kind: string; name: string; scope_type?: string };
export type BackupRow = {
  id: string;
  status: string;
  verified_at?: string | null;
  error?: string | null;
  started_at?: string | null;
  kind?: string;
};
export type BackupHealth = { state: "none" | "failed" | "running" | "ok" | "pending"; sentence: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function remember(found: SecretPlaceholder[], raw: Record<string, unknown>) {
  const name = typeof raw.name === "string" ? raw.name : "";
  if (!name) return;
  found.push({
    kind: typeof raw.kind === "string" ? raw.kind : "",
    name,
    scope_type: typeof raw.scope_type === "string" ? raw.scope_type : undefined,
  });
}

function walk(value: unknown, path: string, leaks: string[], found: SecretPlaceholder[]) {
  if (Array.isArray(value)) {
    value.forEach((item, i) => walk(item, `${path}[${i}]`, leaks, found));
    return;
  }
  if (!isRecord(value)) return;
  if (PLACEHOLDER_KEY in value && isRecord(value[PLACEHOLDER_KEY])) {
    remember(found, value[PLACEHOLDER_KEY]);
    return;
  }
  if (value.placeholder === true) {
    remember(found, value);
    return;
  }
  for (const [key, item] of Object.entries(value)) {
    const here = path ? `${path}.${key}` : key;
    if (typeof item === "string" && item.trim() && SECRET_KEY.test(key) && !NOT_A_VALUE.test(key)) leaks.push(here);
    else walk(item, here, leaks, found);
  }
}

/** Typed empty placeholders in a bundle: nested `$secret_placeholder`s and the secrets list. */
export function secretPlaceholders(bundle: unknown): SecretPlaceholder[] {
  const found: SecretPlaceholder[] = [];
  walk(bundle, "", [], found);
  return found;
}

/** Paths where a secret-shaped key still holds a string. Empty when the bundle is safe to show. */
export function secretLeaks(bundle: unknown): string[] {
  const leaks: string[] = [];
  walk(bundle, "", leaks, []);
  return leaks;
}

/** Health from GET /v1/instance/backups (newest first). */
export function backupHealth(backups: BackupRow[] | null | undefined): BackupHealth {
  const rows = Array.isArray(backups) ? backups : [];
  if (!rows.length) return { state: "none", sentence: "No backup yet." };
  const newest = rows[0];
  if (newest.status === "failed") {
    const why = newest.error ? `: ${newest.error}` : "";
    return { state: "failed", sentence: `The newest backup failed${why}.` };
  }
  if (newest.status === "running") return { state: "running", sentence: "A backup is running." };
  const good = rows.find((row) => (row.status === "verified" || row.status === "pruned") && row.verified_at);
  if (good) return { state: "ok", sentence: `Last verified backup ${good.id}.` };
  return { state: "pending", sentence: "No verified backup yet." };
}

/** The host command that puts the previous release back. Null when the path is not a directory. */
export function rollbackCommand(restoreBackup?: string): string | null {
  const path = (restoreBackup ?? "").trim();
  if (!path) return "boss rollback";
  if (!SAFE_BACKUP_PATH.test(path)) return null;
  return `boss rollback --restore-backup ${path}`;
}

/** One line for a dry-run summary: "1 update in model settings". */
export function importSummary(summary: unknown): string {
  if (!isRecord(summary)) return "Nothing would change.";
  const bits: string[] = [];
  for (const [section, actions] of Object.entries(summary)) {
    if (!isRecord(actions)) continue;
    for (const [action, count] of Object.entries(actions)) {
      if (typeof count === "number" && count > 0) bits.push(`${count} ${action} in ${section.replace(/_/g, " ")}`);
    }
  }
  return bits.join(", ") || "Nothing would change.";
}
