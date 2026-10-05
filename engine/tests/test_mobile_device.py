from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from boss_engine.services import mobile_device
from boss_engine.services.mobile_device import MobileDeviceError
from boss_engine.services.scheduling import NICE_LEVEL
from boss_engine.services.skill_executors import (
    SkillExecutorContext,
    execute_mobile_device,
    registered_executor_ids,
)


IOS_ID = "A8A41972-C4E3-4A0F-980E-049407889D2C"


class FakeConnection:
    def __init__(self, uri: str | None = None):
        self.uri = uri
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        if "SELECT uri FROM artifacts" in str(statement):
            row = {"uri": self.uri} if self.uri else None
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: row))
        return SimpleNamespace()


class FakeReceipts:
    def __init__(self):
        self.receipt = None
        self.succeeded = 0
        self.failed = 0
        self.payload = None
        self.error = None

    def create_or_reuse(self, *args, **kwargs):
        self.payload = args[7]
        if self.receipt:
            return self.receipt, False
        self.receipt = {"id": "rcpt_mobile_1", "status": "in_progress", "result": None}
        return self.receipt, True

    def mark_succeeded(self, connection, receipt_id, provider_ref, result):
        self.succeeded += 1
        self.receipt = {"id": receipt_id, "status": "succeeded", "result": result}

    def mark_failed(self, connection, receipt_id, error):
        self.failed += 1
        self.error = error
        self.receipt = {"id": receipt_id, "status": "failed", "result": None}


def context(tmp_path: Path, *, enabled: bool = True, uri: str | None = None):
    receipts = FakeReceipts()
    ctx = SkillExecutorContext(
        settings=SimpleNamespace(
            mobile_device_runner_enabled=enabled,
            artifact_root=tmp_path,
        ),
        connection=FakeConnection(uri),
        project_id="project_mobile",
        actor="mobile-worker",
        created_by_job_id="job_mobile_1",
        org_id="org_mobile",
        workspace_id="workspace_mobile",
        receipt_service=receipts,
    )
    return ctx, receipts


def fake_tools(monkeypatch, *, ios: bool = False):
    calls = []

    def run(argv, **kwargs):
        # Keep these action tests focused on the typed device command; the scheduling prefix
        # is asserted separately for both subprocess branches below.
        command = argv[next(i for i, part in enumerate(argv) if part.startswith("/tool/")):]
        calls.append(command)
        if command[0] == "/tool/adb" and command[1:] == ["devices"]:
            data = b"List of devices attached\nemulator-5554\tdevice\n"
        elif command[0] == "/tool/xcrun" and command[1:5] == [
            "simctl", "list", "devices", "booted"
        ]:
            data = json.dumps({"devices": {"iOS": [{"udid": IOS_ID}]}}).encode()
        else:
            data = b"ok"
        if "stdout" in kwargs:
            kwargs["stdout"].write(b"\x89PNG\r\n\x1a\nimage")
            data = b""
        return subprocess.CompletedProcess(argv, 0, data, b"")

    monkeypatch.setattr(mobile_device.shutil, "which", lambda name: f"/tool/{name}")
    monkeypatch.setattr(mobile_device.owned_process, "run", run)
    monkeypatch.setattr(mobile_device.platform, "system", lambda: "Darwin" if ios else "Linux")
    return calls


@pytest.mark.parametrize("file_output", [False, True])
def test_local_device_tools_yield_to_the_control_plane_in_both_output_modes(
    monkeypatch, tmp_path, file_output
):
    commands = []

    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        if file_output:
            kwargs["stdout"].write(b"device output")
        return subprocess.CompletedProcess(argv, 0, b"device output", b"")

    monkeypatch.setattr(mobile_device.owned_process, "run", run)
    monkeypatch.setattr(mobile_device.shutil, "which", lambda _name: None)
    ctx, _ = context(tmp_path)
    adapter = mobile_device.LocalMobileDeviceAdapter(ctx)
    output = tmp_path / "device.txt" if file_output else None

    result = adapter._run(["/tool/adb", "devices"], output=output)

    assert len(commands) == 1
    argv, options = commands[0]
    assert argv == ["nice", "-n", NICE_LEVEL, "/tool/adb", "devices"]
    assert options["timeout"] == 60
    assert options["check"] is False
    if file_output:
        assert output.read_bytes() == b"device output"
        assert result == ""
    else:
        assert result == "device output"


