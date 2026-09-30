"""doc-render — an MCP server that renders Markdown into a PDF document.

BOSS has no document-rendering capability natively; this exposes one as a
governed, reusable MCP tool so any agent can turn a written spec/PRD/markdown
into a real PDF. The PDF is written to a shared outputs directory and the tool
returns a *handle* (path + page count + size + sha256) rather than the raw
bytes, so large binaries never travel through the agent's LLM context.

Tool:
  - render_markdown_pdf(markdown, title?, subtitle?, filename?) -> file handle

Run standalone:
    .venv/bin/python -m uvicorn tools.mcp_doc_render_server:app --port 8771

Implements MCP JSON-RPC 2.0 (POST /mcp).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, Request, Response

# shared, dependency-light renderer (reportlab only)
from apps.api.app.services.pdf_render import pdf_page_count, render_markdown_pdf

app = FastAPI(title="boss-doc-render-mcp")

OUT_DIR = Path(os.environ.get("BOSS_DOC_RENDER_DIR", ".local-logs/rendered")).resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _slug(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "document")).strip("-").lower()
    return (s or "document")[:60]


def _render_markdown_pdf(args: dict) -> dict:
    markdown = str(args.get("markdown") or "").strip()
    if not markdown:
        return {"error": "markdown is required"}
    title = args.get("title")
    subtitle = args.get("subtitle")
    filename = args.get("filename") or (_slug(title) if title else "document")
    if not str(filename).endswith(".pdf"):
        filename = f"{_slug(str(filename))}.pdf"
    try:
        pdf = render_markdown_pdf(markdown, title=title, subtitle=subtitle)
    except Exception as exc:
        return {"error": f"render failed: {type(exc).__name__}: {exc}"}
    out_path = OUT_DIR / filename
    out_path.write_bytes(pdf)
    return {
        "status": "rendered",
        "path": str(out_path),
        "filename": filename,
        "bytes": len(pdf),
        "pages": pdf_page_count(pdf),
        "sha256": hashlib.sha256(pdf).hexdigest(),
    }


TOOLS = [
    {
        "name": "render_markdown_pdf",
        "description": (
            "Render a Markdown document (e.g. a PRD/spec) into a PDF file and "
            "return a handle (path, page count, size, sha256). Use after writing "
            "the document's markdown to produce a verifiable PDF artifact."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "markdown": {"type": "string", "description": "Full markdown body of the document"},
                "title": {"type": "string", "description": "Document title (rendered as the cover heading)"},
                "subtitle": {"type": "string", "description": "Optional subtitle / context line"},
                "filename": {"type": "string", "description": "Optional output filename (\".pdf\" appended if missing)"},
            },
            "required": ["markdown"],
        },
    },
]
TOOL_FNS = {"render_markdown_pdf": _render_markdown_pdf}


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "boss-doc-render", "version": "0.1.0"},
            },
        }
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOL_FNS.get(name)
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        result = fn(args)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": bool(result.get("error"))},
        }
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-doc-render-mcp", "tools": [t["name"] for t in TOOLS], "out_dir": str(OUT_DIR)}
