"""T1 (P9a): the intake adapter contract and registry — a port of the ticket-registry tests.
The manifest is the truth the console shows; the registry refuses a manifest that claims a hook
its adapter lacks, or an adapter that implements a hook its manifest denies."""
from __future__ import annotations

import copy

import pytest
import yaml

from boss_engine.connectors.intake import REGISTRY, IntakeAdapterBase, get_intake_adapter
from boss_engine.connectors.intake.base import HOOKS, implements
from boss_engine.connectors.intake.contract import (
    CAPABILITIES,
    MODES,
    CapabilityNotSupported,
    ConnectorManifestError,
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    RawRequest,
    kept_headers,
)
from boss_engine.connectors.intake.registry import ROOT, check_adapter, load_folder, validate
from boss_engine.services import env_refs

WEBHOOK_MANIFEST = yaml.safe_load((ROOT / "webhook" / "manifest.yaml").read_text())
GITHUB_MANIFEST = yaml.safe_load((ROOT / "github" / "manifest.yaml").read_text())


# ---- registry --------------------------------------------------------------------------------


def test_every_shipped_folder_loads_and_is_registered():
    folders = {p.name for p in ROOT.iterdir() if (p / "manifest.yaml").is_file()}
    assert folders == {
        "webhook", "github", "slack", "jira", "plane", "gmail", "outlook", "salesforce"
    } <= set(REGISTRY.adapters)
    for key in folders:
        adapter = get_intake_adapter(key)
        assert isinstance(adapter, IntakeAdapterBase) and adapter.key == key
        assert adapter.manifest.key == key and adapter.intake_capabilities == adapter.manifest.capabilities


def test_the_slack_manifest_rides_on_the_chat_endpoint_and_acks_within_3_s():
    slack = get_intake_adapter("slack").manifest
    assert slack.modes == ("push", "socket") and slack.dedupe == ("id",)
    assert slack.ack["deadline_ms"] == 3000 and slack.ack["success_status"] == 200
    assert slack.ack["retry_signal_headers"] == ["x-slack-retry-num", "x-slack-retry-reason"]
    fields = {f["name"]: f for f in slack.auth_fields}
    assert fields["chat_endpoint_id"].get("required", True) is True
    assert all(fields[n]["secret"] and fields[n]["required"] is False
               for n in ("signing_secret", "bot_token", "app_token"))
    assert slack.capabilities["reply"] and slack.capabilities["stream_reply"]
    assert set(slack.event_types) == {"com.slack.message.app_mention", "com.slack.message.im",
                                      "com.slack.message.channel",
                                      "com.slack.message.thread_reply"}


def test_unknown_source_is_a_clear_error():
    with pytest.raises(ValueError, match="Unknown intake source"):
        get_intake_adapter("teletype")
    assert REGISTRY.get("") is None


def test_the_shipped_manifests_are_valid_and_say_what_the_adapters_do():
    webhook = validate(copy.deepcopy(WEBHOOK_MANIFEST), where="webhook")
    assert webhook.modes == ("push",) and webhook.capabilities["verify"] is True
    assert webhook.ack["deadline_ms"] == 10000 and webhook.ack["success_status"] == 202
    github = validate(copy.deepcopy(GITHUB_MANIFEST), where="github")
    assert github.dedupe == ("delivery_id",) and github.self_identity == ("bot_login",)
    assert "api.github.com" in github.default_hosts


def test_manifest_hosts_are_registered_with_the_outbound_guard():
    REGISTRY.load()
    assert "api.github.com" in env_refs.PROVIDER_HOSTS["github"]
    assert env_refs.PROVIDER_HOSTS.get("webhook", ()) == ()      # no official host: nothing added


