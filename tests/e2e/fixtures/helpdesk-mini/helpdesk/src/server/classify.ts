export type Category = "Billing" | "Account access" | "Technical issue" | "Feature request" | "Other";

const RULES: Array<{ category: Category; keywords: string[] }> = [
  { category: "Billing", keywords: ["invoice", "charge", "refund", "billing", "payment"] },
  { category: "Account access", keywords: ["password", "login", "2fa", "locked out", "sign in"] },
  { category: "Technical issue", keywords: ["crash", "error", "bug", "not loading", "broken"] },
  { category: "Feature request", keywords: ["please add", "feature", "suggestion", "would love"] },
];

export function classifyByKeyword(text: string): { category: Category; confidence: number } {
  const lower = text.toLowerCase();
  let best: { category: Category; hits: number } = { category: "Other", hits: 0 };
  for (const rule of RULES) {
    const hits = rule.keywords.filter((k) => lower.includes(k)).length;
    if (hits > best.hits) best = { category: rule.category, hits };
  }
  if (best.hits === 0) return { category: "Other", confidence: 0.3 };
  return { category: best.category, confidence: Math.min(0.95, 0.5 + best.hits * 0.15) };
}
