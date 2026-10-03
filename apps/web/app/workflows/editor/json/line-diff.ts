/**
 * A line diff for the JSON view's `Changes`: what this draft changes against the published
 * version, added lines green and removed lines red.
 *
 * Longest-common-subsequence over lines, after trimming the common head and tail (a typical
 * edit touches a few lines of a few hundred, so the table stays small). A pathological middle
 * past the cell budget degrades to "all of the old middle removed, all of the new added" —
 * still correct, just less tidy — rather than freezing the tab.
 */
export type DiffLine = { kind: "same" | "add" | "del"; text: string; oldNo: number | null; newNo: number | null };

const CELL_BUDGET = 4_000_000;

export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head
         && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;

  const out: DiffLine[] = [];
  for (let i = 0; i < head; i += 1) out.push({ kind: "same", text: a[i], oldNo: i + 1, newNo: i + 1 });

  const am = a.slice(head, a.length - tail);
  const bm = b.slice(head, b.length - tail);
  const n = am.length;
  const m = bm.length;
  if (n * m > CELL_BUDGET) {
    am.forEach((text, i) => out.push({ kind: "del", text, oldNo: head + i + 1, newNo: null }));
    bm.forEach((text, j) => out.push({ kind: "add", text, oldNo: null, newNo: head + j + 1 }));
  } else {
    // lcs[i][j] = LCS length of am[i:] and bm[j:], in one flat array.
    const w = m + 1;
    const lcs = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        lcs[i * w + j] = am[i] === bm[j] ? lcs[(i + 1) * w + j + 1] + 1
          : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && am[i] === bm[j]) {
        out.push({ kind: "same", text: am[i], oldNo: head + i + 1, newNo: head + j + 1 });
        i += 1; j += 1;
      } else if (i < n && (j >= m || lcs[(i + 1) * w + j] >= lcs[i * w + j + 1])) {
        // Removed before added, the order every diff reader expects.
        out.push({ kind: "del", text: am[i], oldNo: head + i + 1, newNo: null });
        i += 1;
      } else {
        out.push({ kind: "add", text: bm[j], oldNo: null, newNo: head + j + 1 });
        j += 1;
      }
    }
  }

  for (let k = tail; k > 0; k -= 1) {
    out.push({ kind: "same", text: a[a.length - k], oldNo: a.length - k + 1, newNo: b.length - k + 1 });
  }
  return out;
}

/** The diff with unchanged runs folded to `context` lines either side of a change. A fold is a
 *  `{ fold }` entry carrying how many lines it hides. */
export function foldDiff(lines: DiffLine[], context = 3): (DiffLine | { fold: number })[] {
  const keep = new Array(lines.length).fill(false);
  lines.forEach((l, i) => {
    if (l.kind === "same") return;
    for (let k = Math.max(0, i - context); k <= Math.min(lines.length - 1, i + context); k += 1) keep[k] = true;
  });
  const out: (DiffLine | { fold: number })[] = [];
  let hidden = 0;
  lines.forEach((l, i) => {
    if (keep[i]) {
      if (hidden) { out.push({ fold: hidden }); hidden = 0; }
      out.push(l);
    } else {
      hidden += 1;
    }
  });
  if (hidden) out.push({ fold: hidden });
  return out;
}
