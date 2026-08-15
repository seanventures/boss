"""APP-1 (#91) — framework-aware scaffolding service.

BOSS today can only *edit an existing repo* one small diff at a time, so a greenfield "build me an X"
starts from whatever seed happened to be there — the reason first-time output feels naive (spec
docs/planning/oss-readiness/09-first-time-output-quality.md §2.1, §3.1). This is the biggest lever:
before any agent role runs, stand up a **coherent, buildable baseline** — a real framework skeleton, a
package manifest with the web verification scripts, and a **design-system token layer** (one type
scale, one spacing rhythm, one accent, radius/shadow) that the design role (APP-2) then themes to the
domain.

**Deterministic and templated, not model-generated** — a scaffold must be consistent and buildable
every time, it costs no frontier tokens, and it makes the build/lint target (APP-3) knowable in
advance. The model's job is to *theme and populate* the scaffold, not reinvent boilerplate.

Default kind is **vite** (React + TS + Tailwind — lean, buildable) with **spa** as an alias; `nextjs`
is reserved for the App-Router variant (follow-on). shadcn-style primitives are copy-in MIT.
"""
from __future__ import annotations

from typing import Any

# Verification scripts APP-3 consumes; present in every scaffold's package.json.
_SCRIPTS = {
    "dev": "npx --package=vite vite",
    "build": "tsc --noEmit && npx --package=vite vite build",
    "typecheck": "tsc --noEmit",
    "lint": "eslint . --ext .ts,.tsx --max-warnings 0",
    "test:e2e": "npx --package=@playwright/test playwright test",
    "verify": "npm run typecheck && npm run build && npm run lint && npm run test:e2e",
    "preview": "npx --package=vite vite preview --host 0.0.0.0 --port ${PORT:-4173}",
}

# --- the design-system token layer (the load-bearing coherence primitive) ----
# One type scale, one spacing rhythm, one accent, radius + shadow — a single source of truth the
# APP-2 design role themes to the domain. Expressed as CSS custom properties + a Tailwind theme.
_GLOBALS_CSS = """@tailwind base;
@tailwind components;
@tailwind utilities;

/* Design-system baseline — one source of truth (APP-1). The design role themes these to the domain. */
:root {
  /* accent + neutrals (HSL so a theme can shift hue in one place) */
  --background: 0 0% 100%;
  --foreground: 222 47% 11%;
  --muted: 210 40% 96%;
  --muted-foreground: 215 16% 47%;
  --accent: 221 83% 53%;
  --accent-foreground: 0 0% 100%;
  --border: 214 32% 91%;
  --ring: 221 83% 53%;
  /* radius + shadow scale */
  --radius: 0.5rem;
  --shadow-sm: 0 1px 2px 0 hsl(222 47% 11% / 0.05);
  --shadow-md: 0 4px 12px -2px hsl(222 47% 11% / 0.10);
}
:root[data-theme="dark"] {
  --background: 222 47% 8%;
  --foreground: 210 40% 98%;
  --muted: 217 33% 17%;
  --muted-foreground: 215 20% 65%;
  --accent: 217 91% 60%;
  --border: 217 33% 20%;
  --ring: 217 91% 60%;
}

body { background: hsl(var(--background)); color: hsl(var(--foreground));
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  -webkit-font-smoothing: antialiased; }
"""

_TAILWIND_CONFIG = """import type { Config } from "tailwindcss";

// The type scale, spacing rhythm, and token bridge — coherence lives here (APP-1).
export default {
  darkMode: ["selector", '[data-theme="dark"]'],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        muted: { DEFAULT: "hsl(var(--muted))", foreground: "hsl(var(--muted-foreground))" },
        accent: { DEFAULT: "hsl(var(--accent))", foreground: "hsl(var(--accent-foreground))" },
        border: "hsl(var(--border))",
        ring: "hsl(var(--ring))",
      },
      borderRadius: { lg: "var(--radius)", md: "calc(var(--radius) - 2px)", sm: "calc(var(--radius) - 4px)" },
      boxShadow: { sm: "var(--shadow-sm)", md: "var(--shadow-md)" },
      // one type scale (major-third-ish), one spacing rhythm inherited from Tailwind's 4px base
      fontSize: {
        xs: ["0.75rem", "1rem"], sm: ["0.875rem", "1.25rem"], base: ["1rem", "1.5rem"],
        lg: ["1.125rem", "1.75rem"], xl: ["1.25rem", "1.75rem"], "2xl": ["1.5rem", "2rem"],
        "3xl": ["1.875rem", "2.25rem"], "4xl": ["2.25rem", "2.5rem"], "5xl": ["3rem", "1.1"],
      },
    },
  },
  plugins: [],
} satisfies Config;
"""

