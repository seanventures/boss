"""API key › Other: a form mistake is a 422 pill, never a 500 (audit P2-9).

A base URL typed without its scheme ("api.example.com/v1") raised SetupError out of
`model_key_details`, outside the handler that turns refusals into pills, so the console got a
500. "Other" without a key is refused the same way (the console now asks for it first).
"""
from sqlalchemy import text

from apps.api.tests.setup_helpers import PROJECT, as_ceo, connect_key, fakes


def _no_custom_row(engine) -> bool:
    with engine.begin() as c:
        return not c.execute(text("SELECT 1 FROM entitlement_sources WHERE project_id = :p "
                                  "AND source_id LIKE 'setup-custom%'"), {"p": PROJECT}).first()


def test_a_base_url_without_a_scheme_is_a_422_pill(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    calls: list = []
    fakes(monkeypatch, model_calls=calls)
    for url in ("api.example.com/v1", "file:///etc/passwd", "https://"):
        response = connect_key(client, provider="custom", model="m", base_url=url)
        assert response.status_code == 422, (url, response.text)
        detail = response.json()["detail"]
        assert detail["headline"] == "Base URL", url
        assert "https://" in detail["fix"]
    assert calls == []                      # no provider was asked anything
    assert _no_custom_row(engine)


def test_other_without_a_key_is_refused_before_anything(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    calls: list = []
    fakes(monkeypatch, model_calls=calls)
    response = connect_key(client, key="", provider="custom", model="m",
                           base_url="https://llm.example.com/v1")
    assert response.status_code == 422
    assert response.json()["detail"]["headline"] == "API key"
    assert calls == []
    assert _no_custom_row(engine)
