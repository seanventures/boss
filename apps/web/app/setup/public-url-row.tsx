"use client";

/**
 * THE PUBLIC URL BOSS USES (the API's H2 fix). Sign-in cookies, passkeys and every emailed link
 * (invitations, magic links, password resets) are bound to one public URL, BOSS_PUBLIC_URL, or
 * the address the install was claimed at, never to whatever Host a request names. Unset, the
 * API refuses to send those emails (`409 public_origin_unset`), so Setup says which URL is in use
 * and, when none is, exactly how to set it. Read from GET /v1/auth/options.
 */
import React, { useEffect, useState } from "react";

import { idGet } from "../../lib/identity/api";
import type { AuthOptions } from "../../lib/identity/types";
import { PUBLIC_URL_HOWTO, PUBLIC_URL_TIP } from "../../lib/identity/public-url";
import { SectionLabel } from "../section-label";

type Answer = Pick<AuthOptions, "public_origin" | "public_origin_source">;

export { PUBLIC_URL_HOWTO, PUBLIC_URL_TIP };

export function PublicUrlRow({ initial = null }: { initial?: Answer | null }) {
  const [answer, setAnswer] = useState<Answer | null>(initial);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (initial) return;
    let alive = true;
    idGet<AuthOptions>("/v1/auth/options")
      .then((o) => { if (alive) setAnswer({ public_origin: o.public_origin, public_origin_source: o.public_origin_source }); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [initial]);

  const origin = answer?.public_origin;
  const known = answer !== null && origin !== undefined;
  const source = answer?.public_origin_source === "env" ? "Set by BOSS_PUBLIC_URL."
    : answer?.public_origin_source === "claim" ? "Bound when this BOSS was claimed." : "In use for sign-in and links.";
  return (
    <section className="su-row" data-testid="su-row-public-url" aria-label="Public URL" id="su-public-url">
      <div className="su-row-label">
        <SectionLabel as="span" tip="Sign-in, passkeys and emailed links use this address.">Public URL</SectionLabel>
      </div>
      <div className="su-row-body">
        {failed ? <span className="su-faint">Not readable now</span>
          : !known ? <span className="su-faint">{answer ? "Not reported" : "Loading…"}</span>
          : origin ? (
            <span className="su-pill su-tone-green" data-testid="public-url" title={source} data-tip={source}>
              <span className="su-dot su-tone-green" aria-hidden="true" />{origin}
            </span>
          ) : (
            <span className="su-line" role="status">
              <span className="su-pill su-tone-red" data-testid="public-url-unset" title={PUBLIC_URL_TIP} data-tip={PUBLIC_URL_TIP}>
                <span className="su-dot su-tone-red" aria-hidden="true" />Not set
              </span>
              <span className="su-faint">Emailed links are off</span>
            </span>
          )}
      </div>
    </section>
  );
}
