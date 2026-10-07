"use client";

/**
 * A LINK AN OLDER BUILD PRINTED (`boss identity recover-owner` before the hotfix printed
 * `/auth/recover#token=…`). It is forwarded, in the browser, to the page its kind lands on
 * (lib/identity/auth-links.gen.ts LEGACY_LINK_ALIASES), with the token moved to the fragment key
 * that page reads. The token never leaves the fragment, so no server or log ever sees it.
 */
import React, { useEffect, useState } from "react";

import { AUTH_LINKS, LEGACY_LINK_ALIASES } from "../../../../lib/identity/auth-links.gen";
import { AuthStage, Logo } from "../../auth-parts";

export function ForwardLegacyLink({ host, path }: { host: string; path: string }) {
  const [lost, setLost] = useState(false);
  useEffect(() => {
    const alias = LEGACY_LINK_ALIASES[path];
    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const token = alias ? hash.get(alias.fragment) ?? hash.get(AUTH_LINKS[alias.kind].fragment) : null;
    if (!alias || !token) { setLost(true); return; }
    const target = AUTH_LINKS[alias.kind];
    window.location.replace(`${target.path}#${target.fragment}=${encodeURIComponent(token)}`);
  }, [path]);
  return (
    <AuthStage label="Opening your link">
      <Logo host={host} />
      <h1>{lost ? "This link is incomplete" : "Opening your link…"}</h1>
      {lost ? <p className="idn-s" data-copy="prose">Copy the whole link from the terminal, or run the command again for a new one.</p> : null}
    </AuthStage>
  );
}
