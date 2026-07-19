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
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "typecheck": "tsc --noEmit",
    "lint": "eslint . --ext .ts,.tsx --max-warnings 0",
    "test:e2e": "playwright test",
    "preview": "vite preview --host 0.0.0.0 --port ${PORT:-4173}",
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


def _package_json(name: str) -> str:
    import json
    pkg = {
        "name": name, "private": True, "version": "0.1.0", "type": "module",
        "scripts": _SCRIPTS,
        "dependencies": {
            "react": "^18.3.1", "react-dom": "^18.3.1",
            "clsx": "^2.1.1", "tailwind-merge": "^2.5.2",
        },
        "devDependencies": {
            "@types/react": "^18.3.3", "@types/react-dom": "^18.3.0",
            "@typescript-eslint/eslint-plugin": "^7.13.1", "@typescript-eslint/parser": "^7.13.1",
            "@vitejs/plugin-react": "^4.3.1", "autoprefixer": "^10.4.19", "eslint": "^8.57.0",
            "eslint-plugin-react-hooks": "^4.6.2", "eslint-plugin-react-refresh": "^0.4.7",
            "postcss": "^8.4.38", "tailwindcss": "^3.4.4", "typescript": "^5.4.5",
            "vite": "^5.3.1", "@playwright/test": "^1.45.0",
        },
    }
    return json.dumps(pkg, indent=2) + "\n"


_ALIASES = {"vite": "vite", "spa": "vite", "react": "vite"}


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
    manifest = {
        "name": safe, "kind": resolved, "framework": "vite+react+ts",
        "design_system": "tailwind+shadcn-style tokens (type scale, spacing, accent, radius, shadow)",
        "scripts": list(_SCRIPTS.keys()),
        "verification": {"build": _SCRIPTS["build"], "typecheck": _SCRIPTS["typecheck"],
                         "lint": _SCRIPTS["lint"], "test:e2e": _SCRIPTS["test:e2e"]},
        "file_count": len(files),
        "design_tokens_file": "src/index.css",
        "component_kit": ["src/components/ui/button.tsx", "src/components/ui/card.tsx"],
    }
    return {"files": files, "manifest": manifest}
