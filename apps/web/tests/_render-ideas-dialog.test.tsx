/** Not a test — a rendering harness, so every card in "Start from an idea" can be LOOKED AT.
 *
 *  Writes two pages: the dialog as it opens (`<out>.html`) and the dialog unclamped so every card
 *  is on one page (`<out>-all.html`). Both link the console's own stylesheets from this checkout.
 *  Screenshot them with headless Chrome at 1440 and, inside a 390 px iframe, at phone width.
 *
 *  Run: IDEAS_DIALOG_OUT=/tmp/ideas npx vitest run tests/_render-ideas-dialog.test.tsx
 */
import * as React from "react";
import { describe, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { readdirSync, writeFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { join, relative } from "node:path";

describe("ideas dialog preview", () => {
  // Opt-in: an ordinary `vitest run` should not drop files on somebody's disk.
  it.runIf(!!process.env.IDEAS_DIALOG_OUT)("writes the dialog to html files", async () => {
    (globalThis as Record<string, unknown>).React = React;
    const { IdeasDialog } = await import("../components/v2/templates/ideas-dialog");
    const { ideasFrom } = await import("../components/v2/templates/catalogue");
    const { parseCard } = await import("../lib/templates");

    const starter = (key: string, name: string, summary: string) =>
      parseCard({ id: `tpl_${key}`, template_key: key, name, summary, status: "active", kind: "idea",
                  stacks: ["node"], labels: ["idea"], adds: [], egress: [], secrets: [] });
    const ideas = ideasFrom([
      starter("banking-starter", "Banking starter", "Accounts with a ledger-derived balance, deposits, withdrawals and transfers"),
      starter("catalog-cart", "Product catalog and cart", "Product list with search and pages, product detail, a cart with server-side totals"),
      starter("hello", "Hello endpoint", "Adds GET /hello answering a JSON greeting"),
    ]);
    const html = renderToStaticMarkup(<IdeasDialog ideas={ideas} onPick={() => {}} onClose={() => {}} />);

    // Every stylesheet the console loads, from this checkout.
    const app = fileURLToPath(new URL("../app/", import.meta.url));
    const sheets: string[] = [];
    const walk = (dir: string) => {
      for (const e of readdirSync(dir, { withFileTypes: true })) {
        if (e.isDirectory()) walk(join(dir, e.name));
        else if (e.name.endsWith(".css")) sheets.push(join(dir, e.name));
      }
    };
    walk(app);
    walk(fileURLToPath(new URL("../components/", import.meta.url)));
    const globals = (s: string) => relative(app, s) === "globals.css";
    const links = [...sheets.filter(globals), ...sheets.filter((s) => !globals(s))]
      .map((s) => `<link rel="stylesheet" href="${pathToFileURL(s).href}">`).join("\n");
    const page = (extra: string) => `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Sora:wght@600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
${links}
<style>body{margin:0;background:var(--bg,#0B1220);color:var(--ink)} ${extra}</style></head>
<body>${html}</body></html>`;
    const out = process.env.IDEAS_DIALOG_OUT as string;
    writeFileSync(`${out}.html`, page(""));
    writeFileSync(`${out}-all.html`, page(`
      .np-fs.np-ideas-fs { position: static; overflow: visible; }
      .np-ideas-card { max-height: none !important; height: auto !important; }
      .np-ideas-scroll { overflow: visible !important; }`));
  });
});