_CN_UTIL = """import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
"""

# shadcn-style primitives (copy-in, MIT) so the design role composes a kit, not hand-rolled divs.
_BUTTON_TSX = """import * as React from "react";
import { cn } from "../../lib/utils";

type Variant = "default" | "outline" | "ghost";
const styles: Record<Variant, string> = {
  default: "bg-accent text-accent-foreground hover:opacity-90 shadow-sm",
  outline: "border border-border bg-background hover:bg-muted",
  ghost: "hover:bg-muted",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
}
export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "default", ...props }, ref) => (
    <button ref={ref} className={cn(
      "inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-medium",
      "transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      "disabled:pointer-events-none disabled:opacity-50", styles[variant], className)} {...props} />
  ),
);
Button.displayName = "Button";
"""

_CARD_TSX = """import * as React from "react";
import { cn } from "../../lib/utils";

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-border bg-background shadow-sm", className)} {...props} />;
}
export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-6 pb-2", className)} {...props} />;
}
export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return <h3 className={cn("text-xl font-semibold tracking-tight", className)} {...props} />;
}
export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("p-6 pt-2 text-muted-foreground", className)} {...props} />;
}
"""

# A real (not blank) starting page that already USES the design system + component kit — a coherent
# hero→features rhythm the design role themes to the domain (placeholder copy flagged for APP-2).
_APP_TSX = """import { Button } from "./components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "./components/ui/card";

const FEATURES = [
  { title: "Coherent by default", body: "One type scale, one spacing rhythm, one accent — themed to your domain." },
  { title: "Buildable first pass", body: "Typechecks, lints, and builds before a single edit." },
  { title: "Design-system driven", body: "Compose the component kit; the tokens keep it consistent." },
];

export default function App() {
  return (
    <main className="mx-auto max-w-5xl px-6 py-20">
      <section className="text-center">
        {/* TODO(APP-2 design role): replace with on-domain, audience-specific copy — no placeholder */}
        <h1 className="text-balance text-5xl font-bold tracking-tight">Your product headline goes here</h1>
        <p className="mx-auto mt-4 max-w-xl text-lg text-muted-foreground">
          A one-line value proposition aimed at a specific audience.
        </p>
        <div className="mt-8 flex justify-center gap-3">
          <Button>Get started</Button>
          <Button variant="outline">Learn more</Button>
        </div>
      </section>
      <section className="mt-20 grid gap-6 sm:grid-cols-3">
        {FEATURES.map((f) => (
          <Card key={f.title}>
            <CardHeader><CardTitle>{f.title}</CardTitle></CardHeader>
            <CardContent>{f.body}</CardContent>
          </Card>
        ))}
      </section>
    </main>
  );
}
"""

_MAIN_TSX = """import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><App /></React.StrictMode>,
);
"""

_INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>__APP_TITLE__</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""

_TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2020", "useDefineForClassFields": true, "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext", "skipLibCheck": true, "moduleResolution": "bundler",
    "resolveJsonModule": true, "isolatedModules": true, "noEmit": true, "jsx": "react-jsx",
    "strict": true, "noUnusedLocals": true, "noUnusedParameters": true, "noFallthroughCasesInSwitch": true
  },
  "include": ["src"]
}
"""

_VITE_CONFIG = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({ plugins: [react()] });
"""

_POSTCSS = """export default { plugins: { tailwindcss: {}, autoprefixer: {} } };\n"""

_E2E_SPEC = """import { test, expect } from "@playwright/test";

// Minimal e2e: the primary route renders REAL content (not a blank body / error overlay).
test("home renders real content", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("h1")).toBeVisible();
  await expect(page.getByRole("button", { name: /get started/i })).toBeVisible();
});
"""

