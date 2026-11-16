/** One failed test already named in a verify tail, or null.
 *  Lines are the ones last_verify_output keeps (pytest, unittest, go, node:test, jest/vitest).
 *  The name is copied from the tail. A count, or more than one name, is null.
 *  A rerun command is never built. */

const ANSI = /\x1b\[[0-9;?]*[A-Za-z]/g;
const DURATION = /\s*\(\s*\d+(?:\.\d+)?\s*m?s\s*\)\s*$/;

const LINES: RegExp[] = [
  /^FAILED\s+(\S+::\S+?)(?:\s+-\s.*)?$/,
  /^(\S+::\S+)\s+FAILED\b/,
  /^(?:FAIL|ERROR):\s+(\S+\s+\(.+\))/,
  /^\s*---\s+FAIL:\s+(\S+)/,
  /^not ok \d+\s+-?\s*(.+?)\s*(?:#.*)?$/,
  /^\s*(?:×|✕|✘|✗)\s+(.+)$/,
  /^\s*FAIL\s+(\S+\s+>\s+.+)$/,
];

export function failedTestName(tail: string | null | undefined): string | null {
  if (!tail) return null;
  const names: string[] = [];
  for (const raw of tail.split("\n")) {
    const line = raw.replace(ANSI, "").replace(/\r$/, "");
    for (const pattern of LINES) {
      const found = pattern.exec(line);
      if (!found) continue;
      const name = found[1].replace(DURATION, "").trim();
      if (name && tail.includes(name) && !names.includes(name)) names.push(name);
      break;
    }
  }
  return names.length === 1 ? names[0] : null;
}
