/**
 * Links a BROWSER opens: same-origin paths, never the API's own address.
 *
 * `NEXT_PUBLIC_BOSS_API_URL` is the address the web SERVER uses to reach the API — on an
 * installed host it is `http://127.0.0.1:8000`. Three screens inlined it into preview links, so
 * on boss-1 (console at https://100.64.0.20) "Open preview" sent the operator's browser to
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

/** A sandbox's own address (127.0.0.1:<port>, localhost): reachable from the API, never a browser. */
export function isLoopbackPreviewUrl(url: string | null | undefined): boolean {
  if (!url) return false;
  const lowered = url.trim().toLowerCase();
  return (
    lowered.includes("://127.") ||
    lowered.includes("://localhost") ||
    lowered.includes("://[::1]") ||
    lowered.includes("://0.0.0.0") ||
    lowered.startsWith("127.") ||
    lowered.startsWith("localhost") ||
    lowered.startsWith("[::1]") ||
    lowered.startsWith("0.0.0.0")
  );
}

/** Where a preview's Open goes: the address the engine gave it, unless that is a sandbox's
 *  loopback, then the console's own gateway path (Infra › Previews, enterprise validation). */
export function previewOpenHref(previewId: string, url: string | null | undefined): string {
  return url && !isLoopbackPreviewUrl(url) ? browserHref(url) : previewWebHref(previewId);
}
