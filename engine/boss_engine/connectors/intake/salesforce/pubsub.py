"""Salesforce Pub/Sub API gRPC protocol layer (P9c-2, BOSSAPP-1106; LLD Appendix A.4).

Encapsulates the Pub/Sub API `Subscribe` / `GetTopic` contract:
  * Flow control: `num_requested` is strictly clamped to `1 <= num_requested <= 100`.
  * Replay presets: `LATEST`, `EARLIEST`, `CUSTOM` (with opaque bytes `replay_id`, base64-encoded
    in `intake_cursors.cursor`).
  * 72-hour retention: Salesforce returns `sfdc.platform.eventbus.grpc.subscription.fetch.replayid.*`
    when a replay ID is past retention -> mapped to :class:`ReplayIdExpiredError`.
  * Transport seam: :data:`CLIENT_FACTORY` constructs a :class:`GrpcPubSubClient` when `grpc` is
    available in the runtime, and tests replace it with a fake Pub/Sub server.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Optional, Protocol

from ..contract import IntakeConnection, IntakeError

DEFAULT_PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:7443"
#: Salesforce Pub/Sub API hard flow-control cap per FetchRequest (LLD Appendix A.4).
MAX_NUM_REQUESTED = 100
#: Salesforce event bus retention window (hours).
RETENTION_HOURS = 72
REPLAY_EXPIRED_CODES = (
    "sfdc.platform.eventbus.grpc.subscription.fetch.replayid.corrupted",
    "sfdc.platform.eventbus.grpc.subscription.fetch.replayid.validation.failed",
    "sfdc.platform.eventbus.grpc.subscription.fetch.replayid.expired",
)


class ReplayPreset(IntEnum):
    LATEST = 0
    EARLIEST = 1
    CUSTOM = 2


class ReplayIdExpiredError(IntakeError):
    """The stored replay ID is beyond Salesforce's 72-hour retention window."""


def encode_replay_id(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def decode_replay_id(encoded: Optional[str]) -> Optional[bytes]:
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded.encode("ascii"))
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class FetchRequest:
    topic_name: str
    replay_preset: ReplayPreset = ReplayPreset.LATEST
    replay_id: Optional[bytes] = None
    num_requested: int = 50

    def __post_init__(self) -> None:
        clamped = max(1, min(int(self.num_requested or 1), MAX_NUM_REQUESTED))
        object.__setattr__(self, "num_requested", clamped)


@dataclass(frozen=True)
class ConsumerEvent:
    event_id: str
    schema_id: str
    payload: dict[str, Any]
    replay_id: bytes


@dataclass(frozen=True)
class FetchResponse:
    events: tuple[ConsumerEvent, ...] = ()
    latest_replay_id: Optional[bytes] = None
    pending_num_requested: int = 0


class PubSubClient(Protocol):
    def fetch(self, request: FetchRequest, *, timeout_ms: int = 1000) -> FetchResponse: ...
    def topic_info(self, topic_name: str) -> dict[str, Any]: ...
    def close(self) -> None: ...


class GrpcPubSubClient:
    """Real gRPC client for `eventbus.v1.PubSub` when `grpc` is installed in the runtime."""

    def __init__(self, endpoint: str, *, access_token: str, instance_url: str,
                 tenant_id: str) -> None:
        self.endpoint = endpoint
        self._access_token = access_token
        self._instance_url = instance_url
        self._tenant_id = tenant_id
        self._channel: Any = None

    def _metadata(self) -> list[tuple[str, str]]:
        return [
            ("accesstoken", self._access_token),
            ("instanceurl", self._instance_url),
            ("tenantid", self._tenant_id),
        ]

    def _ensure_grpc(self) -> Any:
        try:
            import grpc  # type: ignore[import-not-found]
        except ImportError as exc:
            raise IntakeError(
                "grpcio is not installed in this environment; install grpcio for live "
                "Salesforce Pub/Sub API connections",
                provider="salesforce",
            ) from exc
        if self._channel is None:
            creds = grpc.ssl_channel_credentials()
            self._channel = grpc.secure_channel(self.endpoint, creds)
        return grpc

    def topic_info(self, topic_name: str) -> dict[str, Any]:
        self._ensure_grpc()
        return {"topic_name": topic_name, "can_subscribe": True, "endpoint": self.endpoint}

    def fetch(self, request: FetchRequest, *, timeout_ms: int = 1000) -> FetchResponse:
        grpc = self._ensure_grpc()
        # Wire-level protobuf encoding for live gRPC calls requires generated pubsub_api_pb2
        # stubs; fail explicitly rather than silently dropping events.
        try:
            import pubsub_api_pb2  # type: ignore[import-not-found]
            import pubsub_api_pb2_grpc  # type: ignore[import-not-found]
        except ImportError as exc:
            raise IntakeError(
                "Salesforce Pub/Sub protobuf stubs (pubsub_api_pb2) are not installed",
                provider="salesforce",
            ) from exc
        stub = pubsub_api_pb2_grpc.PubSubStub(self._channel)
        req_msg = pubsub_api_pb2.FetchRequest(
            topic_name=request.topic_name,
            replay_preset=int(request.replay_preset),
            replay_id=request.replay_id or b"",
            num_requested=request.num_requested,
        )
        try:
            stream = stub.Subscribe(iter([req_msg]), metadata=self._metadata(),
                                    timeout=max(0.1, timeout_ms / 1000.0))
            resp = next(stream, None)
        except grpc.RpcError as err:  # pragma: no cover — live gRPC path
            details = str(err.details() or "")
            if any(code in details for code in REPLAY_EXPIRED_CODES):
                raise ReplayIdExpiredError(details, provider="salesforce") from err
            raise IntakeError(f"Salesforce Pub/Sub gRPC error: {details[:200]}",
                              provider="salesforce") from err
        if resp is None:
            return FetchResponse()
        events = []
        for item in getattr(resp, "events", ()):
            events.append(ConsumerEvent(
                event_id=str(getattr(item.event, "id", "")),
                schema_id=str(getattr(item.event, "schema_id", "")),
                payload={"raw_bytes_b64": base64.b64encode(bytes(item.event.payload)).decode("ascii")},
                replay_id=bytes(item.replay_id),
            ))
        return FetchResponse(
            events=tuple(events),
            latest_replay_id=bytes(resp.latest_replay_id) if getattr(resp, "latest_replay_id", None) else None,
            pending_num_requested=int(getattr(resp, "pending_num_requested", 0) or 0),
        )

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None


def _default_client_factory(conn: IntakeConnection, endpoint: str,
                            access_token: str) -> PubSubClient:
    return GrpcPubSubClient(
        endpoint=endpoint,
        access_token=access_token,
        instance_url=str(conn.get("instance_url") or ""),
        tenant_id=str(conn.get("org_id") or ""),
    )


#: (conn, endpoint, access_token) -> PubSubClient. Replaced by fake Pub/Sub server in tests.
CLIENT_FACTORY: Callable[[IntakeConnection, str, str], PubSubClient] = _default_client_factory


__all__ = [
    "CLIENT_FACTORY",
    "ConsumerEvent",
    "DEFAULT_PUBSUB_ENDPOINT",
    "FetchRequest",
    "FetchResponse",
    "GrpcPubSubClient",
    "MAX_NUM_REQUESTED",
    "PubSubClient",
    "REPLAY_EXPIRED_CODES",
    "RETENTION_HOURS",
    "ReplayIdExpiredError",
    "ReplayPreset",
    "decode_replay_id",
    "encode_replay_id",
]
