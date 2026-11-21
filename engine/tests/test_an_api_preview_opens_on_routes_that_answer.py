"""An API's preview opens on a page that says it is an API and links the routes that answer.

THE INCIDENT (prev_82bfd120d83a4662a23a, 2026-11-21). The preview of an API gateway ran fine —
/healthz and /readyz answered 200 — but "Open preview" opens "/", where the gateway answers
404 "unmatched". Every person read it as a broken preview.

Now the preview proxy, on a browser page load of the root that the app answers 4xx "nothing
here", serves BOSS's landing: "This app is an API — there is no page at /" and the routes that
answer (declared health checks, the app's own routes, the common ones), each asked first.
"""
from __future__ import annotations

import inspect
import json

import pytest

from boss_engine.services import preview_landing as pl

PAGE = {"sec-fetch-mode": "navigate", "accept": "text/html"}
FETCH = {"sec-fetch-mode": "cors", "accept": "application/json"}


class TestTheDecision:
    @pytest.mark.parametrize("status", sorted(pl.ROOT_MISS_STATUSES))
    def test_a_page_load_of_a_root_that_says_nothing_here_gets_the_landing(self, status):
        assert pl.wants_landing("GET", "", status, PAGE)
        assert pl.wants_landing("GET", "/", status, {"accept": "text/html,*/*"})

    @pytest.mark.parametrize("status", [200, 204, 301, 302, 401, 403, 407, 500, 502, 503])
    def test_a_root_that_answers_or_asks_for_a_login_is_the_app_s_own(self, status):
        assert not pl.wants_landing("GET", "", status, PAGE)

    def test_a_script_s_fetch_keeps_the_app_s_real_status(self):
        assert not pl.wants_landing("GET", "", 404, FETCH)

    def test_only_the_root(self):
        assert not pl.wants_landing("GET", "orders/7", 404, PAGE)
        assert not pl.wants_landing("GET", "healthz", 404, PAGE)

    def test_only_a_read(self):
        assert not pl.wants_landing("POST", "", 404, PAGE)


class TestWhatBossKnows:
    def test_declared_and_target_checks_come_first_then_the_tree_then_convention(self):
        got = pl.candidate_paths(declared=["/", "/ready"], targets=["/healthz"],
                                 repo=["/v1/orders", "/healthz"])
        assert got[:3] == ["/ready", "/healthz", "/v1/orders"]
        assert "/readyz" in got and "/" not in got
        assert len(got) == len(set(got)) <= pl.MAX_PROBES

    def test_templates_are_not_links(self):
        assert pl.candidate_paths(repo=["/orders/{id}", "/users/:id", "/files/*"],
                                  declared=[])[0] == "/healthz"

    def test_both_target_check_shapes_give_their_paths(self):
        assert pl.target_check_paths([
            {"domains": ["192.0.2.202:8080"],
             "health_checks": [{"kind": "http", "path": "/healthz", "port": 8080,
                                "scheme": "http", "expect_status": 200}]},
            {"domains": ["http://192.0.2.202:8080/readyz"],
             "health_checks": [{"url": "http://192.0.2.202:8080/readyz", "expected_status": 200}]},
        ]) == ["/healthz", "/readyz"]

    def test_an_openapi_document_names_its_parameterless_get_routes(self):
        doc = {"servers": [{"url": "/api"}],
               "paths": {"/orders": {"get": {}}, "/orders/{id}": {"get": {}},
                         "/orders/bulk": {"post": {}}, "/ping": {"GET": {}}}}
        assert pl.openapi_get_paths(doc) == ["/api/orders", "/api/ping"]

    def test_the_tree_s_routes_are_read_from_spec_code_and_config(self, tmp_path):
        (tmp_path / "openapi.json").write_text(json.dumps({"paths": {"/v1/catalog": {"get": {}}}}))
        (tmp_path / "cmd").mkdir()
        (tmp_path / "cmd" / "main.go").write_text(
            'mux.HandleFunc("/healthz", h)\nmux.HandleFunc("/readyz", r)\nr.GET("/v1/items/:id", x)\n')
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "routes.yaml").write_text(
            "routes:\n  - path: /orders\n    upstream: http://orders\n  - prefix: \"/payments\"\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "x.js").write_text('app.get("/never", f)')
        got = pl.repo_routes(tmp_path)
        assert got[0] == "/v1/catalog"
        assert {"/healthz", "/readyz", "/orders", "/payments"} <= set(got)
        assert "/never" not in got and not any(":" in p for p in got)

    def test_no_tree_is_no_routes(self):
        assert pl.repo_routes(None) == []


class TestOnlyWhatAnswersIsLinked:
    def test_answering_keeps_2xx_and_3xx_in_order(self):
        codes = {"/healthz": 200, "/readyz": 204, "/docs": 302, "/api": 404, "/boom": 500}

        def fetch(p):
            if p == "/raises":
                raise OSError("reset")
            return codes.get(p, 0)

        got = pl.answering(["/healthz", "/api", "/readyz", "/raises", "/docs", "/boom"], fetch)
        assert [r["path"] for r in got] == ["/healthz", "/readyz", "/docs"]


class TestThePage:
    def _page(self, **kw):
        base = {"root_status": 404, "root_body": b"unmatched", "asked": 12,
                "routes": [{"path": "/healthz", "status": 200}, {"path": "/readyz", "status": 200}]}
        return pl.landing_html(**{**base, **kw}).decode()

    def test_it_says_what_happened_in_plain_words(self):
        page = self._page()
        assert "This app is an API — there is no page at /" in page
        assert "HTTP 404" in page and "unmatched" in page
        assert "The preview is running" in page

    def test_its_links_stay_inside_the_preview(self):
        assert "href='healthz'" in self._page()
        assert "href='web/healthz'" in self._page(link_prefix=pl.link_prefix_for(
            "/v1/previews/prev_x/web"))
        assert pl.link_prefix_for("/v1/previews/prev_x/web/") == ""
        assert pl.link_prefix_for("/") == ""

    def test_nothing_answering_says_so_rather_than_an_empty_list(self):
        page = self._page(routes=[])
        assert "None of the 12 routes" in page and "<ul>" not in page

    def test_the_app_s_body_cannot_inject_markup(self):
        page = self._page(root_body=b"<script>alert(1)</script>")
        assert "<script>" not in page


class TestTheProxyServesIt:
    def test_the_preview_proxy_consults_the_decision(self):
        from apps.api.app import main

        body = inspect.getsource(main.preview_web_proxy)
        assert "preview_landing.wants_landing(request.method, path, status, request.headers)" in body
        assert "**isolation" in body.split("preview_landing.landing_for")[1].split("except")[0]
