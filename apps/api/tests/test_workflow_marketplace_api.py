"""The Workflow Marketplace over HTTP, as the console drives it — on the community surface.

Browse, Install into Yours, Review update, Uninstall, Publish and Unpublish live under
/v1/delivery-workflows/marketplace: `/v1/marketplace` is the paid surface (route_ownership), and
workflow packs must work on an open-source install.
"""
from __future__ import annotations

import copy
from uuid import uuid4

import pytest

from apps.api.app.routers.route_ownership import is_enterprise_path

PROJECT = "boss-dogfood"
IR = "incident_response"
ROUTES = ("/v1/delivery-workflows/marketplace",
          "/v1/delivery-workflows/marketplace/install",
          "/v1/delivery-workflows/marketplace/uninstall",
          "/v1/delivery-workflows/marketplace/update",
          "/v1/delivery-workflows/marketplace/publish",
          "/v1/delivery-workflows/marketplace/unpublish")


def _post(client, path: str, **body):
    return client.post(f"/v1/delivery-workflows/marketplace/{path}",
                       json={"project_id": PROJECT, "actor": "ceo", **body})


def _packs(client) -> dict:
    r = client.get("/v1/delivery-workflows/marketplace", params={"project_id": PROJECT})
    assert r.status_code == 200, r.text
    return {p["id"]: p for p in r.json()["items"]}


def _library(client) -> dict:
    r = client.get("/v1/delivery-workflows", params={"project_id": PROJECT})
    assert r.status_code == 200, r.text
    return {w["workflow_key"]: w for w in r.json()["items"]}


@pytest.mark.parametrize("path", ROUTES)
def test_workflow_packs_are_community_not_the_paid_marketplace(path):
    assert not is_enterprise_path(path)
    assert is_enterprise_path("/v1/marketplace/listings")   # the paid one stays paid


def test_the_routes_are_mounted_on_this_app():
    from fastapi.routing import APIRoute

    from apps.api.app.main import app

    mounted = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert set(ROUTES) <= mounted


def test_the_catalogue_install_keeps_working_and_ships_no_pack(client):
    r = client.post("/v1/delivery-workflows/catalogue/install",
                    json={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 200, r.text
    library = _library(client)
    assert library["standard_product_delivery"]["is_catalogue"] is True
    assert IR not in library and "dq_incident_rca" not in library


def test_install_review_update_and_uninstall_over_http(client):
    packs = _packs(client)
    assert {"banking-dq", "sre-helpdesk"} <= set(packs)
    sre = packs["sre-helpdesk"]
    assert sre["publisher"] == "BOSS" and sre["version"] and sre["installed"] is False
    assert sre["workflows"][0]["key"] == IR and sre["workflows"][0]["steps"]

    assert _post(client, "install", pack_id="no-such-pack").status_code == 404
    r = _post(client, "install", pack_id="sre-helpdesk")
    assert r.status_code == 200, r.text
    assert r.json()["installed"][0]["key"] == IR
    item = _library(client)[IR]
    assert item["is_catalogue"] is False
    assert item["marketplace"]["pack_id"] == "sre-helpdesk"
    assert item["marketplace"]["version"] == sre["version"]
    spec = client.get(f"/v1/delivery-workflows/{IR}/spec", params={"project_id": PROJECT}).json()
    assert spec["standard"] is None and spec["marketplace"]["pack_name"] == "SRE / Helpdesk"
    assert _packs(client)["sre-helpdesk"]["installed"] is True

    # Nothing is waiting, so there is nothing to take; a wrong act is a 400.
    did = item["definition_id"]
    assert _post(client, "update", definition_id=did, act="take").status_code == 409
    assert _post(client, "update", definition_id=did, act="overwrite").status_code == 400

    r = _post(client, "uninstall", pack_id="sre-helpdesk")
    assert r.status_code == 200, r.text
    assert IR not in _library(client)
    assert _post(client, "uninstall", pack_id="sre-helpdesk").status_code == 404


def test_starting_a_pack_workflow_that_is_not_installed_says_where_it_is(client):
    r = client.post("/v1/delivery-workflows/start",
                    json={"project_id": PROJECT, "actor": "ceo", "workflow_key": IR,
                          "input": {"incident_id": "inc_x", "directive": "x"}})
    assert r.status_code == 404, r.text
    assert "Shared library" in r.text and "SRE / Helpdesk" in r.text


def test_publish_and_unpublish_a_workflow_of_your_own(client):
    client.post("/v1/delivery-workflows/catalogue/install",
                json={"project_id": PROJECT, "actor": "ceo"})
    standard = client.get("/v1/delivery-workflows/fast_fix/spec",
                          params={"project_id": PROJECT}).json()
    name = f"Lumen Pay partner review {uuid4().hex[:6]}"
    spec = copy.deepcopy(standard["spec"])
    saved = client.post("/v1/delivery-workflows/publish",
                        json={"project_id": PROJECT, "actor": "ceo", "name": name,
                              "spec": spec, "definition_id": standard["definition_id"],
                              "as_copy": True})
    assert saved.status_code == 200, saved.text
    mine = next(w for w in _library(client).values() if w["name"] == name)
    assert mine["is_catalogue"] is False

    r = _post(client, "publish", definition_id=standard["definition_id"])
    assert r.status_code == 409 and "standard" in r.text
    r = _post(client, "publish", definition_id=mine["definition_id"],
              name="Gateway tenant onboarding", version="0.3")
    assert r.status_code == 200, r.text
    pack = r.json()["pack"]
    assert pack["version"] == "0.3" and pack["status"] == "published"
    listed = _packs(client)[pack["id"]]
    assert listed["published_here"] is True and listed["source"] == "org"
    assert _library(client)[mine["workflow_key"]]["published"]["version"] == "0.3"
    assert _post(client, "publish", definition_id=mine["definition_id"],
                 version="0.2").status_code == 409
    assert _post(client, "publish", definition_id=mine["definition_id"],
                 version="next").status_code == 422
    assert _post(client, "install", pack_id=pack["id"]).status_code == 409

    r = _post(client, "unpublish", pack_id=pack["id"])
    assert r.status_code == 200, r.text
    assert pack["id"] not in _packs(client)
    # Again: the next version.
    r = _post(client, "publish", definition_id=mine["definition_id"])
    assert r.status_code == 200 and r.json()["pack"]["version"] == "0.4"
