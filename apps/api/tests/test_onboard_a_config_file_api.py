"""R5 over HTTP: "Onboard a config file" is in the catalogue, installs into a project with its
"Checks must pass" setting, and installing it touches no custom workflow (API onboarding keeps
every row)."""
import json
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services import workflows

PROJECT = "boss-dogfood"
KEY = "onboard_config_file"


def test_the_catalogue_lists_it_with_its_standard_steps(client):
    listed = client.get("/v1/delivery-workflows/catalogue")
    assert listed.status_code == 200, listed.text
    item = next(i for i in listed.json()["items"] if i["key"] == KEY)
    assert item["name"] == "Onboard a config file"
    assert [s["capability"] for s in item["steps"]] == [
        "build", "review", "merge", "config_deploy", "contract_test"]
    assert {"start_from", "target_path"} <= set(item["input_schema"]["required"])


def test_installing_it_keeps_every_custom_workflow(client, engine):
    custom = f"api_onboarding_{uuid4().hex[:8]}"
    with engine.begin() as connection:
        d = workflows.create_definition(
            connection, project_id=PROJECT, workflow_key=custom, name="API onboarding",
            description=None, actor="ceo", namespace=workflows.PRODUCT_NAMESPACE)
        v = workflows.publish_version(
            connection, project_id=PROJECT, definition_id=d["id"], actor="ceo",
            spec={"entry_step": "tell", "steps": [{
                "id": "tell", "kind": "capability", "next": [],
                "binding": {"capability": "notify", "inputs": {"message": "x"}}}]})
    installed = client.post("/v1/delivery-workflows/catalogue/install",
                            json={"project_id": PROJECT, "actor": "ceo"})
    assert installed.status_code == 200, installed.text
    assert KEY in [i["key"] for i in installed.json()["installed"]]
    with engine.connect() as connection:
        spec = connection.execute(text(
            "SELECT v.spec FROM workflow_definitions d JOIN workflow_versions v "
            "ON v.workflow_definition_id = d.id WHERE d.project_id = :p AND d.workflow_key = :k "
            "ORDER BY v.version DESC LIMIT 1"), {"p": PROJECT, "k": KEY}).scalar()
        spec = json.loads(spec) if isinstance(spec, str) else spec
        assert spec["checks_must_pass"] is True
        kept = connection.execute(text(
            "SELECT d.status, d.active_version_id FROM workflow_definitions d "
            "WHERE d.id = :i"), {"i": d["id"]}).mappings().one()
    assert kept["status"] != "retired" and kept["active_version_id"] == v["id"]
