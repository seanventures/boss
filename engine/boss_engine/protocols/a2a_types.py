"""THE A2A v1.0 DATA MODEL: BOSS's ONE ASYNC-TASK SHAPE (AP-P5, BOSSAPP-1056; AGENT_PLATFORM §4.5).

Source of truth: the A2A v1.0 specification (https://a2a-protocol.org/latest/specification/) and
its normative proto (`lf.a2a.v1`, specification/a2a.proto). JSON follows ProtoJSON: camelCase
field names, enums by their full names (`TASK_STATE_INPUT_REQUIRED`, `ROLE_USER`), a Part is ONE
of `text | raw | url | data` with no `kind` discriminator.

Who uses it:

  * the A2A server (`services/a2a_server.py`): a deployed agent's run IS a Task — contextId is the
    thread, the Task id is the run, `TASK_STATE_INPUT_REQUIRED` is an open interrupt (a Decision);
  * the A2A client (`services/a2a_client.py`): what a remote agent returns, parsed here;
  * async sub-agents (AP-P7): `start/check/update/cancel` over child runs return these Tasks, so a
    local child and a remote A2A agent look the same to the parent.

Parsing is lenient where v0.3 peers differ (`kind` on parts, lower-case kebab states such as
`input-required`, roles `user`/`agent`); emitting is always v1.0. Pure: no database, no I/O.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

A2A_VERSION = "1.0"
#: The media type v1.0 sends push notifications and HTTP+JSON bodies with.
A2A_MEDIA_TYPE = "application/a2a+json"


class TaskState(str, enum.Enum):
    UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def interrupted(self) -> bool:
        """Waiting on the client (a person's answer, or credentials): the stream closes here."""
        return self in INTERRUPTED_STATES

    @classmethod
    def parse(cls, value: Any) -> "TaskState":
        """v1.0 names, and the v0.3 kebab forms (`input-required`, `canceled`)."""
        if isinstance(value, TaskState):
            return value
        raw = str(value or "").strip()
        try:
            return cls(raw)
        except ValueError:
            pass
        key = raw.lower().replace("-", "_")
        if key.startswith("task_state_"):
            key = key[len("task_state_"):]
        legacy = {"submitted": cls.SUBMITTED, "working": cls.WORKING,
                  "completed": cls.COMPLETED, "failed": cls.FAILED, "canceled": cls.CANCELED,
                  "cancelled": cls.CANCELED, "input_required": cls.INPUT_REQUIRED,
                  "rejected": cls.REJECTED, "auth_required": cls.AUTH_REQUIRED}
        return legacy.get(key, cls.UNSPECIFIED)


TERMINAL_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED,
                             TaskState.REJECTED})
INTERRUPTED_STATES = frozenset({TaskState.INPUT_REQUIRED, TaskState.AUTH_REQUIRED})

#: BOSS run status (the Agent Protocol names, services/agent_protocol.STATUS) → A2A TaskState.
RUN_STATUS_TO_STATE: dict = {
    "pending": TaskState.SUBMITTED,
    "running": TaskState.WORKING,
    "interrupted": TaskState.INPUT_REQUIRED,
    "success": TaskState.COMPLETED,
    "error": TaskState.FAILED,
    "cancelled": TaskState.CANCELED,
    "refused": TaskState.REJECTED,
}


def state_for_run_status(status: Any) -> TaskState:
    """The TaskState a BOSS run status is (raw agent_runs statuses are normalised first)."""
    s = str(status or "")
    if s in RUN_STATUS_TO_STATE:
        return RUN_STATUS_TO_STATE[s]
    raw = {"queued": "pending", "drained": "pending", "budget_wait": "pending",
           "awaiting_approval": "interrupted", "completed": "success", "failed": "error",
           "denied": "error", "rolled_back": "cancelled", "canceled": "cancelled"}
    return RUN_STATUS_TO_STATE.get(raw.get(s, ""), TaskState.UNSPECIFIED)


