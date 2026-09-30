"""Mission Control (community) — a read-only web view of the engine.

This is the "simple Mission Control" of the community tier: one self-contained
HTML page (no build step, no external assets, no new Python deps) served by the
MCP HTTP transport at GET /. Everything it shows comes from the engine's READ
tools, called through the same JSON-RPC /mcp endpoint external agents use —
one source of truth, no parallel API surface. Write tools are never exposed
here, regardless of the server's read_only flag.

The commercial console (boss-web) is a different product: multi-user,
approvals, live sockets. This page is deliberately a snapshot viewer.
"""

# Read tools rendered as sections, in display order. Each entry: (tool name,
# section title, result key holding the row list — or None for a plain dict).
SECTIONS = [
    ("project_status", "Project status", None),
    ("list_running_work", "Running work", "items"),
    ("list_code_tasks", "Code tasks", "items"),
    ("list_previews", "Previews", "items"),
    ("list_discovery", "Discovery", "items"),
    ("list_meetings", "Meetings", "items"),
]

MISSION_CONTROL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BOSS Mission Control</title>
<style>
  :root {
    --bg: #0e1116; --panel: #161b24; --line: #232b38; --text: #d7dee8;
    --dim: #8b96a5; --accent: #5aa9e6; --ok: #4caf7d; --warn: #e0a63f; --err: #e0604f;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  header { display: flex; align-items: baseline; gap: 12px; padding: 14px 20px;
           border-bottom: 1px solid var(--line); }
  header h1 { font-size: 15px; margin: 0; letter-spacing: .04em; }
  header .sub { color: var(--dim); font-size: 12px; }
  header .right { margin-left: auto; color: var(--dim); font-size: 12px; }
  main { padding: 16px 20px 40px; max-width: 1200px; margin: 0 auto; }
  section { background: var(--panel); border: 1px solid var(--line);
            border-radius: 6px; margin-bottom: 16px; overflow: hidden; }
  section > h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
                 color: var(--dim); margin: 0; padding: 10px 14px;
                 border-bottom: 1px solid var(--line); }
  section .body { padding: 10px 14px; overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 4px 10px 4px 0; vertical-align: top;
           border-bottom: 1px solid var(--line); font-size: 13px; }
  th { color: var(--dim); font-weight: normal; }
  tr:last-child td { border-bottom: none; }
  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; }
  .kv dt { color: var(--dim); } .kv dd { margin: 0; word-break: break-word; }
  .pill { display: inline-block; padding: 0 8px; border-radius: 9px;
          font-size: 12px; border: 1px solid var(--line); }
  .s-ok { color: var(--ok); } .s-warn { color: var(--warn); } .s-err { color: var(--err); }
  .empty { color: var(--dim); font-style: italic; }
  .error { color: var(--err); white-space: pre-wrap; }
  footer { color: var(--dim); font-size: 12px; text-align: center; padding: 8px; }
  a { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>BOSS · Mission Control</h1>
  <span class="sub">community · read-only</span>
  <span class="right" id="meta">loading…</span>
</header>
<main id="sections"></main>
<footer>Served by the boss-engine MCP HTTP transport · data via read tools over /mcp · refreshes every 15s</footer>
<script>
"use strict";
const SECTIONS = __SECTIONS__;
let rpcId = 0;
let authToken = "";
let authPrompted = false;

async function rpc(method, params) {
  const requestBody = JSON.stringify({jsonrpc: "2.0", id: ++rpcId, method, params});
  async function send() {
    const headers = {"Content-Type": "application/json"};
    if (authToken) headers.Authorization = "Bearer " + authToken;
    return fetch("mcp", {method: "POST", headers, body: requestBody});
  }
  let res = await send();
  if (res.status === 401 && !authPrompted) {
    authPrompted = true;
    authToken = window.prompt("BOSS MCP token for this browser session") || "";
    if (authToken) res = await send();
  }
  if (!res.ok) throw new Error("HTTP " + res.status);
  const body = await res.json();
  if (body.error) throw new Error(body.error.message || "rpc error");
  return body.result;
}

async function callTool(name) {
  const result = await rpc("tools/call", {name, arguments: {}});
  const text = ((result.content || [])[0] || {}).text || "{}";
  const payload = JSON.parse(text);
  if (result.isError) throw new Error(payload.error || "tool error");
  return payload;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function statusClass(value) {
  const v = String(value).toLowerCase();
  if (/(fail|error|dead|blocked|cancel)/.test(v)) return "s-err";
  if (/(run|progress|pending|waiting|open)/.test(v)) return "s-warn";
  if (/(done|complete|passed|merged|ok|true|healthy)/.test(v)) return "s-ok";
  return "";
}

function renderRows(container, rows) {
  if (!rows.length) { container.appendChild(el("div", "empty", "nothing here")); return; }
  const cols = [];
  for (const row of rows.slice(0, 25))
    for (const key of Object.keys(row))
      if (!cols.includes(key) && cols.length < 8) cols.push(key);
  const table = el("table"), head = el("tr");
  cols.forEach(c => head.appendChild(el("th", "", c)));
  table.appendChild(head);
  for (const row of rows.slice(0, 25)) {
    const tr = el("tr");
    for (const c of cols) {
      let v = row[c];
      if (v !== null && typeof v === "object") v = JSON.stringify(v);
      const td = el("td", /status|state|phase|result/.test(c) ? statusClass(v) : "",
                    v === undefined || v === null ? "" : String(v).slice(0, 160));
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  container.appendChild(table);
}

function renderDict(container, payload) {
  const dl = el("dl", "kv");
  for (const [key, value] of Object.entries(payload)) {
    if (key === "ok") continue;
    dl.appendChild(el("dt", "", key));
    const dd = el("dd", statusClass(value));
    dd.textContent = value !== null && typeof value === "object"
      ? JSON.stringify(value) : String(value);
    dl.appendChild(dd);
  }
  container.appendChild(dl);
}

function firstListValue(payload, preferred) {
  if (preferred && Array.isArray(payload[preferred])) return payload[preferred];
  for (const value of Object.values(payload)) if (Array.isArray(value)) return value;
  return null;
}

async function refresh() {
  document.getElementById("meta").textContent = new Date().toLocaleTimeString();
  for (const [tool, title, listKey] of SECTIONS) {
    const section = document.getElementById("sec-" + tool);
    const body = section.querySelector(".body");
    try {
      const payload = await callTool(tool);
      body.textContent = "";
      const rows = firstListValue(payload, listKey);
      if (rows) renderRows(body, rows); else renderDict(body, payload);
    } catch (err) {
      body.textContent = "";
      body.appendChild(el("div", "error", String(err.message || err)));
    }
  }
}

const main = document.getElementById("sections");
for (const [tool, title] of SECTIONS) {
  const section = el("section"); section.id = "sec-" + tool;
  section.appendChild(el("h2", "", title));
  const body = el("div", "body"); body.appendChild(el("div", "empty", "loading…"));
  section.appendChild(body);
  main.appendChild(section);
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


def mission_control_html() -> str:
    """The Mission Control page with its section manifest injected."""
    import json

    sections = [[tool, title, key] for tool, title, key in SECTIONS]
    return MISSION_CONTROL_HTML.replace("__SECTIONS__", json.dumps(sections))