@pytest.mark.parametrize("mutate, says", [
    (lambda m: m.pop("icon"), "missing 'icon'"),
    (lambda m: m.update(colour="red"), "unknown field 'colour'"),
    (lambda m: m["capabilities"].update(verify="yes"), "capabilities.verify"),
    (lambda m: m["capabilities"].update(teleport=True), "unknown capability"),
    (lambda m: m.update(modes=["push", "carrier_pigeon"]), "unknown mode"),
    (lambda m: m.update(modes=[]), "at least one of"),
    (lambda m: m.update(modes=["push", "push"]), "modes repeat"),
    (lambda m: m.update(modes=["poll"]), "mode poll needs capabilities.poll"),
    (lambda m: m.update(modes=["stream"]), "mode stream needs capabilities.stream"),
    (lambda m: m.update(ack={"success_status": 202}), "ack.deadline_ms"),
    (lambda m: m.update(ack={"deadline_ms": 3000, "success_status": 404}), "2xx"),
    (lambda m: m.update(ack={"deadline_ms": 3000, "colour": 1}), "ack: unknown field"),
    (lambda m: m.update(dedupe=["serial"]), "dedupe field 'serial'"),
    (lambda m: m.update(dedupe=[]), "'dedupe' names at least one"),
    (lambda m: m.update(default_hosts=["not a host!"]), "is not a host name"),
    (lambda m: m["auth_fields"].append({"name": "signing_secret", "label": "again"}), "names repeat"),
    (lambda m: m.update(auth_fields=[{"name": "team", "label": "Team"}]), "needs a secret auth field"),
    (lambda m: m.update(self_identity=["Bot-Login"]), "self_identity entry"),
    (lambda m: m.update(event_types=["Com.Github"]), "event_types entry"),
    (lambda m: m["capabilities"].update(stream_reply=True), "stream_reply needs reply"),
    (lambda m: m.update(key="Web Hook"), "'key' must be lowercase"),
])
def test_manifest_schema_refuses(mutate, says):
    manifest = copy.deepcopy(WEBHOOK_MANIFEST)
    mutate(manifest)
    with pytest.raises(ConnectorManifestError) as raised:
        validate(manifest, where="webhook")
    assert says in str(raised.value)


def test_a_manifest_must_be_a_mapping_and_its_key_must_match_its_folder(tmp_path):
    with pytest.raises(ConnectorManifestError, match="is a mapping"):
        validate(["not", "a", "mapping"])
    folder = tmp_path / "other"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(yaml.safe_dump(WEBHOOK_MANIFEST))
    (folder / "adapter.py").write_text("")
    with pytest.raises(ConnectorManifestError, match="must match its folder"):
        load_folder(folder)
    with pytest.raises(ConnectorManifestError, match="no manifest.yaml"):
        load_folder(tmp_path / "missing")


# ---- the two-way capability check --------------------------------------------------------------


class _Half(IntakeAdapterBase):
    key = "half"

    def verify(self, conn, req, now):          # verify is two hooks: this alone is not "verify"
        return None


class _Verifier(_Half):
    def dedupe_key(self, conn, req):
        return "x"


class _Talker(_Verifier):
    def parse(self, conn, raw):
        return []

    def send(self, conn, op, secrets):
        return None


def _manifest(**caps):
    raw = {"key": "half", "label": "Half", "icon": "x", "description": "d", "adapter": "_Half",
           "modes": ["push"], "ack": {"deadline_ms": 3000},
           "dedupe": ["delivery_id"],
           "auth_fields": [{"name": "signing_secret", "label": "Secret", "secret": True}],
           "capabilities": {c: False for c in CAPABILITIES} | {"verify": True, "parse": True} | caps,
           "default_hosts": ["half.example"]}
    return validate(raw, where="half")


def test_an_adapter_must_implement_exactly_what_its_manifest_claims():
    with pytest.raises(ConnectorManifestError, match="claims 'verify' but does not implement"):
        check_adapter(_manifest(), _Half)
    with pytest.raises(ConnectorManifestError, match="claims 'parse'"):
        check_adapter(_manifest(), _Verifier)
    with pytest.raises(ConnectorManifestError, match="implements 'reply' but the manifest says false"):
        check_adapter(_manifest(), _Talker)
    check_adapter(_manifest(reply=True), _Talker)       # exactly right: no error