class Role(str, enum.Enum):
    UNSPECIFIED = "ROLE_UNSPECIFIED"
    USER = "ROLE_USER"
    AGENT = "ROLE_AGENT"

    @classmethod
    def parse(cls, value: Any) -> "Role":
        if isinstance(value, Role):
            return value
        raw = str(value or "").strip()
        try:
            return cls(raw)
        except ValueError:
            return {"user": cls.USER, "agent": cls.AGENT}.get(raw.lower(), cls.UNSPECIFIED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


class A2AShapeError(ValueError):
    """A payload that is not a valid A2A object (maps to JSON-RPC -32602 invalid params)."""


@dataclass
class Part:
    """Exactly one of text, raw (base64 in JSON), url, data."""
    text: Optional[str] = None
    raw: Optional[str] = None
    url: Optional[str] = None
    data: Any = None
    metadata: Optional[dict] = None
    filename: Optional[str] = None
    media_type: Optional[str] = None

    def __post_init__(self) -> None:
        present = [k for k in ("text", "raw", "url", "data") if getattr(self, k) is not None]
        if len(present) != 1:
            raise A2AShapeError(f"a Part holds exactly one of text|raw|url|data (got {present})")

    def to_dict(self) -> dict:
        return _drop_empty({"text": self.text, "raw": self.raw, "url": self.url,
                            "data": self.data, "metadata": self.metadata,
                            "filename": self.filename, "mediaType": self.media_type})

    @classmethod
    def from_dict(cls, d: Mapping) -> "Part":
        if not isinstance(d, Mapping):
            raise A2AShapeError("a Part must be an object")
        file = d.get("file") if isinstance(d.get("file"), Mapping) else {}
        return cls(text=d.get("text"), raw=d.get("raw") or file.get("bytes"),
                   url=d.get("url") or file.get("uri"), data=d.get("data"),
                   metadata=d.get("metadata"),
                   filename=d.get("filename") or file.get("name"),
                   media_type=d.get("mediaType") or file.get("mimeType"))


def text_part(text: str) -> Part:
    return Part(text=str(text))


def data_part(data: Any) -> Part:
    return Part(data=data, media_type="application/json")


@dataclass
class Message:
    role: Role
    parts: list
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    context_id: Optional[str] = None
    task_id: Optional[str] = None
    metadata: Optional[dict] = None
    extensions: Optional[list] = None
    reference_task_ids: Optional[list] = None

    def text(self) -> str:
        """The message's text parts joined (what a run's input or a reply says)."""
        return "\n".join(p.text for p in self.parts if p.text is not None)

    def data(self) -> list:
        return [p.data for p in self.parts if p.data is not None]

    def to_dict(self) -> dict:
        return _drop_empty({"messageId": self.message_id, "contextId": self.context_id,
                            "taskId": self.task_id, "role": Role.parse(self.role).value,
                            "parts": [p.to_dict() for p in self.parts],
                            "metadata": self.metadata, "extensions": self.extensions,
                            "referenceTaskIds": self.reference_task_ids})

    @classmethod
    def from_dict(cls, d: Mapping) -> "Message":
        if not isinstance(d, Mapping):
            raise A2AShapeError("a Message must be an object")
        parts = d.get("parts")
        if not isinstance(parts, list) or not parts:
            raise A2AShapeError("a Message needs at least one part")
        return cls(role=Role.parse(d.get("role")), parts=[Part.from_dict(p) for p in parts],
                   message_id=str(d.get("messageId") or f"msg_{uuid.uuid4().hex}"),
                   context_id=d.get("contextId"), task_id=d.get("taskId"),
                   metadata=d.get("metadata"), extensions=d.get("extensions"),
                   reference_task_ids=d.get("referenceTaskIds"))


def agent_message(text: str, *, context_id: Optional[str] = None,
                  task_id: Optional[str] = None, data: Any = None) -> Message:
    parts = [text_part(text)] + ([data_part(data)] if data is not None else [])
    return Message(role=Role.AGENT, parts=parts, context_id=context_id, task_id=task_id)


def user_message(text: str, *, context_id: Optional[str] = None,
                 task_id: Optional[str] = None, data: Any = None) -> Message:
    parts = [text_part(text)] + ([data_part(data)] if data is not None else [])
    return Message(role=Role.USER, parts=parts, context_id=context_id, task_id=task_id)


@dataclass
class Artifact:
    parts: list
    artifact_id: str = field(default_factory=lambda: f"art_{uuid.uuid4().hex}")
    name: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[dict] = None
    extensions: Optional[list] = None

    def to_dict(self) -> dict:
        return _drop_empty({"artifactId": self.artifact_id, "name": self.name,
                            "description": self.description,
                            "parts": [p.to_dict() for p in self.parts],
                            "metadata": self.metadata, "extensions": self.extensions})

    @classmethod
    def from_dict(cls, d: Mapping) -> "Artifact":
        return cls(parts=[Part.from_dict(p) for p in d.get("parts") or []],
                   artifact_id=str(d.get("artifactId") or f"art_{uuid.uuid4().hex}"),
                   name=d.get("name"), description=d.get("description"),
                   metadata=d.get("metadata"), extensions=d.get("extensions"))


@dataclass
class TaskStatus:
    state: TaskState
    message: Optional[Message] = None
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return _drop_empty({"state": TaskState.parse(self.state).value,
                            "message": self.message.to_dict() if self.message else None,
                            "timestamp": self.timestamp})

    @classmethod
    def from_dict(cls, d: Mapping) -> "TaskStatus":
        msg = d.get("message")
        return cls(state=TaskState.parse(d.get("state")),
                   message=Message.from_dict(msg) if isinstance(msg, Mapping) else None,
                   timestamp=str(d.get("timestamp") or _now()))


@dataclass
class Task:
    """A unit of work: in BOSS, one run. `context_id` is its thread."""
    id: str
    status: TaskStatus
    context_id: Optional[str] = None
    artifacts: list = field(default_factory=list)
    history: list = field(default_factory=list)
    metadata: Optional[dict] = None

    @property
    def state(self) -> TaskState:
        return self.status.state

    def to_dict(self, *, history_length: Optional[int] = None) -> dict:
        hist = self.history
        if history_length is not None:
            hist = hist[-history_length:] if history_length > 0 else []
        return _drop_empty({"id": self.id, "contextId": self.context_id,
                            "status": self.status.to_dict(),
                            "artifacts": [a.to_dict() for a in self.artifacts],
                            "history": [m.to_dict() for m in hist],
                            "metadata": self.metadata})

    @classmethod
    def from_dict(cls, d: Mapping) -> "Task":
        if not isinstance(d, Mapping) or not d.get("id"):
            raise A2AShapeError("a Task needs an id")
        return cls(id=str(d["id"]), status=TaskStatus.from_dict(d.get("status") or {}),
                   context_id=d.get("contextId"),
                   artifacts=[Artifact.from_dict(a) for a in d.get("artifacts") or []],
                   history=[Message.from_dict(m) for m in d.get("history") or []],
                   metadata=d.get("metadata"))


@dataclass
class TaskStatusUpdateEvent:
    task_id: str
    context_id: str
    status: TaskStatus
    metadata: Optional[dict] = None

    def to_dict(self) -> dict:
        return _drop_empty({"taskId": self.task_id, "contextId": self.context_id,
                            "status": self.status.to_dict(), "metadata": self.metadata})


@dataclass
class TaskArtifactUpdateEvent:
    task_id: str
    context_id: str
    artifact: Artifact
    append: bool = False
    last_chunk: bool = False
    metadata: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {"taskId": self.task_id, "contextId": self.context_id,
             "artifact": self.artifact.to_dict(), "metadata": self.metadata}
        if self.append:
            d["append"] = True
        if self.last_chunk:
            d["lastChunk"] = True
        return _drop_empty(d)


@dataclass
class AuthenticationInfo:
    scheme: str
    credentials: Optional[str] = None

    def to_dict(self) -> dict:
        return _drop_empty({"scheme": self.scheme, "credentials": self.credentials})


@dataclass
class TaskPushNotificationConfig:
    url: str
    id: Optional[str] = None
    task_id: Optional[str] = None
    token: Optional[str] = None
    authentication: Optional[AuthenticationInfo] = None
    tenant: Optional[str] = None

    def to_dict(self, *, redact: bool = True) -> dict:
        """`redact` (the default) never echoes the token or credentials back to a caller."""
        auth = self.authentication
        return _drop_empty({
            "tenant": self.tenant, "id": self.id, "taskId": self.task_id, "url": self.url,
            "token": None if redact else self.token,
            "authentication": ({"scheme": auth.scheme} if redact else auth.to_dict())
            if auth else None})

    @classmethod
    def from_dict(cls, d: Mapping) -> "TaskPushNotificationConfig":
        if not isinstance(d, Mapping) or not d.get("url"):
            raise A2AShapeError("a push notification config needs a url")
        a = d.get("authentication")
        return cls(url=str(d["url"]), id=d.get("id"), task_id=d.get("taskId"),
                   token=d.get("token"), tenant=d.get("tenant"),
                   authentication=AuthenticationInfo(str(a.get("scheme") or "Bearer"),
                                                     a.get("credentials"))
                   if isinstance(a, Mapping) else None)


@dataclass
class SendMessageConfiguration:
    accepted_output_modes: list = field(default_factory=list)
    task_push_notification_config: Optional[TaskPushNotificationConfig] = None
    history_length: Optional[int] = None
    return_immediately: bool = False

    @classmethod
    def from_dict(cls, d: Optional[Mapping]) -> "SendMessageConfiguration":
        d = d or {}
        push = d.get("taskPushNotificationConfig") or d.get("pushNotificationConfig")
        blocking = d.get("blocking")
        return cls(accepted_output_modes=list(d.get("acceptedOutputModes") or []),
                   task_push_notification_config=TaskPushNotificationConfig.from_dict(push)
                   if isinstance(push, Mapping) else None,
                   history_length=d.get("historyLength"),
                   return_immediately=bool(d.get("returnImmediately"))
                   or (blocking is False))

    def to_dict(self) -> dict:
        return _drop_empty({
            "acceptedOutputModes": self.accepted_output_modes,
            "taskPushNotificationConfig": self.task_push_notification_config.to_dict(
                redact=False) if self.task_push_notification_config else None,
            "historyLength": self.history_length,
            "returnImmediately": True if self.return_immediately else None})


@dataclass
class SendMessageRequest:
    message: Message
    configuration: SendMessageConfiguration = field(default_factory=SendMessageConfiguration)
    metadata: Optional[dict] = None
    tenant: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Mapping) -> "SendMessageRequest":
        if not isinstance(d, Mapping) or not isinstance(d.get("message"), Mapping):
            raise A2AShapeError("SendMessage needs a `message`")
        return cls(message=Message.from_dict(d["message"]),
                   configuration=SendMessageConfiguration.from_dict(d.get("configuration")),
                   metadata=d.get("metadata"), tenant=d.get("tenant"))

    def to_dict(self) -> dict:
        return _drop_empty({"tenant": self.tenant, "message": self.message.to_dict(),
                            "configuration": self.configuration.to_dict(),
                            "metadata": self.metadata})


