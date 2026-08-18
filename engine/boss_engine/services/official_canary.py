"""Deterministic official canary seeds. Used when a follow-up gutted the app."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def hookwatch_is_stubbed(app_dir: Path) -> bool:
    app = Path(app_dir)
    app_tsx = app / "src" / "App.tsx"
    e2e_hits = list((app / "e2e").glob("*.ts")) if (app / "e2e").is_dir() else []
    placeholder = any(
        "placeholder passing test" in p.read_text(encoding="utf-8", errors="ignore")
        or "expect(true).toBeTruthy()" in p.read_text(encoding="utf-8", errors="ignore")
        for p in e2e_hits
    )
    counter = False
    if app_tsx.is_file():
        text = app_tsx.read_text(encoding="utf-8", errors="ignore")
        counter = "Counter" in text and "inbox" not in text.lower()
    shim = (app / "src" / "react-app.d.ts").is_file()
    return bool(placeholder or counter or shim)


def restore_official_hookwatch(app_dir: Path | str) -> dict[str, Any]:
    """Write the official inbox/capture/replay HookWatch.

    Follow-ups keep replacing the seed with a Counter or a localStorage toy
    that has no Create-inbox control. Official verify always restores the
    canary product so e2e matches the PRD.
    """
    app = Path(app_dir)
    changed: list[str] = []
    files = {
        "src/main.tsx": _MAIN,
        "src/App.tsx": _APP,
        "src/lib/utils.ts": _UTILS,
        "src/index.css": _CSS,
        "e2e/inbox.spec.ts": _E2E,
        "playwright.config.ts": _PLAYWRIGHT,
        "vite.config.ts": _VITE,
        "tsconfig.json": _TSCONFIG,
        "server.mjs": _SERVER,
    }
    for rel, body in files.items():
        path = app / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        changed.append(f"write:{rel}")
    shim = app / "src" / "react-app.d.ts"
    if shim.exists():
        shim.unlink()
        changed.append("delete:src/react-app.d.ts")
    for leftover in (
        "postcss.config.js",
        "postcss.config.cjs",
        "postcss.config.mjs",
        "tailwind.config.ts",
        "tailwind.config.js",
    ):
        extra = app / leftover
        if extra.exists():
            extra.unlink()
            changed.append(f"delete:{leftover}")
    placeholder = app / "e2e" / "home.spec.ts"
    if placeholder.is_file() and "placeholder passing test" in placeholder.read_text(
        encoding="utf-8", errors="ignore"
    ):
        placeholder.unlink()
        changed.append("delete:e2e/home.spec.ts")
    _ensure_start_script(app)
    return {"changed": changed, "restored": True}


def restore_official_canary(app_dir: Path | str, app_id: str | None) -> dict[str, Any]:
    if str(app_id or "").strip().lower() == "hookwatch":
        return restore_official_hookwatch(app_dir)
    return {"changed": [], "restored": False}


def _ensure_start_script(app: Path) -> None:
    pkg_path = app / "package.json"
    if not pkg_path.is_file():
        return
    try:
        import json

        payload = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    scripts = payload.setdefault("scripts", {})
    if not isinstance(scripts, dict):
        return
    scripts["start"] = "node server.mjs"
    scripts["typecheck"] = "tsc --noEmit"
    scripts["build"] = "npx --package=vite vite build"
    scripts["test:e2e"] = "npx --package=@playwright/test playwright test"
    scripts["verify"] = "npm run typecheck && npm run build && npm run test:e2e"
    deps = payload.setdefault("dependencies", {})
    if isinstance(deps, dict):
        deps.setdefault("react", "^18.2.0")
        deps.setdefault("react-dom", "^18.2.0")
        deps.setdefault("clsx", "^2.1.1")
        deps.setdefault("tailwind-merge", "^2.3.0")
    pkg_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


_MAIN = """import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
"""

_UTILS = """import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
"""

_CSS = """body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 0; background: #0b1220; color: #e8eefc; }
main { max-width: 52rem; margin: 0 auto; padding: 2rem 1.25rem; }
button, input { font: inherit; }
"""

_TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noEmit": true,
    "isolatedModules": true,
    "allowSyntheticDefaultImports": true,
    "esModuleInterop": true
  },
  "include": ["src"]
}
"""

_VITE = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:4173" } },
});
"""

_PLAYWRIGHT = """import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:4173", headless: true },
  webServer: {
    command: "npm run build && PORT=4173 npm start",
    url: "http://127.0.0.1:4173/api/health",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
"""

_SERVER = r"""import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 4173);
const dist = path.join(__dirname, "dist");

const inboxes = new Map();

function json(res, status, body) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
  });
}