def test_android_tap_is_executable_receipted_and_not_replayed(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch)
    ctx, receipts = context(tmp_path)
    inputs = {"target": "android_emulator", "device_id": "emulator-5554",
              "action": "tap", "x": 120, "y": 240}
    first = execute_mobile_device(ctx, inputs)
    second = execute_mobile_device(ctx, inputs)
    assert first["receipt_id"] == "rcpt_mobile_1"
    assert first["actual_cost_usd_micros"] == 0
    assert second["reused"] is True
    assert receipts.succeeded == 1
    assert calls.count(["/tool/adb", "-s", "emulator-5554", "shell", "input",
                        "tap", "120", "240"]) == 1


def test_disabled_and_physical_targets_fail_with_receipt(monkeypatch, tmp_path):
    fake_tools(monkeypatch)
    disabled, receipts = context(tmp_path, enabled=False)
    with pytest.raises(MobileDeviceError, match="disabled"):
        execute_mobile_device(disabled, {"target": "android_emulator",
                                         "device_id": "emulator-5554", "action": "tap",
                                         "x": 1, "y": 2})
    assert receipts.failed == 1

    physical, receipts = context(tmp_path)
    with pytest.raises(MobileDeviceError, match="physical device"):
        execute_mobile_device(physical, {"target": "android_physical",
                                         "device_id": "ABC123", "action": "tap",
                                         "x": 1, "y": 2})
    assert receipts.failed == 1


def test_android_text_refuses_remote_shell_metacharacters(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch)
    ctx, receipts = context(tmp_path)
    with pytest.raises(MobileDeviceError, match="basic punctuation"):
        execute_mobile_device(ctx, {"target": "android_emulator",
                                    "device_id": "emulator-5554", "action": "text",
                                    "text": "hello; rm -rf /"})
    assert receipts.failed == 1
    assert not any("input" in call for call in calls)


def test_typed_text_is_not_persisted_in_receipt_or_result(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch)
    ctx, receipts = context(tmp_path)
    value = "private passphrase 927"
    result = execute_mobile_device(
        ctx, {"target": "android_emulator", "device_id": "emulator-5554",
              "action": "text", "text": value}
    )
    assert result["performed"] is True
    assert value not in json.dumps(receipts.payload)
    assert value not in json.dumps(receipts.receipt)
    assert value not in json.dumps(result)
    assert any("private%spassphrase%s927" in call for call in calls)


def test_tool_stderr_cannot_leak_typed_text(monkeypatch, tmp_path):
    fake_tools(monkeypatch)
    original = mobile_device.owned_process.run

    def run(argv, **kwargs):
        if "input" in argv:
            return subprocess.CompletedProcess(argv, 1, b"", b"private passphrase 927")
        return original(argv, **kwargs)

    monkeypatch.setattr(mobile_device.owned_process, "run", run)
    ctx, receipts = context(tmp_path)
    with pytest.raises(MobileDeviceError, match="exited 1") as error:
        execute_mobile_device(
            ctx, {"target": "android_emulator", "device_id": "emulator-5554",
                  "action": "text", "text": "private passphrase 927"}
        )
    assert "private passphrase" not in str(error.value)
    assert "private passphrase" not in json.dumps(receipts.error)


def test_ios_simulator_tap_uses_idb_and_explicit_udid(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch, ios=True)
    ctx, receipts = context(tmp_path)
    execute_mobile_device(ctx, {"target": "ios_simulator", "device_id": IOS_ID,
                                "action": "tap", "x": 42, "y": 99})
    assert receipts.succeeded == 1
    assert ["/tool/idb", "ui", "tap", "42", "99", "--udid", IOS_ID] in calls