def test_half_implemented_capability_is_named():
    class _PartPoller(_Talker):
        def subscribe(self, conn, spec):
            return None

    with pytest.raises(ConnectorManifestError, match="implements part of 'subscribe'"):
        check_adapter(_manifest(reply=True), _PartPoller)


def test_a_non_base_class_is_refused():
    class Loose:
        key = "half"

    with pytest.raises(ConnectorManifestError, match="must extend IntakeAdapterBase"):
        check_adapter(_manifest(), Loose)


def test_every_hook_the_contract_names_is_refused_by_the_base_until_implemented():
    adapter = _Talker()
    adapter.key = "half"
    adapter.intake_capabilities = {c: False for c in CAPABILITIES}
    conn = IntakeConnection("icn_x", "p", "half")
    for hook in HOOKS:
        if hook == "handshake" or implements(_Talker, hook):
            continue        # handshake has a soft default (None), checked below
        with pytest.raises(CapabilityNotSupported) as raised:
            getattr(adapter, hook)(conn)
        assert raised.value.capability in CAPABILITIES and raised.value.operation == hook
    assert set(MODES) == {"push", "poll", "subscribe", "socket", "stream"}


def test_the_base_defaults_are_safe():
    adapter = _Talker()
    adapter.key = "half"
    adapter.manifest = _manifest(reply=True)
    adapter.intake_capabilities = dict(adapter.manifest.capabilities)
    conn = IntakeConnection("icn_x", "p", "half", config={"bot_user_id": "B1"})
    req = RawRequest(method="POST", headers={}, body=b"{}")
    assert adapter.ack(conn, req, "received").status == 202
    assert adapter.handshake(conn, req) is None
    ev = IntakeEvent(id="1", source="half://x", type="com.half.thing", bossactor=ExternalActor(
        kind="bot", external_id="B1"))
    assert adapter.is_self(conn, ev) is False       # no self_identity declared in the manifest
    assert adapter.enrich(conn, ev) is ev


# ---- the envelope and the connection -----------------------------------------------------------


def test_the_envelope_round_trips_and_screens_only_what_an_outsider_wrote():
    ev = IntakeEvent(id="Ev1", source="slack://T1", type="com.slack.message.app_mention",
                     subject="C1/1.2", time="2026-11-14T09:14:02+00:00",
                     data={"text": "@BOSS why?", "mentions": ["U1"], "fields": {"k": "v", "n": 1}},
                     bossactor=ExternalActor(kind="user", external_id="U4", display="Priya"),
                     bossconnection="icn_1", bossdedupe="slack:T1:Ev1")
    as_dict = ev.to_dict()
    assert as_dict["specversion"] == "1.0" and as_dict["bossactor"] == {
        "kind": "user", "external_id": "U4", "display": "Priya"}
    back = IntakeEvent.from_dict(as_dict)
    assert back.to_dict() == as_dict
    assert ev.text_fields() == {"text": "@BOSS why?", "fields": {"k": "v"}, "event_subject": "C1/1.2"}
    assert ev.untrusted is False
    ev.bossscreen = {"verdict": "suspicious", "rules": ["x"], "mode": "patterns"}
    assert ev.untrusted is True


def test_a_connection_never_prints_a_secret():
    conn = IntakeConnection("icn_1", "p_1", "webhook", config={"team_id": "T1"},
                            secret_resolver=lambda name: "whsec_SUPER" if name == "signing_secret"
                            else None)
    shown = repr(conn) + str(conn) + repr(conn.secrets) + str(conn.secrets)
    assert "SUPER" not in shown and "whsec" not in shown
    assert conn.secret("signing_secret") == "whsec_SUPER" and conn.secret("other") is None
    assert conn.source_uri == "webhook://T1"


def test_kept_headers_never_carry_a_signature_or_a_credential():
    kept = kept_headers({"Authorization": "Bearer x", "X-Hub-Signature-256": "sha256=abc",
                         "Webhook-Signature": "v1,zzz", "X-GitHub-Delivery": "d1",
                         "X-Slack-Retry-Num": "2", "Cookie": "s=1"})
    assert kept == {"x-github-delivery": "d1", "x-slack-retry-num": "2"}
