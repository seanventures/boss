"""Every knowledge route answers from its own handler (BOSSAPP-622).

Starlette serves the first route that matches. The knowledge router is mounted after main.py's
own /v1/knowledge/* routes, so a path both declare is answered by main.py and the router's
handler never runs: the console's knowledge settings and promotion queue read
GET /v1/knowledge/status and got BOSS-356's pack-context probe instead of the graph status.
"""
from __future__ import annotations

from collections import Counter


def test_no_two_knowledge_routes_share_a_method_and_path():
    from apps.api.app.main import app

    seen = Counter(
        (method, route.path)
        for route in app.routes
        if getattr(route, "path", "").startswith("/v1/knowledge")
        for method in (getattr(route, "methods", None) or ())
    )
    assert [key for key, count in seen.items() if count > 1] == []


def test_the_graph_status_route_is_the_knowledge_routers():
    from apps.api.app.main import app
    from apps.api.app.routers import knowledge

    handlers = [route.endpoint for route in app.routes
                if getattr(route, "path", "") == "/v1/knowledge/graph-status"]
    assert handlers and handlers[0].__module__ == knowledge.__name__
