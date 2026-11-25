/**
 * PLAIN WORDS ON EVERY SURFACE (engine services/plain_words.py is the source; this is the same
 * mapping for text the console renders from older records).
 *
 * The e2e recipe-box walk (2026-11-18) found BOSS's own words on the build page, the merge card
 * and the work list: "· retry 2" in titles, the raw repair prompt ("Autonomous review-repair: the
 * quality gate blocked on code_review_passed, …") as a headline, "not convened: profile 'lean'
 * does not include this seat", gate keys in brackets. The engine now sends plain words; this
 * cleans what an older record still carries. Keys stay in a details fold only.
 */

/** gate key → label. THE SAME TABLE as engine factory_view.GATE_LABELS (a test compares them). */
export const GATE_LABELS: Record<string, string> = {
  code_written: "Code written",
  written_tests_pass: "New tests pass",
  existing_tests_intact: "Old tests pass",
  tests_written_first: "Tests came first",
  coder_did_not_edit_tests: "Tests untouched",
  brief_tests_written: "Named tests written",
  tests_strong_enough: "Tests catch bugs",
  code_review_passed: "Review passed",
  security_clean: "Security clean",
  consistent: "Consistent",
  acceptance_met: "Must-haves met",
  input_fidelity: "Derived from input",
  nothing_broke: "Nothing broke",
  app_serves: "App starts",
  web_package_honest: "Package honest",
  template_tests: "Template tests",
  no_committed_secrets: "No secrets",
};

/** A failed gate as what went wrong. THE SAME TABLE as engine plain_words.FAILED_WORDS. */
export const FAILED_WORDS: Record<string, string> = {
  code_written: "No code written",
  written_tests_pass: "New tests fail",
  existing_tests_intact: "Old tests broke",
  tests_written_first: "Tests not written first",
  coder_did_not_edit_tests: "Coder edited the tests",
  brief_tests_written: "Named tests missing",
  tests_strong_enough: "Tests too weak",
  code_review_passed: "Review asked for changes",
  security_clean: "Security findings open",
  consistent: "Not consistent with the code",
  acceptance_met: "Must-haves not all met",
  input_fidelity: "Not derived from input",
  nothing_broke: "Something broke",
  app_serves: "App does not start",
  web_package_honest: "Package not honest",
  template_tests: "Template tests fail",
  no_committed_secrets: "A secret was committed",
};

/** "Review asked for changes", never "Review passed" beside a ✗. */
export function gateFailedWords(key: string): string {
  return FAILED_WORDS[key] ?? `${gateLabel(key)}: failed`;
}

const RETRY_TAIL = /\s*·\s*retry\s*\d+\b/gi;
const REPAIR_PREFIX = /^\s*(?:Autonomous review-repair|Auto review-repair round \d+)/i;
const NOT_CONVENED = /not convened:\s*profile\s*'([^']*)'\s*does not include this seat/gi;
/** Keys with an underscore: "consistent" is also an ordinary word. */
const KEYS = Object.keys(GATE_LABELS).filter((k) => k.includes("_")).sort((a, b) => b.length - a.length);
const keyRe = (k: string) => new RegExp(`(?<![A-Za-z0-9_])${k}(?![A-Za-z0-9_])`, "g");

export function gateLabel(key: string): string {
  if (GATE_LABELS[key]) return GATE_LABELS[key];
  if (key.startsWith("iac_")) return `Infra: ${key.slice(4).replaceAll("_", " ")}`;
  const s = key.replaceAll("_", " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** A build's name without the attempt counter. */
export function plainTitle(title: string | null | undefined): string {
  return String(title ?? "").replace(RETRY_TAIL, "").trim();
}

/** Why a check did not run, as the project's quality level. */
export function plainSkip(reason: string | null | undefined): string {
  return String(reason ?? "").replace(NOT_CONVENED, (_m, level: string) =>
    !level || level.toLowerCase() === "none"
      ? "Not part of this project's quality level"
      : `Not part of the ${level.charAt(0).toUpperCase()}${level.slice(1).toLowerCase()} quality level`);
}

/** Any sentence a surface shows, plain: repair prompt → one sentence, keys → labels. */
export function plainSentence(text: string | null | undefined): string {
  const s = String(text ?? "");
  if (!s) return s;
  if (REPAIR_PREFIX.test(s)) {
    const lead = s.split(". ")[0];
    const named = KEYS.filter((k) => keyRe(k).test(lead));
    const order = named.sort((a, b) => lead.indexOf(a) - lead.indexOf(b)).map(gateFailedWords);
    return order.length ? `BOSS is fixing what its checks found: ${order.join(", ")}` : "BOSS is fixing what its checks found";
  }
  let out = plainSkip(s).replace(RETRY_TAIL, "");
  // A sentence about failing names the failure, never the pass label (engine plain_sentence).
  const words = /fail|block/i.test(out) ? gateFailedWords : gateLabel;
  for (const k of KEYS) {
    out = out.replace(new RegExp(`\\s*\\(\\s*${k}\\s*\\)(?=\\s*:)`, "g"), "").replace(keyRe(k), words(k));
  }
  return out;
}

/** The internal words a rendered string still carries ([] when plain). The guard tests use it. */
export function leaks(text: string | null | undefined): string[] {
  const s = String(text ?? "");
  const found: string[] = [];
  if (new RegExp(RETRY_TAIL.source, "i").test(s)) found.push("retry N");
  if (/Autonomous review-repair|Auto review-repair round/i.test(s)) found.push("repair prompt");
  if (new RegExp(NOT_CONVENED.source, "i").test(s)) found.push("not convened");
  for (const k of KEYS) if (keyRe(k).test(s)) found.push(k);
  return found;
}
