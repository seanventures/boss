/**
 * A page outside the identity/account sections that the viewer cannot open (lib/identity/pages.ts
 * CONSOLE_PAGES): <AccessDenied> naming the permission, rendered INSTEAD of the page — the page
 * returns this before it fetches anything, so nothing 403s and no data reaches the HTML.
 */
import { pageFor } from "../../lib/identity/pages";
import { AccessDenied } from "./access-denied";

export function PageDenied({ path }: { path: string }) {
  const page = pageFor(path);
  const permission = page && "permission" in page.access ? page.access.permission : null;
  if (!page || !permission) return null;
  return (
    <main className="idn" data-testid="page-denied" data-path={page.path}>
      <AccessDenied permission={permission} page={page.label} />
    </main>
  );
}
