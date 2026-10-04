"""web-intel — an MCP server that lets BOSS agents STUDY a live external website.

BOSS's internal tools only see internal data and the local repo. This server adds
the missing capability: fetch and *render* an external URL (including JavaScript
single-page apps) and return clean text + the site's navigation, so an agent can
research a product before writing a spec/PRD.

Tools:
  - fetch_page(url, render=true)         -> rendered title/text/links for ONE url
  - study_site(url, max_pages=6)         -> entry page + a breadth crawl of same-
                                            origin nav routes, as compact digests
  - render_mockup(html, viewport, ...)   -> screenshot a self-contained HTML/CSS
                                            mockup to a PNG (the "designer screen"
                                            capability — free, reuses Chromium)
  - search_web(query, n=5)               -> top web results {title, url, snippet}
                                            from Exa (no key needed), Tavily or Brave

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
import os
import re
from contextvars import ContextVar
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


def _render_mockup(args: dict) -> dict:
    """Render a self-contained HTML mockup with Chromium and save a PNG screenshot.

    Closes the 'designer screenshot' gap with no paid dependency: the agent writes
    a self-contained HTML/CSS mockup (the model already does this), and this tool
    turns it into a real, pixel-accurate design screen via the Playwright that
    web-intel already runs. HTML must be self-contained (inline CSS; web fonts /
    images via https URLs only — no local file refs)."""
    import os
    import hashlib

    html = args.get("html")
    if not html or not isinstance(html, str):
        return {"error": "an 'html' string (self-contained mockup) is required"}
    presets = {"desktop": (1280, 800), "tablet": (834, 1112), "mobile": (390, 844)}
    vp = args.get("viewport") or "desktop"
    if isinstance(vp, dict):
        try:
            width, height = int(vp.get("width", 1280)), int(vp.get("height", 800))
        except Exception:
            width, height = presets["desktop"]
    else:
        width, height = presets.get(str(vp), presets["desktop"])
    width = max(320, min(width, 2560))
    height = max(320, min(height, 4000))
    full_page = bool(args.get("full_page", True))
    label = re.sub(r"[^a-z0-9]+", "-", str(args.get("label") or "mockup").lower()).strip("-") or "mockup"
    out_dir = os.path.expanduser(os.environ.get("BOSS_MOCKUP_DIR", "~/.boss/mockups"))
    os.makedirs(out_dir, exist_ok=True)
    digest = hashlib.sha1(html.encode("utf-8")).hexdigest()[:8]
    fpath = os.path.join(out_dir, f"{label}-{width}x{height}-{digest}.png")

    def shoot(page):
        page.set_viewport_size({"width": width, "height": height})
        page.set_content(html, wait_until="networkidle")
        page.wait_for_timeout(400)
        page.screenshot(path=fpath, full_page=full_page)
        return True

    try:
        _with_browser(shoot)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": fpath,
        "viewport": {"width": width, "height": height},
        "full_page": full_page,
        "bytes": os.path.getsize(fpath),
        "note": "PNG mockup rendered from the provided HTML. Open/attach the path; "
                "embed in a PRD or PR for review.",
    }


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
    {
        "name": "render_mockup",
        "description": (
            "RENDER a self-contained HTML/CSS mockup to a PNG screenshot (a real "
            "designer screen). Use after writing a mockup to produce a visual the "
            "user can review or attach to a PRD/PR. HTML must be self-contained: "
            "inline CSS, and any fonts/images via https URLs (no local file refs). "
            "Returns the saved PNG path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "html": {"type": "string", "description": "Self-contained HTML document to render"},
                "viewport": {
                    "description": "Preset 'desktop'|'tablet'|'mobile' or {width,height}",
                    "type": ["string", "object"],
                },
                "full_page": {"type": "boolean", "description": "Capture full scroll height (default true)"},
                "label": {"type": "string", "description": "Filename hint, e.g. 'home-mockup'"},
            },
            "required": ["html"],
        },
    },
]
TOOLS.append({
    "name": "search_web",
    "description": (
        "SEARCH the web for sources on a question and return the top results as "
        "{title, url, snippet}. Use to find pages worth reading; then call fetch_page "
        "on the ones that matter."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for"},
            "n": {"type": "integer", "description": "How many results (1-10, default 5)"},
        },
        "required": ["query"],
    },
})


# ---------------------------------------------------------------- search_web
#
# WHICH SEARCH, AND WITH WHOSE KEY, travels with the request, not the arguments: BOSS gives this
# server a sign-in (X-Search-Provider, and the key as a bearer) from Skills & MCP › Web search, so
# the key is never in a tool call's inputs and never in a receipt. Exa answers without a key
# through its hosted MCP server (the default, operator decision D-3); Tavily and Brave need one.
_REQUEST_HEADERS: ContextVar[dict] = ContextVar("web_intel_request_headers", default={})

SEARCH_PROVIDERS = ("exa", "tavily", "brave")
EXA_MCP_URL = os.environ.get("BOSS_EXA_MCP_URL", "https://mcp.exa.ai/mcp")
SEARCH_TIMEOUT_SECONDS = 15.0


def _search_identity() -> tuple[str, str]:
    headers = {k.lower(): v for k, v in (_REQUEST_HEADERS.get() or {}).items()}
    provider = (headers.get("x-search-provider") or os.environ.get("BOSS_SEARCH_PROVIDER")
                or "exa").strip().lower()
    auth = headers.get("authorization") or ""
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not key:
        key = os.environ.get({"exa": "EXA_API_KEY", "tavily": "TAVILY_API_KEY",
                              "brave": "BRAVE_SEARCH_API_KEY"}.get(provider, ""), "")
    return provider, key


def _clip(text: str, n: int = 300) -> str:
    text = _norm_text(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def _results(items: list, title: str, url: str, snippet: str) -> list[dict]:
    out = []
    for item in items or []:
        if isinstance(item, dict) and item.get(url):
            out.append({"title": str(item.get(title) or item.get(url)),
                        "url": str(item[url]), "snippet": _clip(str(item.get(snippet) or ""))})
    return out


def _search_exa(query: str, n: int, key: str) -> list[dict]:
    import httpx

    if key:
        resp = httpx.post("https://api.exa.ai/search",
                          headers={"x-api-key": key, "Content-Type": "application/json"},
                          json={"query": query, "numResults": n,
                                "contents": {"text": {"maxCharacters": 400}}},
                          timeout=SEARCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return _results(resp.json().get("results") or [], "title", "url", "text")
    # NO KEY: Exa's own hosted MCP server, which answers without one.
    from boss_engine.services.mcp_client import MCPClient

    found = MCPClient(None, timeout=SEARCH_TIMEOUT_SECONDS).call_tool(
        EXA_MCP_URL, "web_search_exa", {"query": query, "numResults": n})
    if isinstance(found, dict) and isinstance(found.get("results"), list):
        return _results(found["results"], "title", "url", "text")[:n]
    text = found.get("text") if isinstance(found, dict) else str(found)
    return _parse_exa_text(str(text or ""))[:n]


def _parse_exa_text(text: str) -> list[dict]:
    """Exa's MCP answers in text blocks of `Title: … URL: … Text: …`; read them back."""
    out: list[dict] = []
    for block in re.split(r"\n(?=Title:)", text):
        title = re.search(r"Title:\s*(.+)", block)
        url = re.search(r"URL:\s*(\S+)", block)
        body = re.search(r"Text:\s*([\s\S]+)", block)
        if url:
            out.append({"title": (title.group(1).strip() if title else url.group(1)),
                        "url": url.group(1).strip(),
                        "snippet": _clip(body.group(1) if body else "")})
    return out


