"""Bounded local device actions behind the leased Skill Runtime.

The API may execute this adapter only when its operator explicitly enables it on
a host with local Android emulators or iOS Simulators. It accepts typed actions,
never shell text, host paths, physical devices, or an implicit device selection.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import text

from .core import emit_event, make_id
from .skill_executors import SkillExecutorConfigurationError, SkillExecutorContext


_ANDROID_ID = re.compile(r"^emulator-[0-9]{4,6}$")
_IOS_ID = re.compile(r"^[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}$")
_BUNDLE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$")
_ARTIFACT_ID = re.compile(r"^art_[A-Za-z0-9_]{8,80}$")
_ACTIONS = {"install", "launch", "tap", "swipe", "text", "screenshot", "logs", "inspect", "test"}
_MAX_EVIDENCE_BYTES = 2_000_000
_TIMEOUT_SECONDS = 60


class MobileDeviceError(SkillExecutorConfigurationError):
    """A visible refusal or failed device operation."""


def _required_string(inputs: dict[str, Any], key: str) -> str:
    value = inputs.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MobileDeviceError(f"{key} is required")
    return value.strip()


def _coordinate(inputs: dict[str, Any], key: str) -> str:
    value = inputs.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise MobileDeviceError(f"{key} must be an integer from 0 to 10000")
    return str(value)


def _host_environment() -> dict[str, str]:
    # The API can hold provider secrets. Device tools get only what they need.
    keep = (
        "PATH", "HOME", "USER", "TMPDIR", "ANDROID_HOME", "ANDROID_SDK_ROOT",
        "DEVELOPER_DIR", "XCODE_SELECT",
    )
    return {key: os.environ[key] for key in keep if key in os.environ}


class LocalMobileDeviceAdapter:
    def __init__(self, context: SkillExecutorContext):
        self.context = context

    def _binary(self, name: str) -> str:
        found = shutil.which(name)
        if not found:
            raise MobileDeviceError(f"{name} is not installed on this device runner host")
        return found

    def _run(
        self, argv: list[str], *, output: Path | None = None, timeout: int = _TIMEOUT_SECONDS
    ) -> str:
        try:
            if output is None:
                completed = subprocess.run(
                    argv, capture_output=True, timeout=timeout,
                    env=_host_environment(), check=False,
                )
                stdout = completed.stdout
            else:
                with output.open("wb") as destination:
                    os.chmod(output, 0o600)
                    completed = subprocess.run(
                        argv, stdout=destination, stderr=subprocess.PIPE,
                        timeout=timeout, env=_host_environment(), check=False,
                    )
                stdout = b""
        except subprocess.TimeoutExpired as exc:
            raise MobileDeviceError(f"device action timed out after {timeout}s") from exc
        except OSError as exc:
            raise MobileDeviceError(f"device tool could not start: {type(exc).__name__}") from exc
        if completed.returncode != 0:
            # Device tools can echo entered text and app log values on stderr.
            # Keep the outcome, not the bytes, in receipts and invocations.
            raise MobileDeviceError(f"device tool exited {completed.returncode}")
        return stdout.decode("utf-8", "replace")

    def _target(self, inputs: dict[str, Any]) -> tuple[str, str, str]:
        target = _required_string(inputs, "target")
        device_id = _required_string(inputs, "device_id")
        if target == "android_emulator":
            if not _ANDROID_ID.fullmatch(device_id):
                raise MobileDeviceError("Android emulator id must look like emulator-5554")
            tool = self._binary("adb")
            available = self._run([tool, "devices"])
            if not any(
                line.split()[:2] == [device_id, "device"]
                for line in available.splitlines()
            ):
                raise MobileDeviceError(f"Android emulator {device_id} is not connected and ready")
            return target, device_id, tool
        if target == "ios_simulator":
            if platform.system() != "Darwin":
                raise MobileDeviceError("iOS Simulator actions require a macOS runner with Xcode")
            if not _IOS_ID.fullmatch(device_id):
                raise MobileDeviceError("iOS simulator id must be a UUID")
            tool = self._binary("xcrun")
            raw = self._run([tool, "simctl", "list", "devices", "booted", "--json"])
            try:
                devices = json.loads(raw).get("devices", {})
            except (ValueError, AttributeError) as exc:
                raise MobileDeviceError("simctl returned invalid simulator inventory") from exc
            if not any(
                entry.get("udid", "").lower() == device_id.lower()
                for group in devices.values() for entry in group
            ):
                raise MobileDeviceError(f"iOS simulator {device_id} is not booted")
            return target, device_id, tool
        if target in {"android_physical", "ios_physical"}:
            raise MobileDeviceError("physical device execution is not supported by this adapter")
        raise MobileDeviceError("target must be android_emulator or ios_simulator")

    def _source_artifact(self, artifact_id: str, suffix: str) -> Path:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise MobileDeviceError("artifact_id must be a BOSS artifact id")
        row = self.context.connection.execute(
            text("SELECT uri FROM artifacts WHERE id = :id AND project_id = :project"),
            {"id": artifact_id, "project": self.context.project_id},
        ).mappings().one_or_none()
        if not row:
            raise MobileDeviceError("install artifact was not found in this project")
        root = self.context.settings.artifact_root.resolve()
        path = Path(str(row["uri"])).resolve()
        if not path.is_relative_to(root):
            raise MobileDeviceError("install artifact must be stored under BOSS_ARTIFACTS_DIR")
        if path.suffix.lower() != suffix or not path.exists():
            raise MobileDeviceError(f"install artifact must be an existing {suffix}")
        return path

    def _evidence_path(self, receipt_id: str, suffix: str) -> Path:
        project_key = hashlib.sha256(self.context.project_id.encode()).hexdigest()[:24]
        directory = self.context.settings.artifact_root / "mobile" / project_key
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        return directory / f"{receipt_id}{suffix}"

    def _record_evidence(self, path: Path, artifact_type: str) -> dict[str, Any]:
        if not path.exists() or path.stat().st_size == 0:
            raise MobileDeviceError("device tool produced empty evidence")
        truncated = path.stat().st_size > _MAX_EVIDENCE_BYTES
        if truncated and artifact_type == "mobile_screenshot":
            raise MobileDeviceError("screenshot exceeded the 2 MB evidence limit")
        if truncated:
            with path.open("r+b") as stream:
                stream.truncate(_MAX_EVIDENCE_BYTES)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifact_id = make_id("art")
        content = {
            "sha256": digest, "bytes": path.stat().st_size,
            "truncated": truncated, "data_boundary": "internal",
        }
        self.context.connection.execute(
            text(
                """INSERT INTO artifacts
                   (id, project_id, artifact_type, title, uri, content,
                    content_hash, created_by_job_id, data_boundary)
                   VALUES (:id, :project, :type, :title, :uri,
                           CAST(:content AS JSONB), :hash, :job, 'internal')"""
            ),
            {
                "id": artifact_id, "project": self.context.project_id,
                "type": artifact_type, "title": artifact_type.replace("_", " "),
                "uri": str(path), "content": json.dumps(content), "hash": digest,
                "job": self.context.created_by_job_id,
            },
        )
        emit_event(
            self.context.connection, self.context.project_id, "artifact.created",
            self.context.actor, "artifact", artifact_id,
            {"artifact_type": artifact_type, "sha256": digest},
        )
        return {"artifact_id": artifact_id, **content}

    def execute(self, inputs: dict[str, Any], receipt_id: str) -> dict[str, Any]:
        if not self.context.settings.mobile_device_runner_enabled:
            raise MobileDeviceError(
                "mobile device runner is disabled; set BOSS_MOBILE_DEVICE_RUNNER_ENABLED "
                "on an explicitly trusted device host"
            )
        action = _required_string(inputs, "action")
        if action not in _ACTIONS:
            raise MobileDeviceError("unsupported mobile action")
        target, device_id, tool = self._target(inputs)
        android = target == "android_emulator"
        prefix = [tool, "-s", device_id] if android else [tool, "simctl"]
        result: dict[str, Any] = {"target": target, "device_id": device_id, "action": action}

        if action == "install":
            suffix = ".apk" if android else ".app"
            source = self._source_artifact(_required_string(inputs, "artifact_id"), suffix)
            argv = prefix + (["install", str(source)] if android
                             else ["install", device_id, str(source)])
            self._run(argv)
            result["performed"] = True
        elif action == "launch":
            bundle = _required_string(inputs, "bundle_id")
            if not _BUNDLE_ID.fullmatch(bundle):
                raise MobileDeviceError("bundle_id is invalid")
            argv = (prefix + ["shell", "monkey", "-p", bundle, "-c",
                              "android.intent.category.LAUNCHER", "1"] if android
                    else prefix + ["launch", device_id, bundle])
            result["output_tail"] = self._run(argv)[-1000:]
        elif action in {"tap", "swipe"}:
            coords = [_coordinate(inputs, key) for key in
                      (("x", "y") if action == "tap" else ("x", "y", "end_x", "end_y"))]
            if android:
                argv = prefix + ["shell", "input", "tap" if action == "tap" else "swipe"] + coords
                if action == "swipe":
                    argv.append("300")
            else:
                idb = self._binary("idb")
                argv = [idb, "ui", action, *coords, "--udid", device_id]
            result["output_tail"] = self._run(argv)[-1000:]
        elif action == "text":
            value = _required_string(inputs, "text")
            if len(value) > 200:
                raise MobileDeviceError("text must be 200 characters or less")
            if android:
                # adb shell may use a remote shell even though the host call uses argv.
                if not re.fullmatch(r"[A-Za-z0-9 .,@_-]+", value):
                    raise MobileDeviceError("Android text permits letters, numbers, spaces, and basic punctuation only")
                argv = prefix + ["shell", "input", "text", value.replace(" ", "%s")]
            else:
                argv = [self._binary("idb"), "ui", "text", value, "--udid", device_id]
            self._run(argv)
            result["performed"] = True
        elif action == "screenshot":
            path = self._evidence_path(receipt_id, ".png")
            try:
                if android:
                    self._run(prefix + ["exec-out", "screencap", "-p"], output=path)
                else:
                    self._run(prefix + ["io", device_id, "screenshot", str(path)])
                result.update(self._record_evidence(path, "mobile_screenshot"))
            except Exception:
                path.unlink(missing_ok=True)
                raise
        else:  # logs, inspect, or test
            path = self._evidence_path(
                receipt_id, ".json" if action == "inspect" else ".txt"
            )
            recorded = False
            try:
                if action == "test" and android:
                    runner = _required_string(inputs, "test_runner")
                    parts = runner.split("/")
                    if len(parts) != 2 or not all(_BUNDLE_ID.fullmatch(part) for part in parts):
                        raise MobileDeviceError(
                            "test_runner must be package/fully.qualified.Runner"
                        )
                    argv = prefix + ["shell", "am", "instrument", "-w", runner]
                elif action == "test":
                    xctestrun = self._source_artifact(
                        _required_string(inputs, "artifact_id"), ".xctestrun"
                    )
                    xcodebuild = self._binary("xcodebuild")
                    argv = [
                        xcodebuild, "test-without-building",
                        "-xctestrun", str(xctestrun),
                        "-destination", f"id={device_id}",
                    ]
                elif android:
                    argv = (prefix + ["logcat", "-d", "-t", "1000"] if action == "logs"
                            else prefix + ["shell", "uiautomator", "dump", "/dev/tty"])
                elif action == "logs":
                    argv = prefix + ["spawn", device_id, "log", "show", "--last", "1m",
                                     "--style", "compact"]
                else:
                    argv = [self._binary("idb"), "ui", "describe-all", "--udid", device_id]
                self._run(argv, output=path, timeout=300 if action == "test" else _TIMEOUT_SECONDS)
                result.update(self._record_evidence(
                    path, {
                        "logs": "mobile_logs",
                        "inspect": "mobile_ui_inspection",
                        "test": "mobile_test_result",
                    }[action]
                ))
                recorded = True
                if action == "test" and android:
                    report = path.read_text(encoding="utf-8", errors="replace")
                    if "FAILURES!!!" in report or "INSTRUMENTATION_FAILED" in report:
                        raise MobileDeviceError(
                            f"Android instrumentation failed; see artifact {result['artifact_id']}"
                        )
            except Exception:
                if not recorded:
                    path.unlink(missing_ok=True)
                raise
        return result
