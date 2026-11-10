// Files a ticket under a category from the words in its title and description.
export const CATEGORIES = ["Billing", "Account access", "Technical issue", "Feature request", "Other"];

const RULES = [
  { category: "Billing", keywords: ["invoice", "charge", "refund", "billing", "payment"] },
  { category: "Account access", keywords: ["password", "login", "2fa", "locked out", "sign in"] },
  { category: "Technical issue", keywords: ["crash", "error", "bug", "not loading", "broken"] },
  { category: "Feature request", keywords: ["please add", "feature", "suggestion", "would love"] },
];

export function classify(text) {
  const words = String(text ?? "").toLowerCase();
  let best = { category: "Other", hits: 0 };
  for (const rule of RULES) {
    const hits = rule.keywords.filter((k) => words.includes(k)).length;
    if (hits > best.hits) best = { category: rule.category, hits };
  }
  if (best.hits === 0) return { category: "Other", confidence: 0.3 };
  return { category: best.category, confidence: Math.min(0.95, 0.5 + best.hits * 0.15) };
}
