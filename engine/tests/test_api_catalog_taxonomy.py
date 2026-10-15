"""Every mounted product operation must have an operator-facing domain."""

from pathlib import Path

import yaml

from boss_engine.services.api_catalog import classify_path


ROOT = Path(__file__).resolve().parents[2]


def test_community_and_private_http_contracts_have_no_unmapped_product_paths():
    # The private source keeps the Community snapshot beside its own contract;
    # the public export ships only api/openapi.yaml, which is the Community
    # contract there (docs/community/TERMINAL_CAPABILITY_MATRIX.md).
    assert (ROOT / "api" / "openapi.yaml").is_file()
    for artifact in ("openapi.community.yaml", "openapi.yaml"):
        if not (ROOT / "api" / artifact).is_file():
            continue
        document = yaml.safe_load((ROOT / "api" / artifact).read_text())
        unmapped = [
            path for path in document["paths"]
            if path.startswith("/v1/") and classify_path(path) == "other"
        ]
        assert unmapped == [], f"{artifact} has product routes hidden in other"


def test_setup_and_console_have_separate_discoverable_domains():
    assert classify_path("/v1/setup/status") == "setup"
    assert classify_path("/v1/console/v2/workforce") == "console"
    assert classify_path("/v1/projects") == "workspace"
