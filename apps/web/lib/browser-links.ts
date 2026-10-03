/**
 * Links a BROWSER opens: same-origin paths, never the API's own address.
 *
 * `NEXT_PUBLIC_BOSS_API_URL` is the address the web SERVER uses to reach the API — on an
 * installed host it is `http://127.0.0.1:8000`. Three screens inlined it into preview links, so
 * on boss-1 (console at https://100.93.58.20) "Open preview" sent the operator's browser to
 * THEIR OWN laptop's port 8000, where a different BOSS answered "preview not found"
 * (prev_6500cdc6176748ed8b05, 2026-10-03). The browser reaches the API through the console's own
 * origin (`app/v1/[...path]`, see lib/client.ts), so a gateway path stays relative. An absolute
 * URL the engine chose on purpose (BOSS_PREVIEW_PUBLIC_BASE) is kept as it is.
 */
export function browserHref(href: string): string {
  return /^https?:\/\//i.test(href) ? href : href.startsWith("/") ? href : `/${href}`;
}

/** The page of a running preview, through the console's own origin. */
export function previewWebHref(previewId: string): string {
  return `/v1/previews/${encodeURIComponent(previewId)}/web/`;
}
