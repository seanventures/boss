def test_qa_benchmark_catalog_is_exposed_as_a_pinned_contract(client):
    response = client.get("/v1/qa/benchmark-apps")

    assert response.status_code == 200
    apps = response.json()["benchmark_apps"]
    assert [app["id"] for app in apps] == ["bugbox", "hookwatch", "approveit"]
    assert all(app["completion_contract"] for app in apps)
    assert all("npm run verify" not in app["verification_commands"] for app in apps)
    assert all("npm run healthcheck" in app["verification_commands"] for app in apps)