_PLAYWRIGHT_CONFIG = """import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  use: { baseURL: "http://localhost:4173" },
  webServer: { command: "npm run preview", url: "http://localhost:4173", reuseExistingServer: true },
});
"""

_ESLINTRC = """{
  "root": true,
  "env": { "browser": true, "es2020": true },
  "extends": ["eslint:recommended", "plugin:@typescript-eslint/recommended", "plugin:react-hooks/recommended"],
  "parser": "@typescript-eslint/parser",
  "plugins": ["react-refresh"],
  "ignorePatterns": ["dist", ".eslintrc.json"]
}
"""

_COMPONENTS_JSON = """{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "default", "rsc": false, "tsx": true,
  "tailwind": { "config": "tailwind.config.ts", "css": "src/index.css", "baseColor": "slate", "cssVariables": true },
  "aliases": { "components": "src/components", "utils": "src/lib/utils" }
}
"""

_GITIGNORE = "node_modules\ndist\n.env\n*.log\n"


def _scripts_in(package_json: str) -> list:
    """The scripts the emitted manifest really declares (fullstack adds start/migrate/seed)."""
    import json

    return sorted(json.loads(package_json).get("scripts", {}))


def _package_json(name: str, *, fullstack: bool = False) -> str:
    import json
    scripts = dict(_SCRIPTS)
    dependencies = {
        "react": "^18.3.1", "react-dom": "^18.3.1",
        "clsx": "^2.1.1", "tailwind-merge": "^2.5.2",
    }
    if fullstack:
        # `start` runs the real server, so "how do I run this in production" has one answer
        # that preview, deploy and the e2e suite all use.
        scripts.update({
            "dev": "concurrently -k \"npm:dev:api\" \"npm:dev:web\"",
            "dev:web": "npx --package=vite vite",
            "dev:api": "PORT=${API_PORT:-3001} node server/index.js",
            "start": "node server/index.js",
            "migrate": "node -e \"import('./server/db.js').then(m=>m.migrate())\"",
            "seed": "node -e \"import('./server/db.js').then(m=>{m.migrate();m.seed()})\"",
            "verify": "npm run typecheck && npm run build && npm run test:e2e",
        })
        dependencies.update({"express": "^4.19.2", "better-sqlite3": "^11.3.0"})
    pkg = {
        "name": name, "private": True, "version": "0.1.0", "type": "module",
        "engines": {"node": ">=20"},
        "scripts": scripts,
        "dependencies": dependencies,
        "devDependencies": {
            "@types/react": "^18.3.3", "@types/react-dom": "^18.3.0",
            "@typescript-eslint/eslint-plugin": "^7.13.1", "@typescript-eslint/parser": "^7.13.1",
            "@vitejs/plugin-react": "^4.3.1", "autoprefixer": "^10.4.19", "eslint": "^8.57.0",
            "eslint-plugin-react": "^7.34.3",
            "eslint-plugin-react-hooks": "^4.6.2", "eslint-plugin-react-refresh": "^0.4.7",
            "postcss": "^8.4.38", "tailwindcss": "^3.4.4", "typescript": "^5.4.5",
            "vite": "^5.3.1", "@playwright/test": "^1.45.0",
            "concurrently": "^9.0.1",
        },
    }
    return json.dumps(pkg, indent=2) + "\n"


_ALIASES = {"vite": "vite", "spa": "vite", "react": "vite",
            "fullstack": "fullstack", "app": "fullstack", "webapp": "fullstack"}


# =============================================================================
# Full-stack scaffold: a frontend WITH a server behind it.
#
# The vite scaffold below is frontend-only, and it was the seed for every PRD-driven web app
# build. So the pipeline produced, deterministically, apps that could not persist anything: an
# expense-approval tool whose approvals vanished on reload, dashboards with no data source.
# This was read as the coding agent building "naive" apps. It was not -- nothing in the seed
# gave it a backend, and the acceptance gate (a 200 from `/`) could not tell the difference.
#
# So the default seed for an app that stores anything is this one: an Express server with real
# SQLite persistence, schema migrations, seed data, and one worked CRUD slice end to end --
# frontend calling /api, server reading and writing the database. The agent extends a working
# vertical slice instead of inventing an architecture.
#
# Plain JavaScript on the server on purpose: no build step, so `node server/index.js` is the
# whole start command and there is one less stage between a working repo and a running app.
# =============================================================================

