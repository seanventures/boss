/**
 * THE ONBOARDING AGENT'S MACHINE CHECKS (BOSSAPP-924), for a project whose code BOSS keeps on
 * this machine: git · repo · disk · network · model as a row of dots, what the agent fixed itself,
 * and one line for each thing it could not fix (with where it is fixed).
 *
 * The engine draws nothing: project_experience/onboarding.machine_view shapes them on the
 * repo_access checklist item as `machine`; this parses it safely.
 */

export type MachineOk = "true" | "false" | "unknown";
export type MachineDot = { key: string; label: string; ok: MachineOk; words: string };
export type MachineCant = { key: string; line: string; href: string | null; link: string | null };
export type MachineChecks = { dots: MachineDot[]; fixed: string[]; online: boolean; cant: MachineCant[] };

const o = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});
const s = (v: unknown): string => (typeof v === "string" ? v : "");
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/** The five dots, in order, whatever the engine sent (a missing check is "unknown"). */
export const MACHINE_KEYS = [
  { key: "git", label: "git" }, { key: "local_repo", label: "repo" }, { key: "disk", label: "disk" },
  { key: "network", label: "network" }, { key: "model", label: "model" },
] as const;

function okOf(v: unknown): MachineOk {
  if (v === true || v === "true") return "true";
  if (v === false || v === "false") return "false";
  return "unknown";
}

export function parseMachine(v: unknown): MachineChecks | null {
  const x = o(v);
  if (!Array.isArray(x.dots)) return null;
  const sent = new Map(arr(x.dots).map((d) => [s(o(d).key), o(d)] as const));
  const dots = MACHINE_KEYS.map(({ key, label }) => {
    const d = sent.get(key);
    return { key, label: s(d?.label) || label, ok: okOf(d?.ok), words: s(d?.words) };
  });
  return {
    dots,
    fixed: arr(x.fixed).map(s).filter(Boolean),
    online: x.online === true,
    cant: arr(x.cant).map((c) => {
      const y = o(c);
      return { key: s(y.key), line: s(y.line), href: s(y.href) || null, link: s(y.link) || null };
    }).filter((c) => c.line),
  };
}

/** "Fixed: made the local repository, made the first commit" — "" when it fixed nothing. */
export function fixedWords(m: MachineChecks | null | undefined): string {
  return m?.fixed.length ? `Fixed: ${m.fixed.join(", ")}` : "";
}
