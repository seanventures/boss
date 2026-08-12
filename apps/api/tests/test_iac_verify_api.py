"""HTTP surface for the IaC verification lane + related operator endpoints.

Covers POST /v1/iac/verify (new), plus smoke assertions that provider definitions
and provider-health poll remain reachable for operators.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    b = tmp_path / "fakebin"
    b.mkdir()
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ.get('PATH', '')}")
    return b


def _write_bin(bindir, name, body):
    p = bindir / name
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return p


def _all_tools(bindir):
    _write_bin(
        bindir,
        "tofu",
        (
            'case "$1" in\n'
            '  fmt) echo "fmt checked"; exit 0 ;;\n'
            '  init) echo "Initializing the backend..."; exit 0 ;;\n'
            '  validate) echo \'{"format_version":"1.0","valid":true,"error_count":0}\'; '
            "exit 0 ;;\n"
            '  plan) echo "Plan: 0 to add, 0 to change, 0 to destroy."; exit 0 ;;\n'
            "  *) exit 0 ;;\n"
            "esac\n"
        ),
    )
    _write_bin(bindir, "tflint", 'echo \'{"issues":[]}\'; exit 0')
    _write_bin(bindir, "checkov", 'echo \'{"summary":{"failed":0}}\'; exit 0')


def test_provider_definitions_list(client):
    response = client.get("/v1/provider-definitions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert "items" in body
    assert isinstance(body["items"], list)
    assert body["items"], "expected at least one provider definition"
    first = body["items"][0]
    assert "provider" in first or "capabilities" in first


def test_provider_health_list_and_poll(client):
    listed = client.get(
        "/v1/provider-health",
        params={"project_id": "boss-dogfood", "limit": 10},
    )
    assert listed.status_code == 200, listed.text
    assert "items" in listed.json()

    polled = client.post(
        "/v1/provider-health/poll",
        json={"project_id": "boss-dogfood", "actor": "test_operator"},
    )
    assert polled.status_code == 200, polled.text
    body = polled.json()
    assert isinstance(body, dict)


def test_iac_verify_rejects_empty_files(client):
    response = client.post("/v1/iac/verify", json={"files": {}})
    assert response.status_code == 422, response.text


def test_iac_verify_rejects_path_escape(client):
    response = client.post(
        "/v1/iac/verify",
        json={"files": {"../etc/passwd": "nope"}},
    )
    assert response.status_code == 422, response.text


def test_iac_verify_happy_path(client, bindir):
    _all_tools(bindir)
    response = client.post(
        "/v1/iac/verify",
        json={
            "files": {
                "main.tf": 'resource "null_resource" "x" {}\n',
                "variables.tf": 'variable "region" { type = string }\n',
            },
            "actor": "test_operator",
            "project_id": "boss-dogfood",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["tf_binary"] in ("tofu", "terraform")
    assert body["scanner"] in ("checkov", "tfsec")
    assert "main.tf" in body["files"]
    steps = {step["step"]: step for step in body["steps"]}
    assert steps["fmt"]["ok"] is True
    assert steps["validate"]["ok"] is True
    assert steps["lint"]["ok"] is True
    assert steps["security"]["ok"] is True
    assert steps["plan_dry"]["ok"] is True
    assert steps["plan_live"]["skipped"] is True
    assert body["actor"] == "test_operator"
    assert body["project_id"] == "boss-dogfood"


def test_iac_verify_fail_closed_without_toolchain(client, bindir, monkeypatch):
    # Isolate PATH to the empty bindir so host toolchain cannot satisfy the lane.
    monkeypatch.setenv("PATH", str(bindir))
    response = client.post(
        "/v1/iac/verify",
        json={"files": {"main.tf": 'resource "null_resource" "x" {}\n'}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    steps = {step["step"]: step for step in body["steps"]}
    assert steps["fmt"]["ok"] is False
    assert steps["fmt"].get("reason") == "tool_missing"
    assert steps["lint"].get("reason") == "tool_missing"
    assert steps["security"].get("reason") == "tool_missing"
