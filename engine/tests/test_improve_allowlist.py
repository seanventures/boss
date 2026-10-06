"""The Improve BOSS allow-list (services/improve_allowlist.py): only named fields travel, names
outside the public catalogue become `custom`, and the redaction gate has the last word."""
from boss_engine.services import improve_allowlist as allow


def _env(**over):
    body = {"v": 1, "day": "2026-10-05", "install_id": "bi_" + "a" * 26, "secret_hash": "a" * 64}
    body.update(over)
    return body


def test_unknown_fields_are_dropped_and_counted():
    clean, dropped = allow.sanitize(_env(hostname="db-01", heartbeat={"os": "linux", "ip": "10.1.1.1"}))
    assert "hostname" not in clean and clean["heartbeat"] == {"os": "linux"}
    assert dropped == 2


def test_identifiers_with_digit_runs_survive_the_pii_gate():
    """A hash or install id full of digits looks like a phone number to the PII patterns."""
    iid = "bi_ab234567234567234567abcdef"
    clean, _ = allow.sanitize(_env(install_id=iid, secret_hash="0123456789" * 6 + "abcd"))
    assert clean["install_id"] == iid and clean["secret_hash"].startswith("0123456789")


def test_custom_names_travel_as_custom_and_models_as_families():
    row = {"unit_kind": "build", "role_key": "acme_growth_hacker", "workflow_key": "acme_flow",
           "model": "acme-llama-internal", "harness": "my_harness", "error_class": "acme_boom",
           "skills": ["acme_skill"], "terminal_state": "completed"}
    clean, _ = allow.sanitize({"rows": [row]}, {"rows": ("list", allow.SCORECARD_ROW, 5)})
    got = clean["rows"][0]
    assert got["role_key"] == "custom" and got["workflow_key"] == "custom"
    assert got["model"] == "other" and got["harness"] == "other"
    assert got["error_class"] == "other" and got["skills"] == []
    named, _ = allow.sanitize({"rows": [{"role_key": "software_engineer", "model": "claude-sonnet-5",
                                         "workflow_key": "standard_product_delivery"}]},
                              {"rows": ("list", allow.SCORECARD_ROW, 5)})
    assert named["rows"][0] == {"role_key": "software_engineer", "model": "claude-sonnet-5",
                                "workflow_key": "standard_product_delivery"}


def test_two_custom_names_fold_into_one_count():
    clean, _ = allow.sanitize({"usage": {"workflows": {"acme_a": 2, "acme_b": 3,
                                                       "standard_product_delivery": 1}}},
                              {"usage": allow.BATCH["usage"]})
    assert clean["usage"]["workflows"] == {"custom": 5, "standard_product_delivery": 1}


def test_free_text_is_masked_and_bounded():
    line = "mail me at someone@example.com, key sk-ant-api03-abcdefghijklmnopqrstuvwxyz " + "x" * 300
    clean, _ = allow.sanitize({"feedback": {"lines": [line]}}, {"feedback": allow.BATCH["feedback"]})
    out = clean["feedback"]["lines"][0]
    assert "@example.com" not in out and "sk-ant" not in out and len(out) <= 140


def test_numbers_are_clamped_and_types_enforced():
    clean, _ = allow.sanitize({"rows": [{"score": 7, "cost_usd": -3, "turns": "many",
                                         "pr_merged": "yes", "tests_total": 3.9}]},
                              {"rows": ("list", allow.SCORECARD_ROW, 5)})
    assert clean["rows"][0] == {"score": 1.0, "cost_usd": 0.0, "tests_total": 3}


def test_ranges():
    assert [allow.count_range(n) for n in (0, 1, 2, 6, 21, 101, 9999)] == list(allow.COUNT_RANGES)
    assert allow.age_bucket(400) == "365+" and allow.edit_bucket(0.5) == "30-60%"