def _search_tavily(query: str, n: int, key: str) -> list[dict]:
    import httpx

    resp = httpx.post("https://api.tavily.com/search",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"query": query, "max_results": n}, timeout=SEARCH_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return _results(resp.json().get("results") or [], "title", "url", "content")


def _search_brave(query: str, n: int, key: str) -> list[dict]:
    import httpx

    resp = httpx.get("https://api.search.brave.com/res/v1/web/search",
                     params={"q": query, "count": n},
                     headers={"X-Subscription-Token": key, "Accept": "application/json"},
                     timeout=SEARCH_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return _results((resp.json().get("web") or {}).get("results") or [], "title", "url",
                    "description")


def _search_web(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    try:
        n = max(1, min(int(args.get("n") or 5), 10))
    except (TypeError, ValueError):
        n = 5
    provider, key = _search_identity()
    if provider not in SEARCH_PROVIDERS:
        return {"error": f"unknown search provider {provider!r}; use exa, tavily or brave"}
    if provider in ("tavily", "brave") and not key:
        return {"error": f"{provider} needs an API key: set it in Skills & MCP › Web search"}
    try:
        results = {"exa": _search_exa, "tavily": _search_tavily,
                   "brave": _search_brave}[provider](query, n, key)
    except Exception as exc:  # noqa: BLE001 — the provider's refusal IS the answer
        status = getattr(getattr(exc, "response", None), "status_code", None)
        said = f"{status} from {provider}" if status else f"{type(exc).__name__}: {exc}"
        return {"error": said[:300], "provider": provider}
    return {"provider": provider, "query": query, "results": results[:n]}


TOOL_FNS = {"fetch_page": _fetch_page, "study_site": _study_site, "render_mockup": _render_mockup,
            "search_web": _search_web}


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
        token = _REQUEST_HEADERS.set(dict(request.headers))
        try:
            result = await run_in_threadpool(fn, args)
        finally:
            _REQUEST_HEADERS.reset(token)
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"content": [{"type": "text", "text": json.dumps(result)}], "isError": bool(result.get("error"))},
        }
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-web-intel-mcp", "tools": [t["name"] for t in TOOLS]}
