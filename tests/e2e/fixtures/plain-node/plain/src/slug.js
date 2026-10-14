// The plumbing gate's plain Node app: ESM, no build, no typecheck, no lint, its own tests.
export function slug(text) {
  return String(text ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}
