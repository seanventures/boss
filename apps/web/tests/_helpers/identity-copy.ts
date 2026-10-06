/**
 * The identity screens' copy budget, on what they RENDER: a visible label is ≤ 6 words; a
 * sentence lives only in an element marked data-copy="prose" (≤ 24 words); API data
 * (data-copy="data", <code>) is not copy; a tooltip (title) is ≤ 14 words.
 */
export const LABEL_MAX = 6;
export const PROSE_MAX = 24;
export const TIP_MAX = 14;

const SKIP = "[title], [data-tip], [role='tooltip'], textarea, script, style, option, [hidden], [aria-hidden='true'], .idn-sr, .ag-sr, [data-copy='data'], [data-user-text], code, pre, time";

function words(text: string): number {
  return text.replace(/[·▸•|→←›]/g, " ").trim().split(/\s+/).filter(Boolean).length;
}

export function identityCopyAudit(root: Element): { labels: string[]; prose: string[]; tips: string[] } {
  const labels: string[] = [];
  const prose: string[] = [];
  for (const el of [root, ...root.querySelectorAll("*")]) {
    if (el.closest(SKIP)) continue;
    const proseRoot = el.closest("[data-copy='prose']");
    if (proseRoot) {
      if (el === proseRoot && words(proseRoot.textContent ?? "") > PROSE_MAX) prose.push((proseRoot.textContent ?? "").trim());
      continue;
    }
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? "").join("");
    if (words(own) > LABEL_MAX) labels.push(own.trim());
  }
  const tips = [...root.querySelectorAll("[title]")]
    .filter((el) => !el.closest("[data-copy='data'], code, pre, time"))
    .map((el) => el.getAttribute("title") ?? "")
    .filter((t) => words(t) > TIP_MAX);
  return { labels: [...new Set(labels)], prose: [...new Set(prose)], tips: [...new Set(tips)] };
}
