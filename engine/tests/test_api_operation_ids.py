"""An HTTP operation has one stable name regardless of FastAPI route order."""

from boss_engine.services.api_operation_ids import stable_operation_id, stable_operation_ids


def test_multi_method_route_gets_distinct_repeatable_ids():
    document = {
        "paths": {
            "/v1/previews/{preview_id}/web": {
                "post": {"operationId": "old_duplicate"},
                "get": {"operationId": "old_duplicate"},
                "delete": {"operationId": "old_duplicate"},
            }
        }
    }

    stable_operation_ids(document)
    operations = document["paths"]["/v1/previews/{preview_id}/web"]
    assert list(operations) == ["delete", "get", "post"]
    assert len({operation["operationId"] for operation in operations.values()}) == 3
    assert operations["get"]["operationId"] == stable_operation_id(
        "get", "/v1/previews/{preview_id}/web"
    )

    before = repr(document)
    stable_operation_ids(document)
    assert repr(document) == before