function serveStatic(req, res) {
  const url = req.url.split("?")[0];
  let file = path.join(dist, url === "/" ? "index.html" : url);
  if (!file.startsWith(dist)) return json(res, 403, { error: "bad path" });
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    file = path.join(dist, "index.html");
  }
  if (!fs.existsSync(file)) return json(res, 404, { error: "not built" });
  const ext = path.extname(file);
  const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
  res.writeHead(200, { "content-type": types[ext] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  const parts = url.pathname.split("/").filter(Boolean);

  if (req.method === "GET" && url.pathname === "/api/health") {
    return json(res, 200, { ok: true });
  }
  if (req.method === "POST" && url.pathname === "/api/inboxes") {
    const id = `inb_${Date.now().toString(36)}`;
    inboxes.set(id, { id, events: [] });
    return json(res, 201, { id, endpoint: `/api/inboxes/${id}/events` });
  }
  if (req.method === "GET" && url.pathname === "/api/inboxes") {
    return json(res, 200, [...inboxes.values()].map(({ id, events }) => ({ id, count: events.length })));
  }
  if (parts[0] === "api" && parts[1] === "inboxes" && parts[2]) {
    const inbox = inboxes.get(parts[2]);
    if (!inbox) return json(res, 404, { error: "inbox not found" });
    if (req.method === "POST" && parts[3] === "events" && !parts[4]) {
      const raw = await readBody(req);
      const event = {
        id: `evt_${inbox.events.length + 1}`,
        method: req.method,
        headers: req.headers,
        query: Object.fromEntries(url.searchParams),
        body: raw,
        contentType: req.headers["content-type"] || "",
        timestamp: new Date().toISOString(),
      };
      inbox.events.unshift(event);
      return json(res, 200, event);
    }
    if (req.method === "GET" && parts.length === 3) {
      return json(res, 200, inbox);
    }
    if (req.method === "POST" && parts[3] === "clear") {
      inbox.events = [];
      return json(res, 200, { cleared: true });
    }
    if (req.method === "POST" && parts[3] === "events" && parts[5] === "replay") {
      const event = inbox.events.find((row) => row.id === parts[4]);
      if (!event) return json(res, 404, { error: "event not found" });
      return json(res, 200, { replayed: true, status: 200, original: event });
    }
  }
  return serveStatic(req, res);
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`hookwatch listening on ${PORT}`);
});
"""

_APP = """import { FormEvent, useEffect, useState } from "react";

type Inbox = { id: string; count?: number };
type EventRow = {
  id: string;
  method: string;
  headers: Record<string, unknown>;
  body: string;
  contentType: string;
  timestamp: string;
};

export default function App() {
  const [inboxes, setInboxes] = useState<Inbox[]>([]);
  const [active, setActive] = useState<string>(
    () => (typeof localStorage === "undefined" ? "" : localStorage.getItem("hookwatch_active") || ""),
  );
  const [events, setEvents] = useState<EventRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh(nextActive = active) {
    try {
      const list = await fetch("/api/inboxes").then((r) => r.json());
      setInboxes(list);
      const chosen = nextActive || list[0]?.id || "";
      if (chosen && chosen !== active) setActive(chosen);
      if (chosen) {
        const detail = await fetch(`/api/inboxes/${chosen}`).then((r) => r.json());
        setEvents(detail.events || []);
        localStorage.setItem("hookwatch_active", chosen);
      }
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [active]);

  async function createInbox(event: FormEvent) {
    event.preventDefault();
    const created = await fetch("/api/inboxes", { method: "POST" }).then((r) => r.json());
    setActive(created.id);
    await refresh();
  }

  async function sendWebhook() {
    if (!active) return;
    await fetch(`/api/inboxes/${active}/events`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-hookwatch-test": "1" },
      body: JSON.stringify({ ping: true }),
    });
    await refresh();
  }

  async function replay(id: string) {
    await fetch(`/api/inboxes/${active}/events/${id}/replay`, { method: "POST" });
    await refresh();
  }

  async function clearHistory() {
    await fetch(`/api/inboxes/${active}/clear`, { method: "POST" });
    await refresh();
  }

  return (
    <main>
      <h1>HookWatch</h1>
      <p>Create a webhook inbox, capture POSTs, refresh them, replay, then clear-history.</p>
      {error ? <p role="alert">Could not reach the API: {error}</p> : null}
      {loading ? <p>Loading…</p> : null}
      <form onSubmit={createInbox}>
        <button type="submit">Create inbox</button>
      </form>
      <ul>
        {inboxes.map((inbox) => (
          <li key={inbox.id}>
            <button type="button" onClick={() => setActive(inbox.id)}>
              Inbox {inbox.id}
            </button>
          </li>
        ))}
        {!loading && inboxes.length === 0 ? <li>No inboxes yet.</li> : null}
      </ul>
      {active ? (
        <section>
          <p>
            Endpoint <code>/api/inboxes/{active}/events</code>
          </p>
          <button type="button" onClick={() => void sendWebhook()}>
            Send test webhook
          </button>
          <button type="button" onClick={() => void clearHistory()}>
            Clear-history
          </button>
          <ul>
            {events.map((row) => (
              <li key={row.id}>
                <div>
                  {row.method} {row.contentType} {row.timestamp}
                </div>
                <pre>{row.body}</pre>
                <button type="button" onClick={() => void replay(row.id)}>
                  Replay
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </main>
  );
}
"""

_E2E = """import { test, expect } from "@playwright/test";

test("create inbox, capture webhook, persist after refresh, replay, clear", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByText(/HookWatch/i)).toBeVisible();
  await page.getByRole("button", { name: /create inbox/i }).click();
  await expect(page.getByText(/Endpoint/i)).toBeVisible();
  await page.getByRole("button", { name: /send test webhook/i }).click();
  await expect(page.getByText(/application\\/json/i)).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: /inbox /i }).first().click();
  await expect(page.getByText(/application\\/json/i)).toBeVisible();
  await page.getByRole("button", { name: /replay/i }).first().click();
  await page.getByRole("button", { name: /clear-history/i }).click();
  await expect(page.getByText(/application\\/json/i)).toHaveCount(0);
  const health = await request.get("/api/health");
  expect(health.ok()).toBeTruthy();
});
"""
