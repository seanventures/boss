"""web-intel — an MCP server that lets BOSS agents STUDY a live external website.

BOSS's internal tools only see internal data and the local repo. This server adds
the missing capability: fetch and *render* an external URL (including JavaScript
single-page apps) and return clean text + the site's navigation, so an agent can
research a product before writing a spec/PRD.

Tools:
  - fetch_page(url, render=true)         -> rendered title/text/links for ONE url
  - study_site(url, max_pages=6)         -> entry page + a breadth crawl of same-
                                            origin nav routes, as compact digests

Rendering uses Playwright/Chromium (handles SPAs that a plain HTTP GET cannot).
Results are size-capped so they fit an agent's tool-result budget; an agent that
needs more depth on one route can call fetch_page on that specific url.

Run standalone:
    .venv/bin/python -m uvicorn tools.mcp_web_intel_server:app --port 8770

Implements MCP JSON-RPC 2.0: initialize, notifications/initialized,
tools/list, tools/call (POST /mcp).
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

app = FastAPI(title="boss-web-intel-mcp")

PER_PAGE_TEXT_CAP = 1600          # chars of rendered text kept per crawled page
SINGLE_PAGE_TEXT_CAP = 12000      # chars kept for a focused fetch_page call
NAV_SETTLE_MS = 3000              # wait after load for client-side rendering


# ---------------------------------------------------------------- helpers
def _norm_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _same_origin(a: str, b: str) -> bool:
    try:
        pa, pb = urlparse(a), urlparse(b)
        return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)
    except Exception:
        return False


def _is_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _render_one(page, url: str, text_cap: int) -> dict:
    """Navigate an existing Playwright page to url and extract a clean digest."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("load", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(NAV_SETTLE_MS)
    title = page.title()
    raw_text = page.inner_text("body")
    text = _norm_text(raw_text)
    hrefs = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))"
    )
    # resolve + dedupe same-origin links
    links: list[str] = []
    seen = set()
    for h in hrefs or []:
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(url, h)
        if _same_origin(url, absu) and absu not in seen:
            seen.add(absu)
            links.append(absu)
    return {
        "url": url,
        "title": title,
        "text": text[:text_cap],
        "text_truncated": len(text) > text_cap,
        "full_text_chars": len(text),
        "links": links[:40],
    }


def _with_browser(fn):
    """Run fn(browser_page_factory) under a fresh Chromium. Synchronous —
    must be called off the event loop (via run_in_threadpool)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            return fn(page)
        finally:
            browser.close()


# ---------------------------------------------------------------- tools
def _fetch_page(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not _is_http_url(url):
        return {"error": "a valid http(s) url is required", "got": url}
    render = args.get("render", True)
    if render is False:
        import httpx

        try:
            r = httpx.get(url, follow_redirects=True, timeout=20.0)
            txt = _norm_text(re.sub(r"(?s)<[^>]+>", " ", r.text))
            return {"url": url, "status": r.status_code, "rendered": False,
                    "text": txt[:SINGLE_PAGE_TEXT_CAP], "title": None, "links": []}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "url": url}
    try:
        data = _with_browser(lambda page: _render_one(page, url, SINGLE_PAGE_TEXT_CAP))
        data["rendered"] = True
        return data
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": url}


def _study_site(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not _is_http_url(url):
        return {"error": "a valid http(s) url is required", "got": url}
    try:
        max_pages = max(1, min(int(args.get("max_pages") or 6), 12))
    except Exception:
        max_pages = 6

    def crawl(page) -> dict:
        visited: list[dict] = []
        seen_urls: set = set()
        # start with the entry page, then BFS its same-origin nav links
        queue = [url]
        while queue and len(visited) < max_pages:
            current = queue.pop(0)
            # normalize trailing slash for dedupe
            key = current.rstrip("/")
            if key in seen_urls:
                continue
            seen_urls.add(key)
            try:
                digest = _render_one(page, current, PER_PAGE_TEXT_CAP)
            except Exception as exc:
                visited.append({"url": current, "error": f"{type(exc).__name__}: {exc}"})
                continue
            visited.append(digest)
            for link in digest.get("links", []):
                if link.rstrip("/") not in seen_urls and link not in queue:
                    queue.append(link)
        # build a sitemap of all discovered routes
        all_routes = sorted({
            urlparse(l).path or "/"
            for d in visited for l in d.get("links", [])
        })
        return {
            "entry_url": url,
            "pages_studied": len(visited),
            "sitemap_routes": all_routes[:60],
            "pages": visited,
        }

    try:
        return _with_browser(crawl)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": url}


TOOLS = [
    {
        "name": "fetch_page",
        "description": (
            "Fetch and render ONE external web page (handles JavaScript SPAs) and "
            "return its title, clean visible text, and same-origin links. Use to "
            "study a specific page of a product/site in depth before writing a spec."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s) URL to fetch"},
                "render": {"type": "boolean", "description": "Render JS (default true). Set false for a fast raw fetch."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "study_site",
        "description": (
            "STUDY an external website: render the entry URL and crawl its same-"
            "origin navigation (up to max_pages), returning a compact digest of "
            "each page plus a sitemap of routes. Use this first to understand a "
            "product end-to-end before writing a PRD/spec; then call fetch_page on "
            "any route you need in more detail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Entry http(s) URL of the site"},
                "max_pages": {"type": "integer", "description": "Max pages to crawl (1-12, default 6)"},
            },
            "required": ["url"],
        },
    },
]
TOOL_FNS = {"fetch_page": _fetch_page, "study_site": _study_site}


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
                "serverInfo": {"name": "boss-web-intel", "version": "0.1.0"},
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
        result = await run_in_threadpool(fn, args)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": bool(result.get("error"))},
        }
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-web-intel-mcp", "tools": [t["name"] for t in TOOLS]}