def stream_response(obj: Any) -> dict:
    """A StreamResponse (also the push-notification body and SendMessage's result): exactly one
    of task | message | statusUpdate | artifactUpdate."""
    if isinstance(obj, Task):
        return {"task": obj.to_dict()}
    if isinstance(obj, Message):
        return {"message": obj.to_dict()}
    if isinstance(obj, TaskStatusUpdateEvent):
        return {"statusUpdate": obj.to_dict()}
    if isinstance(obj, TaskArtifactUpdateEvent):
        return {"artifactUpdate": obj.to_dict()}
    raise A2AShapeError(f"not a stream payload: {type(obj).__name__}")


def parse_stream_response(d: Mapping) -> Any:
    """The object a StreamResponse / SendMessageResponse carries (v0.3 `kind` bodies too)."""
    if not isinstance(d, Mapping):
        raise A2AShapeError("a stream response must be an object")
    if "task" in d:
        return Task.from_dict(d["task"])
    if "message" in d:
        return Message.from_dict(d["message"])
    if "statusUpdate" in d:
        s = d["statusUpdate"]
        return TaskStatusUpdateEvent(str(s.get("taskId")), str(s.get("contextId")),
                                     TaskStatus.from_dict(s.get("status") or {}),
                                     s.get("metadata"))
    if "artifactUpdate" in d:
        a = d["artifactUpdate"]
        return TaskArtifactUpdateEvent(str(a.get("taskId")), str(a.get("contextId")),
                                       Artifact.from_dict(a.get("artifact") or {}),
                                       bool(a.get("append")), bool(a.get("lastChunk")),
                                       a.get("metadata"))
    kind = d.get("kind")  # v0.3: the object itself, discriminated by `kind`
    if kind == "task":
        return Task.from_dict(d)
    if kind == "message":
        return Message.from_dict(d)
    if kind == "status-update":
        return TaskStatusUpdateEvent(str(d.get("taskId")), str(d.get("contextId")),
                                     TaskStatus.from_dict(d.get("status") or {}),
                                     d.get("metadata"))
    if kind == "artifact-update":
        return TaskArtifactUpdateEvent(str(d.get("taskId")), str(d.get("contextId")),
                                       Artifact.from_dict(d.get("artifact") or {}),
                                       bool(d.get("append")), bool(d.get("lastChunk")),
                                       d.get("metadata"))
    raise A2AShapeError("a stream response holds one of task|message|statusUpdate|artifactUpdate")


