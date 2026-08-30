/** A run, reduced to only what naming an app needs. */
export type AppRun = {
  title?: string | null;
  finalize_ctx?: Record<string, unknown> | null;
};

/**
 * Prefixes that say what KIND of run this is, not what the app is called.
 * They belong on a badge — left in front, four different apps all displayed as "QA benchmark".
 */
const LABELS: Array<[RegExp, string]> = [
  [/^\s*QA benchmark:\s*/i, "benchmark"],
  [/^\s*Follow-up:\s*deferred review findings\s*[-:]\s*/i, "follow-up"],
  [/^\s*Follow-up:\s*/i, "follow-up"],
  [/^\s*Official\s+/i, "official"],
  [/^\s*e2e smoke\s*[-:]\s*/i, "e2e"],
];

/** Headings that describe a DOCUMENT rather than a product. Never a name. */
const BOILERPLATE =
  /^(product requirements document|prd|design|spec|specification|overview|summary|web app|version\b.*|introduction|document)\b[\s:–—-]*$/i;

function norm(s: string): string {
  return (s ?? "")
    .normalize("NFKC")
    .replace(/[  ]/g, " ")
    // U+2010 HYPHEN and U+2012 FIGURE DASH belong here too. They are what a model emits when
    // it writes "Pull‐Request", they are NOT the ASCII hyphen, and leaving them out meant the
    // name matched no pattern below and rendered with a character the operator cannot type.
    .replace(/[\u2010\u2011\u2012\u2013\u2014]/g, "-")
    .replace(/[“”]/g, '"')
    .replace(/[‘’]/g, "'");
}

/**
 * Drop a leading emoji or symbol so the boilerplate test can see the words behind it.
 *
 * A card read "Product Requirements Document" — a document TYPE shown as a product name. The
 * BOILERPLATE pattern matches that phrase, but it is anchored at ^ and the real title began
 * with a page emoji, so the check never fired and the heading became the app's name. Emoji are
 * decoration on a title; they must not change how it is read.
 */
