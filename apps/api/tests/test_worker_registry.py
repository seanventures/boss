def test_worker_registry_records_capabilities_and_liveness(client):
    registered = client.post(
        "/v1/workers/register",
        json={
            "project_id": "boss-dogfood",
            "worker_id": "worker-registry-test",
            "capabilities": ["test.registry", "test.registry"],
            "metadata": {"pool": "qa"},
            "actor": "ceo",
        },
    )
    assert registered.status_code == 200
    assert registered.json()["capabilities"] == ["test.registry"]
    assert registered.json()["liveness"] == "live"

    listed = client.get("/v1/workers", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200
    row = next(item for item in listed.json()["items"] if item["worker_id"] == "worker-registry-test")
    assert row["metadata"] == {"pool": "qa"}