# JSON-RPC error codes (A2A v1.0 §5.4).
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
INVALID_AGENT_RESPONSE = -32006
EXTENDED_AGENT_CARD_NOT_CONFIGURED = -32007
EXTENSION_SUPPORT_REQUIRED = -32008
VERSION_NOT_SUPPORTED = -32009

#: The v1.0 method names, and the v0.3 names a v1.0 server also accepts.
METHODS = ("SendMessage", "SendStreamingMessage", "GetTask", "ListTasks", "CancelTask",
           "SubscribeToTask", "CreateTaskPushNotificationConfig",
           "GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs",
           "DeleteTaskPushNotificationConfig", "GetExtendedAgentCard")
LEGACY_METHODS = {"message/send": "SendMessage", "message/stream": "SendStreamingMessage",
                  "tasks/get": "GetTask", "tasks/cancel": "CancelTask",
                  "tasks/resubscribe": "SubscribeToTask",
                  "tasks/pushNotificationConfig/set": "CreateTaskPushNotificationConfig",
                  "tasks/pushNotificationConfig/get": "GetTaskPushNotificationConfig",
                  "tasks/pushNotificationConfig/list": "ListTaskPushNotificationConfigs",
                  "tasks/pushNotificationConfig/delete": "DeleteTaskPushNotificationConfig",
                  "agent/getAuthenticatedExtendedCard": "GetExtendedAgentCard"}


def method_name(method: str) -> str:
    """The v1.0 name of a JSON-RPC method (v0.3 names accepted)."""
    return LEGACY_METHODS.get(method, method)


__all__ = [
    "A2AShapeError", "A2A_MEDIA_TYPE", "A2A_VERSION", "Artifact", "AuthenticationInfo",
    "INTERRUPTED_STATES", "LEGACY_METHODS", "METHODS", "Message", "Part", "RUN_STATUS_TO_STATE",
    "Role", "SendMessageConfiguration", "SendMessageRequest", "TERMINAL_STATES", "Task",
    "TaskArtifactUpdateEvent", "TaskPushNotificationConfig", "TaskState", "TaskStatus",
    "TaskStatusUpdateEvent", "agent_message", "data_part", "method_name",
    "parse_stream_response", "state_for_run_status", "stream_response", "text_part",
    "user_message",
]
