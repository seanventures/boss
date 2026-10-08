/**
 * A NETWORK ADDRESS IS SHOWN ONLY WHEN IT SAYS SOMETHING ABOUT THE PERSON.
 *
 * Behind the console's proxy, or inside the compose network, the API sees the console
 * container's address (172.18.0.8) for every request — so "New sign-in from 172.18.0.8" and a
 * Sessions list of identical 172.18.x addresses named our own network, not anyone's device. A
 * private, loopback, link-local or tailnet address is never shown as where someone was; a public
 * one is. The engine has the same rule for the notices it writes (identity/where.py).
 */

function v4(ip: string): number[] | null {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(ip);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  return parts.every((n) => n <= 255) ? parts : null;
}

/** The address when it is a public one; null for a private, internal or unreadable one. */
export function publicAddress(raw: string | null | undefined): string | null {
  if (!raw) return null;
  let ip = raw.trim().split("%")[0];
  const mapped = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/i.exec(ip);
  if (mapped) ip = mapped[1];
  const a = v4(ip);
  if (a) {
    const [x, y] = a;
    if (x === 0 || x === 10 || x === 127 || x >= 224) return null;
    if (x === 172 && y >= 16 && y <= 31) return null;
    if (x === 192 && y === 168) return null;
    if (x === 169 && y === 254) return null;
    if (x === 100 && y >= 64 && y <= 127) return null; // carrier-grade NAT, the tailnet
    return ip;
  }
  if (!/^[0-9a-f:]+$/i.test(ip) || !ip.includes(":")) return null; // a name, "internal", "console"
  const low = ip.toLowerCase();
  if (low === "::" || low === "::1") return null;
  if (/^f[cd]/.test(low) || /^fe[89ab]/.test(low) || /^ff/.test(low)) return null; // ULA, link-local, multicast
  return ip;
}

/** Where a request came from, as an audit row says it: the public address, else "private" (the raw address is its tooltip). */
export function sourceWords(raw: string | null | undefined): string {
  const pub = publicAddress(raw);
  if (pub) return pub;
  if (raw === "internal") return "BOSS itself";
  if (raw === "console") return "console";
  return raw ? "private" : "unknown";
}
