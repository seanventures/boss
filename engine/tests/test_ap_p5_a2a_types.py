"""AP-P5: the A2A v1.0 Task model is the one async-task shape (protocols/a2a_types.py)."""
from __future__ import annotations

import pytest

from boss_engine.protocols import a2a_types as t


def test_task_round_trips_in_v1_json():
    task = t.Task(id="arun_1", context_id="thr_1",
                  status=t.TaskStatus(t.TaskState.INPUT_REQUIRED,
                                      t.agent_message("approve?", data={"decision_id": "d1"})),
                  history=[t.user_message("do it")],
                  artifacts=[t.Artifact(parts=[t.text_part("out")], name="result")])
    d = task.to_dict()
    assert d["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert d["contextId"] == "thr_1" and d["history"][0]["role"] == "ROLE_USER"
    assert d["status"]["message"]["parts"][1] == {"data": {"decision_id": "d1"},
                                                   "mediaType": "application/json"}
    assert "kind" not in d["history"][0]["parts"][0]
    back = t.Task.from_dict(d)
    assert back.to_dict() == d and back.state.interrupted and not back.state.terminal


def test_v03_shapes_parse():
    task = t.parse_stream_response({"kind": "task", "id": "x", "contextId": "c",
                                    "status": {"state": "input-required"},
                                    "history": [{"kind": "message", "role": "user",
                                                 "messageId": "m",
                                                 "parts": [{"kind": "text", "text": "hi"}]}]})
    assert task.state is t.TaskState.INPUT_REQUIRED
    assert task.history[0].role is t.Role.USER and task.history[0].text() == "hi"
    assert t.method_name("message/send") == "SendMessage"


def test_part_is_exactly_one_kind():
    with pytest.raises(t.A2AShapeError):
        t.Part(text="a", data={"b": 1})
    with pytest.raises(t.A2AShapeError):
        t.Part()


def test_run_status_maps_to_task_state():
    assert t.state_for_run_status("success") is t.TaskState.COMPLETED
    assert t.state_for_run_status("awaiting_approval") is t.TaskState.INPUT_REQUIRED
    assert t.state_for_run_status("queued") is t.TaskState.SUBMITTED
    assert t.state_for_run_status("rolled_back") is t.TaskState.CANCELED
    assert {s for s in t.TaskState if s.terminal} == set(t.TERMINAL_STATES)


def test_push_config_never_echoes_credentials():
    cfg = t.TaskPushNotificationConfig.from_dict(
        {"url": "https://x/hook", "token": "tok",
         "authentication": {"scheme": "Bearer", "credentials": "secret"}})
    out = cfg.to_dict()
    assert "token" not in out and out["authentication"] == {"scheme": "Bearer"}


def test_stream_response_is_exactly_one_payload():
    ev = t.TaskStatusUpdateEvent("a", "c", t.TaskStatus(t.TaskState.COMPLETED))
    d = t.stream_response(ev)
    assert list(d) == ["statusUpdate"]
    assert isinstance(t.parse_stream_response(d), t.TaskStatusUpdateEvent)
