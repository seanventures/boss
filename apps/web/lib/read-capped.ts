/**
 * A request body read under a byte cap (the console's /v1 proxy, for the Improve BOSS
 * collector's open POSTs — re-check item 3). Kept out of the route file: a Next route module may
 * export only its handlers.
 */
/** What the console accepts from a collector POST before refusing it: the API's own cap. */
export const COLLECTOR_BODY_CAP = 256 * 1024;

/** The body, read chunk by chunk and abandoned the moment it passes `cap` (null). A declared
 *  Content-Length over the cap is refused without reading at all. */
export async function readCapped(req: Request, cap: number): Promise<Uint8Array | null> {
  const declared = Number(req.headers.get("content-length") ?? "");
  if (Number.isFinite(declared) && declared > cap) return null;
  if (!req.body) return new Uint8Array(0);
  const reader = req.body.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > cap) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    parts.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.byteLength; }
  return out;
}

