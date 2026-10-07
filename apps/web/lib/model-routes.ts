/**
 * The model router's routes as the console reads them ("Why this model",
 * docs/product/MODEL_ROUTER.md). Plain data and pure functions: safe on the server and client.
 */
export type RouteRow = {
  id: string; step: string; step_label?: string; ref?: string | null; mode: string;
  tier?: string | null; tier_label?: string | null; label?: string;
  engine?: string | null; model?: string | null; why: string[];
  escalation?: string | null; moved_by_headroom?: boolean; budget_lite?: boolean;
  classifier?: string | null; confidence?: number | null;
};

export function parseRoutes(value: unknown): RouteRow[] {
  if (!Array.isArray(value)) return [];
  return value.filter((r): r is Record<string, unknown> => !!r && typeof r === "object")
    .map((r) => ({
      id: String(r.id ?? ""), step: String(r.step ?? ""),
      step_label: typeof r.step_label === "string" ? r.step_label : undefined,
      ref: typeof r.ref === "string" ? r.ref : null, mode: String(r.mode ?? "fixed"),
      tier: typeof r.tier === "string" ? r.tier : null,
      tier_label: typeof r.tier_label === "string" ? r.tier_label : null,
      label: typeof r.label === "string" ? r.label : undefined,
      engine: typeof r.engine === "string" ? r.engine : null,
      model: typeof r.model === "string" ? r.model : null,
      why: Array.isArray(r.why) ? r.why.map(String) : [],
      escalation: typeof r.escalation === "string" ? r.escalation : null,
      moved_by_headroom: r.moved_by_headroom === true, budget_lite: r.budget_lite === true,
      classifier: typeof r.classifier === "string" ? r.classifier : null,
      confidence: typeof r.confidence === "number" ? r.confidence : null,
    }))
    .filter((r) => r.id);
}

/** The latest route per step, in the order the steps first ran. */
export function latestPerStep(routes: RouteRow[]): RouteRow[] {
  const order: string[] = [];
  const last = new Map<string, RouteRow>();
  for (const r of routes) {
    const key = `${r.step}|${r.ref ?? ""}`;
    if (!last.has(key)) order.push(key);
    last.set(key, r);
  }
  return order.map((k) => last.get(k)!);
}


/** "OpenRouter · deepseek/deepseek-chat" → "OpenRouter · deepseek-chat": the vendor prefix of an
 *  OpenRouter-style id is noise in a narrow column; the full label goes in the tooltip. */
export function shortLabel(label: string): string {
  return label.replace(/ · [^·\s/]+\/([^·\s]+)$/, " · $1");
}
