/**
 * A page outside the identity/account sections that the viewer cannot open (lib/identity/pages.ts
 * CONSOLE_PAGES): <AccessDenied> naming the permission, rendered INSTEAD of the page, the page
 * returns this before it fetches anything, so nothing 403s and no data reaches the HTML. When the
 * viewer's access could not be checked at all (the identity API is unreachable), it says that
 * instead, with Retry: the page fails closed, never open.
 */
import React from "react";

import { PROJECT_ID } from "../../lib/client";
import { pageFor } from "../../lib/identity/pages";
import { loadIdentityCached } from "../../lib/identity/server-cache";
import { AccessDenied } from "./access-denied";
import { accessUncheckable } from "./route-shell";
import { AccessUnchecked } from "./states";

export async function PageDenied({ path }: { path: string }) {
  const page = pageFor(path);
  const permission = page && "permission" in page.access ? page.access.permission : null;
  if (!page || !permission) return null;
  if (accessUncheckable(await loadIdentityCached(PROJECT_ID))) {
    return <main className="idn" data-testid="page-unchecked" data-path={page.path}><AccessUnchecked page={page.label} /></main>;
  }
  return (
    <main className="idn" data-testid="page-denied" data-path={page.path}>
      <AccessDenied permission={permission} page={page.label} />
    </main>
  );
}
