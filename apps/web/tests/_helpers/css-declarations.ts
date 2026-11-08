/** A small CSS reader for the stylesheet guards (page widths, type sizes): every declaration
 *  with the selector of the rule it sits in and its line. */
export type Decl = { selector: string; prop: string; value: string; line: number };

/** Every declaration with the selector of the rule it sits in (at-rule preludes are not
 *  declarations, so `@media (max-width: 900px)` is never read as a width). */
export function declarations(css: string): Decl[] {
  const src = css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));
  const out: Decl[] = [];
  const stack: string[] = [];
  let start = 0;
  const breaks: number[] = [];
  for (let i = src.indexOf("\n"); i >= 0; i = src.indexOf("\n", i + 1)) breaks.push(i);
  const lineAt = (i: number) => {
    let lo = 0, hi = breaks.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (breaks[mid] < i) lo = mid + 1; else hi = mid; }
    return lo + 1;
  };
  const flush = (end: number) => {
    const chunk = src.slice(start, end);
    let offset = start;
    for (const part of chunk.split(";")) {
      const colon = part.indexOf(":");
      const sel = stack[stack.length - 1] ?? "";
      if (colon > 0 && sel && !sel.startsWith("@")) {
        const prop = part.slice(0, colon).trim().toLowerCase();
        if (/^[a-z-]+$/.test(prop)) {
          out.push({ selector: sel, prop, value: part.slice(colon + 1).trim(),
            line: lineAt(offset + part.search(/\S/)) });
        }
      }
      offset += part.length + 1;
    }
  };
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (ch === "{") {
      const prelude = src.slice(start, i);
      // A nested rule's prelude follows the last declaration of its parent.
      const cut = prelude.lastIndexOf(";");
      if (cut >= 0) { flush(start + cut); }
      stack.push(prelude.slice(cut + 1).trim().replace(/\s+/g, " "));
      start = i + 1;
    } else if (ch === "}") {
      flush(i);
      stack.pop();
      start = i + 1;
    }
  }
  return out;
}
