"""Portable, canonical capability bundles with detached Ed25519 trust evidence."""
from __future__ import annotations

import base64
import json
from hashlib import sha256
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event
from .entitlements import EntitlementBrokerService, SkillNotFoundError
from .mcp_registry import (
    ensure_mcp_server,
    list_mcp_servers,
    set_mcp_enabled,
)
from .skill_registry import disable_skill, enable_skill, ensure_skill


BUNDLE_FORMAT = "boss.capability-bundle/v1"
MAX_BUNDLE_BYTES = 1_048_576


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json_text(payload: Any) -> str:
    return canonical_json_bytes(payload).decode("utf-8") + "\n"


class CapabilityBundleError(ValueError):
    pass


class CapabilityBundleService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.entitlements = EntitlementBrokerService(settings)

    def _trust_keys(self) -> dict[str, Ed25519PublicKey]:
        raw = self.settings.skill_trust_keys.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CapabilityBundleError(
                "BOSS_SKILL_TRUST_KEYS must be a JSON object keyed by key id"
            ) from exc
        if not isinstance(parsed, dict):
            raise CapabilityBundleError(
                "BOSS_SKILL_TRUST_KEYS must be a JSON object keyed by key id"
            )
        keys: dict[str, Ed25519PublicKey] = {}
        for key_id, encoded in parsed.items():
            if not isinstance(key_id, str) or not isinstance(encoded, str):
                raise CapabilityBundleError("trust key ids and values must be strings")
            keys[key_id] = self._load_public_key(encoded)
        return keys

    @staticmethod
    def _load_public_key(encoded: str) -> Ed25519PublicKey:
        value = encoded.strip()
        try:
            if value.startswith("-----BEGIN"):
                key = serialization.load_pem_public_key(value.encode("utf-8"))
            else:
                key = Ed25519PublicKey.from_public_bytes(base64.b64decode(value))
        except Exception as exc:  # noqa: BLE001 - normalize secret configuration error
            raise CapabilityBundleError("invalid Ed25519 public trust key") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise CapabilityBundleError("trust key is not Ed25519")
        return key

    def _load_private_key(self) -> Optional[Ed25519PrivateKey]:
        value = self.settings.skill_signing_private_key.strip()
        if not value:
            return None
        try:
            if value.startswith("-----BEGIN"):
                key = serialization.load_pem_private_key(
                    value.encode("utf-8"), password=None
                )
            else:
                key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(value))
        except Exception as exc:  # noqa: BLE001 - do not echo signing material
            raise CapabilityBundleError("invalid Ed25519 signing key") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise CapabilityBundleError("signing key is not Ed25519")
        return key

    @staticmethod
    def _body(
        kind: str,
        identifier: str,
        payload: dict[str, Any],
        publisher: dict[str, Any],
        version: str,
        provenance: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "format": BUNDLE_FORMAT,
            "kind": kind,
            "id": identifier,
            "version": version,
            "payload": payload,
            "publisher": publisher,
            "provenance": provenance,
        }

    def export_bundle(
        self,
        *,
        kind: str,
        identifier: str,
        payload: dict[str, Any],
        publisher: Optional[dict[str, Any]] = None,
        version: str = "1",
        provenance: Optional[dict[str, Any]] = None,
    ) -> str:
        if kind not in {"skill", "mcp"}:
            raise CapabilityBundleError("bundle kind must be skill or mcp")
        signed_provenance = {
            "source": (provenance or {}).get("source", "local_export"),
            "version": (provenance or {}).get("version", version),
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in signed_provenance.values()
        ):
            raise CapabilityBundleError(
                "bundle provenance source and version must be non-empty strings"
            )
        body = self._body(
            kind,
            identifier,
            payload,
            publisher or {},
            version,
            signed_provenance,
        )
        checksum = sha256(canonical_json_bytes(body)).hexdigest()
        bundle: dict[str, Any] = {
            **body,
            "provenance": {**signed_provenance, "checksum": checksum},
        }
        private_key = self._load_private_key()
        if private_key is not None:
            key_id = self.settings.skill_signing_key_id.strip()
            if not key_id:
                raise CapabilityBundleError(
                    "BOSS_SKILL_SIGNING_KEY_ID is required when signing is configured"
                )
            bundle["signature"] = {
                "algorithm": "Ed25519",
                "key_id": key_id,
                "value": base64.b64encode(
                    private_key.sign(checksum.encode("ascii"))
                ).decode("ascii"),
            }
        return canonical_json_text(bundle)

    def verify_bundle(
        self,
        bundle_text: str,
        *,
        allow_untrusted: bool = False,
        expected_kind: Optional[str] = None,
    ) -> dict[str, Any]:
        if not isinstance(bundle_text, str):
            raise CapabilityBundleError("bundle must be a JSON string")
        if len(bundle_text.encode("utf-8")) > MAX_BUNDLE_BYTES:
            raise CapabilityBundleError("bundle exceeds the 1 MiB local import limit")
        try:
            bundle = json.loads(bundle_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CapabilityBundleError("bundle must be valid JSON") from exc
        if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
            raise CapabilityBundleError(f"bundle format must be {BUNDLE_FORMAT}")
        kind = bundle.get("kind")
        if kind not in {"skill", "mcp"} or (expected_kind and kind != expected_kind):
            raise CapabilityBundleError(
                f"bundle kind must be {expected_kind or 'skill or mcp'}"
            )
        identifier = bundle.get("id")
        payload = bundle.get("payload")
        publisher = bundle.get("publisher") or {}
        version = bundle.get("version") or "1"
        if not isinstance(identifier, str) or not identifier.strip():
            raise CapabilityBundleError("bundle id must be a non-empty string")
        if not isinstance(payload, dict) or not isinstance(publisher, dict):
            raise CapabilityBundleError("bundle payload and publisher must be objects")
        provenance = bundle.get("provenance") or {}
        if not isinstance(provenance, dict):
            raise CapabilityBundleError("bundle provenance must be an object")
        signed_provenance = {
            "source": provenance.get("source", "local_export"),
            "version": provenance.get("version", str(version)),
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in signed_provenance.values()
        ):
            raise CapabilityBundleError(
                "bundle provenance source and version must be non-empty strings"
            )
        body = self._body(
            kind,
            identifier,
            payload,
            publisher,
            str(version),
            signed_provenance,
        )
        checksum = sha256(canonical_json_bytes(body)).hexdigest()
        if provenance.get("checksum") != checksum:
            raise CapabilityBundleError("bundle checksum does not match its contents")

        trusted = False
        signature = bundle.get("signature")
        key_id: Optional[str] = None
        if signature is not None:
            if not isinstance(signature, dict) or signature.get("algorithm") != "Ed25519":
                raise CapabilityBundleError("unsupported capability bundle signature")
            key_id = signature.get("key_id")
            value = signature.get("value")
            if not isinstance(key_id, str) or not isinstance(value, str):
                raise CapabilityBundleError("signature key_id and value are required")
            public_key = self._trust_keys().get(key_id)
            if public_key is not None:
                try:
                    public_key.verify(
                        base64.b64decode(value), checksum.encode("ascii")
                    )
                except (InvalidSignature, ValueError) as exc:
                    raise CapabilityBundleError("bundle signature verification failed") from exc
                trusted = True
        if not trusted and not allow_untrusted:
            raise CapabilityBundleError(
                "bundle is unsigned or its signing key is not trusted; "
                "set allow_untrusted=true for an explicit override"
            )
        return {
            **bundle,
            "payload": payload,
            "provenance": {
                **provenance,
                "trusted": trusted,
                "key_id": key_id,
            },
        }

    def export_skill(
        self, connection: Connection, project_id: str, skill_id: str
    ) -> str:
        skill = next(
            (
                item
                for item in self.entitlements.list_skills(
                    project_id, connection=connection
                )
                if item["id"] == skill_id
            ),
            None,
        )
        if skill is None:
            raise SkillNotFoundError(f"Skill '{skill_id}' not found")
        payload = {
            key: skill[key]
            for key in (
                "id", "kind", "system", "load", "risk_tier", "description",
                "mock_mode", "input_schema", "output_schema", "cost", "retry",
                "permissions", "data_boundary", "policy",
            )
        }
        return self.export_bundle(
            kind="skill",
            identifier=skill_id,
            payload=payload,
            publisher=skill.get("publisher") or {},
            provenance=skill.get("provenance") or {},
        )

    def import_skill(
        self,
        connection: Connection,
        project_id: str,
        bundle_text: str,
        *,
        allow_untrusted: bool,
        enabled: bool,
        on_demand: Optional[bool],
        actor: str,
    ) -> dict[str, Any]:
        bundle = self.verify_bundle(
            bundle_text, allow_untrusted=allow_untrusted, expected_kind="skill"
        )
        skill = bundle["payload"]
        skill_id = str(bundle["id"])
        if skill.get("id") != skill_id:
            raise CapabilityBundleError("SkillSpec id must match bundle id")
        if skill.get("system") is True:
            raise CapabilityBundleError("imported project skills cannot be system skills")
        self.entitlements.validate_skill_spec(skill, "capability bundle")
        item = ensure_skill(
            connection,
            project_id,
            skill_id,
            source="tenant",
            enabled=enabled,
            on_demand=(
                bool(on_demand)
                if on_demand is not None
                else skill.get("load") == "on_demand"
            ),
            config={"skill_spec": skill},
            publisher=bundle.get("publisher") or {},
            provenance=bundle["provenance"],
            actor=actor,
        )
        item = (
            enable_skill(
                connection, project_id, skill_id, actor, on_demand=on_demand
            )
            if enabled
            else disable_skill(connection, project_id, skill_id, actor)
        ) or item
        emit_event(
            connection,
            project_id,
            "skill.imported",
            actor,
            "project_skill",
            item["id"],
            {
                "skill_id": skill_id,
                "trusted": bool(bundle["provenance"].get("trusted")),
                "checksum": bundle["provenance"]["checksum"],
            },
        )
        return item

    def export_mcp(
        self, connection: Connection, project_id: str, server_id: str
    ) -> str:
        server = next(
            (
                item
                for item in list_mcp_servers(connection, project_id)
                if item["id"] == server_id
            ),
            None,
        )
        if server is None:
            raise CapabilityBundleError("MCP server not found")
        payload = {
            key: server[key]
            for key in (
                "name", "url", "transport", "risk_tier", "data_boundary",
                "description", "tool_risk_overrides",
            )
        }
        return self.export_bundle(
            kind="mcp",
            identifier=server["name"],
            payload=payload,
            publisher=server.get("publisher") or {},
            provenance=server.get("provenance") or {},
        )

    def import_mcp(
        self,
        connection: Connection,
        project_id: str,
        bundle_text: str,
        *,
        allow_untrusted: bool,
        enabled: bool,
        actor: str,
    ) -> dict[str, Any]:
        bundle = self.verify_bundle(
            bundle_text, allow_untrusted=allow_untrusted, expected_kind="mcp"
        )
        payload = bundle["payload"]
        if payload.get("name") != bundle["id"]:
            raise CapabilityBundleError("MCP server name must match bundle id")
        for field in ("name", "url", "transport"):
            if not isinstance(payload.get(field), str) or not payload[field].strip():
                raise CapabilityBundleError(
                    f"MCP bundle payload.{field} must be a non-empty string"
                )
        risk = payload.get("risk_tier", 1)
        overrides = payload.get("tool_risk_overrides") or {}
        if not isinstance(risk, int) or risk < 0 or risk > 3:
            raise CapabilityBundleError("MCP risk_tier must be between 0 and 3")
        if not isinstance(overrides, dict) or any(
            not isinstance(name, str)
            or not isinstance(value, int)
            or value < 0
            or value > 3
            for name, value in overrides.items()
        ):
            raise CapabilityBundleError("MCP tool risk overrides must be integers 0-3")
        server = ensure_mcp_server(
            connection,
            project_id,
            str(payload["name"]),
            str(payload["url"]),
            transport=str(payload["transport"]),
            risk_tier=risk,
            data_boundary=str(payload.get("data_boundary") or "internal"),
            description=payload.get("description"),
            publisher=bundle.get("publisher") or {},
            provenance=bundle["provenance"],
            tool_risk_overrides=overrides,
        )
        if not enabled:
            server = set_mcp_enabled(
                connection, project_id, server["id"], False, actor
            ) or server
        emit_event(
            connection,
            project_id,
            "mcp.imported",
            actor,
            "mcp_server",
            server["id"],
            {
                "name": server["name"],
                "trusted": bool(bundle["provenance"].get("trusted")),
                "checksum": bundle["provenance"]["checksum"],
            },
        )
        return server
