/**
 * AN SVG DRAWN THROUGH <img> IS A STANDALONE DOCUMENT (round 3, boss-1).
 *
 * An <img src="data:image/svg+xml…"> renders only an SVG whose root declares
 * xmlns="http://www.w3.org/2000/svg". Inline SVG for HTML may omit it, and the TOTP QR (segno's
 * svg_inline) did: the enrolment screen showed a broken image. Every data URI the console builds
 * for an SVG goes through here, which adds the namespace when the root lacks it.
 * tests/an-svg-in-an-img-has-its-namespace.test.ts keeps it the only way.
 */
const SVG_NS = "http://www.w3.org/2000/svg";

/** The SVG with the namespace on its root element (unchanged when it is already there). */
export function standaloneSvg(svg: string): string {
  const root = /<svg\b[^>]*>/i.exec(svg);
  if (!root) return svg;
  if (/\sxmlns\s*=\s*["']http:\/\/www\.w3\.org\/2000\/svg["']/i.test(root[0])) return svg;
  const tag = root[0].replace(/^<svg\b/i, `<svg xmlns="${SVG_NS}"`);
  return svg.slice(0, root.index) + tag + svg.slice(root.index + root[0].length);
}

function base64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

/** `data:image/svg+xml;base64,…` for an SVG string, namespace guaranteed. */
export function svgDataUri(svg: string): string {
  return `data:image/svg+xml;base64,${base64(standaloneSvg(svg))}`;
}
