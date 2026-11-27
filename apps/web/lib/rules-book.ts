/**
 * One Rules Book (BOSSAPP-572): read-only client for GET /v1/rules-book.
 */

export type EnforcementKind = "setting" | "gate" | "policy" | "prompt_only";

export type RuleBookEntry = {
  id: string;
  title: string;
  text: string;
  why: string;
  category: string;
  roles: string[];
  enforcement: {
    kind: EnforcementKind;
    ref: string;
    where: string;
  };
  source: string;
  universal: boolean;
  priority: number;
  effective_value?: unknown;
};

export type RulesBookView = {
  rules: RuleBookEntry[];
  categories: string[];
  enforcement_kinds: EnforcementKind[];
  roles: string[];
  counts_by_category: Record<string, number>;
  counts_by_enforcement: Record<string, number>;
  total: number;
  read_only: boolean;
};

type R = Record<string, unknown>;
const obj = (v: unknown): R => (v && typeof v === "object" && !Array.isArray(v) ? (v as R) : {});
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : d);

export function parseRulesBook(v: unknown): RulesBookView {
  const x = obj(v);
  const rules: RuleBookEntry[] = arr(x.rules).map((item) => {
    const r = obj(item);
    const enf = obj(r.enforcement);
    const kind = str(enf.kind, "prompt_only") as EnforcementKind;
    return {
      id: str(r.id),
      title: str(r.title),
      text: str(r.text),
      why: str(r.why),
      category: str(r.category, "ops"),
      roles: arr(r.roles).map((s) => str(s)).filter(Boolean),
      enforcement: {
        kind: (["setting", "gate", "policy", "prompt_only"] as EnforcementKind[]).includes(kind) ? kind : "prompt_only",
        ref: str(enf.ref),
        where: str(enf.where),
      },
      source: str(r.source, "operator_rule"),
      universal: r.universal === true,
      priority: typeof r.priority === "number" ? r.priority : 50,
      ...(r.effective_value !== undefined ? { effective_value: r.effective_value } : {}),
    };
  }).filter((r) => r.id);

  const countsByCategory: Record<string, number> = {};
  for (const [k, val] of Object.entries(obj(x.counts_by_category))) {
    if (typeof val === "number") countsByCategory[k] = val;
  }
  const countsByEnforcement: Record<string, number> = {};
  for (const [k, val] of Object.entries(obj(x.counts_by_enforcement))) {
    if (typeof val === "number") countsByEnforcement[k] = val;
  }

  return {
    rules,
    categories: arr(x.categories).map((c) => str(c)).filter(Boolean),
    enforcement_kinds: (arr(x.enforcement_kinds).map((e) => str(e)).filter(Boolean) as EnforcementKind[]),
    roles: arr(x.roles).map((r) => str(r)).filter(Boolean),
    counts_by_category: countsByCategory,
    counts_by_enforcement: countsByEnforcement,
    total: typeof x.total === "number" ? x.total : rules.length,
    read_only: x.read_only !== false,
  };
}

export async function readRulesBook(
  projectId: string,
  params: { category?: string; role?: string; enforcement?: string; q?: string } = {},
): Promise<{ ok: true; data: RulesBookView } | { ok: false; error: string }> {
  const qs = new URLSearchParams();
  if (projectId) qs.set("project_id", projectId);
  if (params.category) qs.set("category", params.category);
  if (params.role) qs.set("role", params.role);
  if (params.enforcement) qs.set("enforcement", params.enforcement);
  if (params.q) qs.set("q", params.q);
  const url = `/v1/rules-book${qs.toString() ? `?${qs.toString()}` : ""}`;
  try {
    const res = await fetch(url, { cache: "no-store" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = obj(body).detail;
      return { ok: false, error: typeof detail === "string" ? detail : `Rules book unavailable (${res.status}).` };
    }
    return { ok: true, data: parseRulesBook(body) };
  } catch (err) {
    return { ok: false, error: `Could not load rules book (${(err as Error)?.message || err}).` };
  }
}