_SERVER_INDEX = r"""import express from "express";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { db, migrate, seed } from "./db.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// HOST/PORT from the environment: this runs in a container behind a proxy, where binding
// 127.0.0.1 or a fixed port makes the app unreachable while looking perfectly healthy.
const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || "0.0.0.0";

migrate();
if (process.env.SEED !== "false") seed();

const app = express();
app.use(express.json());

// A health endpoint that actually proves the app works: it reads the database. A handler that
// returns a hard-coded 200 reports healthy while every real request fails.
app.get("/api/health", (_req, res) => {
  try {
    const row = db.prepare("SELECT count(*) AS n FROM items").get();
    res.json({ ok: true, items: row.n });
  } catch (error) {
    res.status(500).json({ ok: false, error: String(error) });
  }
});

// --- One complete CRUD slice. Rename `items` to the domain and extend; keep the shape. ---
app.get("/api/items", (_req, res) => {
  res.json(db.prepare("SELECT * FROM items ORDER BY created_at DESC").all());
});

app.post("/api/items", (req, res) => {
  const title = String(req.body?.title || "").trim();
  if (!title) return res.status(400).json({ error: "title is required" });
  const status = String(req.body?.status || "pending");
  const info = db
    .prepare("INSERT INTO items (title, status, created_at) VALUES (?, ?, datetime('now'))")
    .run(title, status);
  res.status(201).json(db.prepare("SELECT * FROM items WHERE id = ?").get(info.lastInsertRowid));
});

app.patch("/api/items/:id", (req, res) => {
  const existing = db.prepare("SELECT * FROM items WHERE id = ?").get(req.params.id);
  if (!existing) return res.status(404).json({ error: "not found" });
  const status = String(req.body?.status || existing.status);
  db.prepare("UPDATE items SET status = ? WHERE id = ?").run(status, req.params.id);
  res.json(db.prepare("SELECT * FROM items WHERE id = ?").get(req.params.id));
});

app.delete("/api/items/:id", (req, res) => {
  db.prepare("DELETE FROM items WHERE id = ?").run(req.params.id);
  res.status(204).end();
});

// The built frontend, served by the same process, so one container is the whole app.
const dist = path.join(__dirname, "..", "dist");
app.use(express.static(dist));
// SPA fallback -- but never for /api, or a missing route would return HTML with status 200
// and the client would report a JSON parse error instead of a 404.
app.get(/^(?!\/api).*/, (_req, res) => res.sendFile(path.join(dist, "index.html")));

app.listen(PORT, HOST, () => console.log(`listening on http://${HOST}:${PORT}`));
"""

_SERVER_DB = """import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// DATA_DIR is a mount that deployments do not replace, so data survives a release. Anything
// written beside the source is destroyed by the next deploy -- correct for code, fatal for data.
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, "..", "data");
fs.mkdirSync(DATA_DIR, { recursive: true });

export const db = new Database(path.join(DATA_DIR, "app.db"));
db.pragma("journal_mode = WAL");

/** Apply every migration not yet applied, in order, recording each. */
export function migrate() {
  db.exec(
    "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
  );
  const dir = path.join(__dirname, "migrations");
  const applied = new Set(db.prepare("SELECT name FROM _migrations").all().map((r) => r.name));
  for (const name of fs.readdirSync(dir).filter((f) => f.endsWith(".sql")).sort()) {
    if (applied.has(name)) continue;
    db.exec(fs.readFileSync(path.join(dir, name), "utf8"));
    db.prepare("INSERT INTO _migrations (name, applied_at) VALUES (?, datetime('now'))").run(name);
    console.log(`migrated ${name}`);
  }
}

/** Deterministic seed data. Idempotent: seeding twice must not duplicate rows. */
export function seed() {
  const { n } = db.prepare("SELECT count(*) AS n FROM items").get();
  if (n > 0) return;
  const insert = db.prepare(
    "INSERT INTO items (title, status, created_at) VALUES (?, ?, datetime('now'))"
  );
  insert.run("First example item", "pending");
  insert.run("Second example item", "approved");
}
"""

