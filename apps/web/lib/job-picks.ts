/**
 * ONE MENU, ONE TOGGLE: a job's model menu lists each model once, and a "Subscription | API key"
 * toggle beside it chooses who pays — for any engine that offers both (operator, 2026-11-03:
 * "we can have a toggle button inside the list … to switch between key / subs both for codex and
 * Anthropic"). The server offers every (engine, model) on each credential it holds as a separate
 * option whose model carries the pick ("sub:sonnet", "key:sonnet" — claude_credential.split_pick);
 * this folds them back into one entry per model plus the picks it can take.
 */
import type { Job } from "./setup-types";

export type Pick = "sub" | "key";
export const PICK_LABEL: Record<Pick, string> = { sub: "Subscription", key: "API key" };
export const PICK_TIP: Record<Pick, string> = {
  sub: "Runs on the subscription's included usage.",
  key: "Runs on the API key, billed per call to your provider account.",
};

type Option = Job["options"][number];

export function splitPick(model: string | null | undefined): { pick: Pick | null; bare: string | null } {
  const raw = (model ?? "").trim();
  const at = raw.indexOf(":");
  if (at > 0) {
    const head = raw.slice(0, at).toLowerCase();
    if (head === "sub" || head === "key") return { pick: head, bare: raw.slice(at + 1).trim() || null };
  }
  return { pick: null, bare: raw || null };
}

export function withPick(pick: Pick | null, bare: string | null): string | null {
  return pick ? `${pick}:${bare ?? ""}` : bare;
}

/** "Claude Code · subscription · sonnet" → "Claude Code · sonnet" (the menu's one entry). */
function menuLabel(label: string): string {
  return label.replace(/ · (subscription|API key)(?= · |$)/, "");
}

export type MenuEntry = { engine: string; bare: string | null; label: string; picks: Pick[];
                          needs_key?: boolean; metered: boolean };

/** One entry per (engine, model), in the server's order, each with the picks it can take. */
export function foldOptions(options: Option[]): MenuEntry[] {
  const out: MenuEntry[] = [];
  const at = new Map<string, MenuEntry>();
  for (const o of options) {
    const { pick, bare } = splitPick(o.model);
    const key = `${o.engine}::${bare ?? ""}`;
    let entry = at.get(key);
    if (!entry) {
      entry = { engine: o.engine, bare, label: pick ? menuLabel(o.label) : o.label, picks: [],
                needs_key: o.needs_key, metered: o.metered };
      at.set(key, entry);
      out.push(entry);
    }
    if (pick && !entry.picks.includes(pick)) entry.picks.push(pick);
  }
  return out;
}

/** The pick a menu choice saves with: the job's current one when the model can take it, else
 *  the subscription when offered, else whatever is offered (none for an engine without picks). */
export function pickFor(entry: MenuEntry, current: Pick | null): Pick | null {
  if (!entry.picks.length) return null;
  if (current && entry.picks.includes(current)) return current;
  return entry.picks.includes("sub") ? "sub" : entry.picks[0];
}