function unadorn(line: string): string {
  return line
    .replace(/^[\s\p{Extended_Pictographic}\p{So}\p{Sk}\u200d\ufe0f•·*_>#]+/u, "")
    .trim();
}

/**
 * What an app IS, pulled out of the run that built it.
 *
 * Titles were built server-side by truncating whatever prose started the brief, so a card read
 * "Web app — # Product Requirements Document (PRD) **Project:** Lightweight Self-" for a
 * project the PRD names on its next line: "Shorty". That is fixed at the source now, but every
 * run created before it keeps its old title, so the same reading happens here.
 *
 * The document usually says what it is called — `**Project:** X`, a first heading, a quoted
 * working title. Read that; strip the wrappers BOSS adds; lift the run-kind into a label.
 */
export function appIdentity(run: AppRun): {
  name: string;
  kind?: string;
  blurb?: string;
  label?: string;
} {
  const bench = ((run.finalize_ctx ?? {}) as Record<string, unknown>).qa_benchmark as
    { name?: string; tagline?: string; summary?: string } | undefined;
  if (bench?.name) {
    return { name: bench.name, kind: bench.tagline, blurb: bench.summary };
  }

  let t = unadorn(norm(run.title ?? "").replace(/^\[\d+\/\d+\]\s*/, ""));
  t = t.replace(/\s*·\s*retry\s*\d+\s*$/i, "");

  let label = "";
  for (let pass = 0; pass < 3; pass += 1) {
    const hit = LABELS.find(([re]) => re.test(t));
    if (!hit) break;
    t = t.replace(hit[0], "");
    label = label || hit[1];
  }
  t = t
    .replace(/^\s*(web app|mobile app|service|api)\s*[-—:]\s*/i, "")
    .replace(/^\s*(build|create|make)\s+/i, "")
    .replace(/^\s*(a|an|the)\s+/i, "");

  // The document usually names itself.
  let named = "";
  const explicit = t.match(/\*\*Project:?\*\*\s*(.+)/) ?? t.match(/^\s*Project:\s*(.+)$/m);
  if (explicit) {
    named = explicit[1];
  } else {
    for (const raw of t.split("\n")) {
      const line = unadorn(raw.replace(/^#+\s*/, ""));
      if (!line || BOILERPLATE.test(line)) continue;
      const stripped = line.replace(/\s*[-–—]\s*(product requirements document|prd)\b.*$/i, "");
      if (stripped && !BOILERPLATE.test(stripped)) {
        named = stripped;
        break;
      }
    }
  }
  // "PRD: BugBox — …" — boilerplate as a PREFIX, which the whole-line check cannot see.
  // Left in, the split on ":" handed back "PRD" as the app's name.
  // "PRD: BugBox" and "PRD — BugBox" are the same sentence. This only accepted the colon, so
  // the dash form survived, split into ["PRD", "BugBox"], and the app was named PRD.
  named = named.replace(
    /^\s*(product requirements document|prd|design|spec|specification|overview)\s*[:\-]\s*/i, "");
  named = named.trim().replace(/[:\-–—]+$/, "").trim()
    .replace(/\((?:working title|draft|v\d[\d.]*)\)/i, "").trim();

  // A quoted short name inside a descriptor is what people actually say out loud.
  const quoted = named.match(/"([^"]{2,32})"|'([^']{2,32})'/);
  if (quoted) {
    const short = (quoted[1] ?? quoted[2]).trim();
    const rest = named.replace(quoted[0], " ").replace(/\s+/g, " ").trim().replace(/^[-–—,\s]+/, "");
    return { name: short.slice(0, 46), kind: rest.slice(0, 44) || undefined, label: label || undefined };
  }

  const parts = named.split(/\s+[-–—]\s+|:\s+/);
  let name = (parts[0] || named || "Untitled app").trim();
  const tail = parts.slice(1).join(" — ").replace(/\*\*Status:\*\*.*$/i, "").trim();

  // A bare descriptive phrase reads as a sentence fragment on a card.
  if (name && name[0] === name[0].toLowerCase() && /[a-z]/.test(name[0])) {
    const drop = new Set(["web", "app", "application", "site", "website", "tool"]);
    const words = name.split(/\s+/).filter((w) => !drop.has(w.toLowerCase()));
    name = (words.length ? words : name.split(/\s+/))
      .slice(0, 4)
      .map((w) => (w === w.toUpperCase() ? w : w.charAt(0).toUpperCase() + w.slice(1)))
      .join(" ");
  }

  // LAST GATE. Everything above tries to find a real name; this makes sure a document TYPE
  // never reaches the card as one. "Product Requirements Document" is what the artifact is,
  // not what the product is called — and left through, four different apps read identically.
  // THE NAME WAS FOUND, THEN THROWN AWAY.
  //
  // "PRD — Pull-Request Review-Time Tracker (PR-RTT)" split into head "PRD" and tail
  // "Pull-Request Review-Time Tracker (PR-RTT)". The gate below correctly refused to call an
  // app "PRD" — and then rendered "Untitled app" with the real name sitting directly above it
  // as the descriptor chip. Every card built from a titled PRD read Untitled while displaying
  // its own title.
  //
  // If the head is a document type, the tail is not a subtitle. It is the name.
  let final = BOILERPLATE.test(unadorn(name)) ? "" : name;
  let kind = tail;
  if (!final && tail) {
    const promoted = tail.split(/\s+[-–—]\s+|:\s+/);
    const head = (promoted[0] || tail).trim();
    if (head && !BOILERPLATE.test(unadorn(head))) {
      final = head;
      kind = promoted.slice(1).join(" — ").trim();
    }
  }
  return {
    name: final.slice(0, 46) || "Untitled app",
    kind: kind ? kind.slice(0, 44) : undefined,
    label: label || undefined,
  };
}

/**
 * Name an app from the RUNS THAT BUILT IT, not from whichever one sorted first.
 *
 * A card takes its name from `members[0]`, and a follow-up run is titled after the work it is
 * doing — "Follow-up: deferred review findings — 📄 Product Requirements Document" — which
 * carries no product name at all. When that run led the group, a shipped app with a perfectly
 * good name on its other runs rendered as "Untitled app".
 *
 * Any run in the group can supply the name; they are all the same app. Falls back to the first
 * run's identity so a group where genuinely nobody knows the name still returns its label and
 * descriptor rather than nothing.
 */
export function groupIdentity(runs: AppRun[]): ReturnType<typeof appIdentity> {
  const first = appIdentity(runs[0] ?? {});
  if (first.name !== "Untitled app") return first;
  for (const run of runs.slice(1)) {
    const id = appIdentity(run);
    if (id.name !== "Untitled app") {
      // Keep the LEADING run's label ("follow-up"): it says what this card's newest work is,
      // which is not something the run that supplied the name can tell you.
      return { ...id, label: first.label ?? id.label };
    }
  }
  return first;
}