_MIGRATION_001 = """-- Initial schema. Rename `items` to the domain object and add columns as needed;
-- add further migrations as NEW files (002_*.sql) rather than editing this one, so an
-- already-deployed database can move forward.
CREATE TABLE IF NOT EXISTS items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT    NOT NULL,
  status     TEXT    NOT NULL DEFAULT 'pending',
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items (status);
"""

_VITE_CONFIG_FULLSTACK = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the API runs as a separate process; proxy /api to it so the frontend uses the SAME
// relative paths it will use in production, where one server serves both.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": { target: `http://localhost:${process.env.API_PORT || 3001}`, changeOrigin: true } },
  },
});
"""

# The frontend talks to the server. This is the part that was always missing: a component that
# loads from an API, writes to it, and shows the result -- so "wire the UI to the backend" is a
# pattern to extend rather than a task to invent.
_APP_TSX_FULLSTACK = """import { useEffect, useState } from "react";
import { Button } from "./components/ui/button";
import { Card } from "./components/ui/card";

type Item = { id: number; title: string; status: string; created_at: string };

export default function App() {
  const [items, setItems] = useState<Item[]>([]);
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const res = await fetch("/api/items");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setItems(await res.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function add() {
    if (!title.trim()) return;
    await fetch("/api/items", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title }),
    });
    setTitle("");
    load();
  }

  async function setStatus(id: number, status: string) {
    await fetch(`/api/items/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ status }),
    });
    load();
  }

  return (
    <main className="mx-auto max-w-2xl p-8">
      <h1 className="text-3xl font-semibold tracking-tight">Items</h1>
      <p className="mt-2 text-muted-foreground">
        Stored server-side in SQLite — reload the page and it is still here.
      </p>

      {error && <p role="alert" className="mt-4 text-red-600">Could not reach the API: {error}</p>}

      <div className="mt-6 flex gap-2">
        <input
          aria-label="New item title"
          className="flex-1 rounded-md border px-3 py-2"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Add an item…"
        />
        <Button onClick={add}>Get started</Button>
      </div>

      <ul className="mt-6 space-y-2">
        {loading && <li className="text-muted-foreground">Loading…</li>}
        {!loading && items.length === 0 && <li className="text-muted-foreground">Nothing yet.</li>}
        {items.map((item) => (
          <li key={item.id}>
            <Card className="flex items-center justify-between p-4">
              <span data-testid="item-title">{item.title}</span>
              <span className="flex items-center gap-3">
                <span className="text-sm text-muted-foreground">{item.status}</span>
                <Button
                  onClick={() => setStatus(item.id, item.status === "approved" ? "pending" : "approved")}
                >
                  {item.status === "approved" ? "Unapprove" : "Approve"}
                </Button>
              </span>
            </Card>
          </li>
        ))}
      </ul>
    </main>
  );
}
"""

# The gate that would have caught both failed builds: it asserts the data SURVIVES a reload,
# which a static mockup and an in-memory SPA both fail.
_E2E_SPEC_FULLSTACK = """import { test, expect } from "@playwright/test";

test("the API is actually running", async ({ request }) => {
  const res = await request.get("/api/health");
  expect(res.ok()).toBeTruthy();
  expect((await res.json()).ok).toBe(true);
});

test("data written through the UI survives a reload", async ({ page }) => {
  const title = `e2e item ${Date.now()}`;

  await page.goto("/");
  await page.getByLabel("New item title").fill(title);
  await page.getByRole("button", { name: /get started/i }).click();
  await expect(page.getByText(title)).toBeVisible();

  // The point of the test. An in-memory app passes everything above and fails here.
  await page.reload();
  await expect(page.getByText(title)).toBeVisible();
});

test("the frontend reads its list from the server, not from itself", async ({ page }) => {
  const seen: string[] = [];
  page.on("request", (r) => {
    if (r.url().includes("/api/")) seen.push(r.url());
  });
  await page.goto("/");
  await expect(page.locator("h1")).toBeVisible();
  expect(seen.length).toBeGreaterThan(0);
});
"""

_PLAYWRIGHT_CONFIG_FULLSTACK = """import { defineConfig } from "@playwright/test";

// Build, then run the REAL server -- the same command production uses. Testing `vite preview`
// would exercise a static file server and prove nothing about the backend.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:4173" },
  webServer: {
    command: "npm run build && PORT=4173 DATA_DIR=./.e2e-data npm start",
    url: "http://127.0.0.1:4173/api/health",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
"""

_README_FULLSTACK = """# __APP_NAME__

Full-stack: React + Vite frontend, Express + SQLite backend, one process in production.

## One-command startup

    npm install
    npm run dev          # frontend on :5173, API on :3001

## Production

    npm run build        # builds the frontend into dist/
    npm start            # serves dist/ AND /api from one process on $PORT (default 3000)

`HOST` and `PORT` come from the environment so this runs behind a proxy or in a container.
`DATA_DIR` (default `./data`) holds the SQLite file and is intended to be a mount that
deployments do not replace, so data survives a release.

## Database

Migrations are `.sql` files in `server/migrations/`, applied in filename order at startup and
recorded in `_migrations`. Add a new numbered file rather than editing an applied one. Seed data
is idempotent (`server/db.js`).

## Verification

    npm run verify       # typecheck + build + end-to-end tests

The e2e suite asserts the API responds AND that data written through the UI survives a page
reload — the check that distinguishes a working app from a convincing mockup.
"""


def _fullstack_files(safe: str, files: dict) -> dict:
    """Layer the server, database and wired frontend on top of the frontend scaffold."""
    files = dict(files)
    files.update({
        "server/index.js": _SERVER_INDEX,
        "server/db.js": _SERVER_DB,
        "server/migrations/001_init.sql": _MIGRATION_001,
        "vite.config.ts": _VITE_CONFIG_FULLSTACK,
        "src/App.tsx": _APP_TSX_FULLSTACK,
        "e2e/home.spec.ts": _E2E_SPEC_FULLSTACK,
        "playwright.config.ts": _PLAYWRIGHT_CONFIG_FULLSTACK,
        "README.md": _README_FULLSTACK.replace("__APP_NAME__", safe),
        "package.json": _package_json(safe, fullstack=True),
    })
    return files


def scaffold_project(name: str = "app", kind: str = "vite") -> dict[str, Any]:
    """Return a deterministic, buildable scaffold: ``{files: {path: content}, manifest}``. Same
    (name, kind) in → identical tree out (no model tokens). ``kind`` accepts vite/spa/react."""
    resolved = _ALIASES.get((kind or "vite").lower())
    if resolved is None:
        raise ValueError(f"unsupported scaffold kind {kind!r}; supported: {sorted(_ALIASES)} (nextjs: follow-on)")
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in (name or "app").lower()) or "app"
    files = {
        "package.json": _package_json(safe),
        "tsconfig.json": _TSCONFIG,
        "vite.config.ts": _VITE_CONFIG,
        "tailwind.config.ts": _TAILWIND_CONFIG,
        "postcss.config.js": _POSTCSS,
        "components.json": _COMPONENTS_JSON,
        ".eslintrc.json": _ESLINTRC,
        ".gitignore": _GITIGNORE,
        "playwright.config.ts": _PLAYWRIGHT_CONFIG,
        "index.html": _INDEX_HTML.replace("__APP_TITLE__", safe),
        "src/main.tsx": _MAIN_TSX,
        "src/App.tsx": _APP_TSX,
        "src/index.css": _GLOBALS_CSS,
        "src/lib/utils.ts": _CN_UTIL,
        "src/components/ui/button.tsx": _BUTTON_TSX,
        "src/components/ui/card.tsx": _CARD_TSX,
        "e2e/home.spec.ts": _E2E_SPEC,
    }
    if resolved == "fullstack":
        files = _fullstack_files(safe, files)
    manifest = {
        "name": safe, "kind": resolved,
        "framework": "vite+react+ts+express+sqlite" if resolved == "fullstack" else "vite+react+ts",
        "design_system": "tailwind+shadcn-style tokens (type scale, spacing, accent, radius, shadow)",
        "scripts": _scripts_in(files["package.json"]),
        "verification": {"build": _SCRIPTS["build"], "typecheck": _SCRIPTS["typecheck"],
                         "lint": _SCRIPTS["lint"], "test:e2e": _SCRIPTS["test:e2e"]},
        "file_count": len(files),
        "design_tokens_file": "src/index.css",
        "component_kit": ["src/components/ui/button.tsx", "src/components/ui/card.tsx"],
    }
    return {"files": files, "manifest": manifest}