def test_ios_refuses_non_mac_host_before_tool_execution(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch)
    ctx, receipts = context(tmp_path)
    with pytest.raises(MobileDeviceError, match="macOS"):
        execute_mobile_device(ctx, {"target": "ios_simulator", "device_id": IOS_ID,
                                    "action": "tap", "x": 1, "y": 2})
    assert receipts.failed == 1
    assert calls == []


def test_screenshot_records_bounded_private_artifact(monkeypatch, tmp_path):
    fake_tools(monkeypatch)
    monkeypatch.setattr(mobile_device, "emit_event", lambda *args, **kwargs: None)
    ctx, receipts = context(tmp_path)
    result = execute_mobile_device(ctx, {"target": "android_emulator",
                                         "device_id": "emulator-5554",
                                         "action": "screenshot"})
    assert receipts.succeeded == 1
    assert result["artifact_id"].startswith("art_")
    artifact = next((tmp_path / "mobile").rglob("rcpt_mobile_1.png"))
    assert artifact.read_bytes().startswith(b"\x89PNG")
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert any("INSERT INTO artifacts" in sql for sql, _ in ctx.connection.calls)


def test_android_instrumentation_runs_and_records_test_evidence(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch)
    monkeypatch.setattr(mobile_device, "emit_event", lambda *args, **kwargs: None)
    original = mobile_device.owned_process.run

    def run(argv, **kwargs):
        if "instrument" in argv:
            calls.append(argv[next(i for i, part in enumerate(argv)
                                   if part.startswith("/tool/")):])
            kwargs["stdout"].write(b"Time: 0.1\nOK (2 tests)\nINSTRUMENTATION_CODE: -1\n")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return original(argv, **kwargs)

    monkeypatch.setattr(mobile_device.owned_process, "run", run)
    ctx, receipts = context(tmp_path)
    result = execute_mobile_device(
        ctx, {"target": "android_emulator", "device_id": "emulator-5554",
              "action": "test",
              "test_runner": "com.example.tests/androidx.test.runner.AndroidJUnitRunner"}
    )
    assert receipts.succeeded == 1
    assert result["artifact_id"].startswith("art_")
    assert ["/tool/adb", "-s", "emulator-5554", "shell", "am", "instrument",
            "-w", "com.example.tests/androidx.test.runner.AndroidJUnitRunner"] in calls


def test_ios_xctestrun_requires_project_artifact(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch, ios=True)
    ctx, receipts = context(tmp_path, uri=str(tmp_path / "not-present.xctestrun"))
    with pytest.raises(MobileDeviceError, match="existing .xctestrun"):
        execute_mobile_device(ctx, {
            "target": "ios_simulator", "device_id": IOS_ID, "action": "test",
            "artifact_id": "art_valid_123",
        })
    assert receipts.failed == 1
    assert not any("test-without-building" in call for call in calls)


def test_install_requires_project_artifact_inside_artifact_root(monkeypatch, tmp_path):
    calls = fake_tools(monkeypatch)
    outside = tmp_path.parent / "foreign.apk"
    outside.write_bytes(b"apk")
    ctx, receipts = context(tmp_path, uri=str(outside))
    with pytest.raises(MobileDeviceError, match="under BOSS_ARTIFACTS_DIR"):
        execute_mobile_device(ctx, {"target": "android_emulator",
                                    "device_id": "emulator-5554",
                                    "action": "install", "artifact_id": "art_valid_123"})
    assert receipts.failed == 1
    assert not any("install" in call for call in calls)


def test_skill_is_registered_and_requires_a_lease(tmp_path):
    assert "mobile.device.execute" in registered_executor_ids()
    ctx, _ = context(tmp_path)
    ctx.created_by_job_id = None
    with pytest.raises(MobileDeviceError, match="leased"):
        execute_mobile_device(ctx, {"action": "tap"})
