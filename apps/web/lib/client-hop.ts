/**
 * The client address the console forwards for the Improve BOSS collector's POSTs (re-check C).
 *
 * Next fills X-Forwarded-For from the socket ONLY when the request has none, and keeps a
 * client's own value otherwise — so a value cannot be trusted by default. `BOSS_CONSOLE_PROXY_HOPS`
 * says how many proxies in front of the console append the real client (Community compose: 1, the
 * install's TLS proxy). The console forwards the hop that many from the right — the one the
 * nearest proxy wrote — and drops everything a client put left of it. With 0 (the console is
 * reached directly), nothing is forwarded: the API counts the console as one address rather than
 * believe a client.
 */
const IP = /^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f:]+)$/i;

export function proxyHops(): number {
  const n = Number(process.env.BOSS_CONSOLE_PROXY_HOPS ?? "0");
  return Number.isInteger(n) && n > 0 && n < 10 ? n : 0;
}

export function clientHop(forwarded: string | null, hops: number): string | null {
  if (!hops) return null;
  const chain = (forwarded ?? "").split(",").map((h) => h.trim()).filter(Boolean);
  const hop = chain[chain.length - hops];
  return hop && IP.test(hop) ? hop : null;
}
