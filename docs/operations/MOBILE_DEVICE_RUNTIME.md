# Mobile device execution (BOSS-439)

The opt-in `mobile.device.execute` skill runs one typed action per leased
`skill.execute` job. The worker calls the existing skill-invocation API with
its lease token; the Skill Runtime checks caller permission, reserves budget,
records the invocation, and calls this adapter. The adapter creates one
deduplicated Receipt per job before calling a device tool. Its failure remains
visible in the Receipt and skill invocation. Local device tools receive a
minimal environment and fixed argument arrays; no caller-supplied shell
command or host path is accepted.

## Supported actions

| Target | Install | Launch | Touch and text | Screenshot | Logs | UI inspection | Run built tests | Build source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Android emulator | APK artifact via ADB | package via ADB | ADB tap, swipe, constrained text | ADB PNG | ADB logcat | ADB UIAutomator | installed instrumentation runner | Not in this skill |
| iOS Simulator on macOS | .app artifact via simctl | bundle via simctl | open-source idb tap, swipe, text | simctl PNG | simctl log | open-source idb accessibility tree | built .xctestrun artifact via xcodebuild | Not in this skill |
| Android physical | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported |
| iOS physical | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported |

The Android SDK Platform Tools and a connected emulator are required for
Android. iOS requires a macOS host with full Xcode selected and a booted
Simulator; idb is additionally required for touch, text, and UI inspection.
The Community Linux VM installer does not install Xcode or make iOS actions
available. BOSS refuses missing tools, wrong host platforms, unready device
IDs, physical targets, and unsupported actions. Device IDs are always
explicit. No real emulator or Simulator action was run during BOSS-439
development; validation used command-boundary tests.

An operator must enable `BOSS_MOBILE_DEVICE_RUNNER_ENABLED=true` on the
trusted host that runs the BOSS API process and has local device tools. The
default is false. Standard Community Compose runs the API in a Linux
container, so setting this flag there does **not** provide a macOS iOS
Simulator, even when Docker runs on a Mac laptop. iOS actions require a
native macOS API process with local Xcode/Simulator/idb; routing these actions
to a separate macOS device host is not implemented. Android under Compose
likewise requires an explicitly connected emulator and ADB inside the API
runtime; the standard installer does not provision that path. Prebuilt APK,
.app, and .xctestrun inputs must already exist
as project-scoped BOSS artifacts under `BOSS_ARTIFACTS_DIR`. This skill does
not accept an arbitrary file path. Screenshots, logs, UI trees, and test
output become private, bounded artifacts with SHA-256 evidence; log and
inspection files are capped at 2 MB, and oversized screenshots fail.

For a direct one-shot operator action, create a `skill.execute` job through
`POST /v1/jobs` using the authenticated API:

```json
{
  "project_id": "project_example",
  "kind": "skill.execute",
  "payload": {
    "skill_id": "mobile.device.execute",
    "caller": "mobile_app_operator",
    "inputs": {
      "target": "android_emulator",
      "device_id": "emulator-5554",
      "action": "tap",
      "x": 120,
      "y": 240
    }
  },
  "max_attempts": 1,
  "dedupe_key": "project_example:mobile:tap:run-123"
}
```

Poll `GET /v1/jobs/{job_id}` and `GET /v1/skills/invocations?project_id=...` for the
result and Receipt. The existing worker owns lease claim and heartbeat.
The skill reserves budget even when its actual external charge is zero.
Android text is limited to 200 characters of basic letters, digits, spaces,
and punctuation because ADB's remote shell can reinterpret other characters.
The receipt stores an input hash, never entered text; tool output and stderr
from text actions are not persisted in the result.

## Remaining work

Agent Studio's `agent.studio.execute` currently produces a deterministic
brief. Its published template cannot dispatch this skill autonomously, even
when the skill appears in its capability catalog. BOSS-440 tracks the
template-to-leased-skill bridge, macOS device-host routing, policy tests, and
live device qualification.
Source builds and artifact handoff also need a separate isolated Android
builder and macOS Xcode runner. Until those paths and live tests pass, label
the mobile template as an available plan/role, not a fully autonomous mobile
operator.

Tool contracts: [Android Debug Bridge](https://developer.android.com/tools/adb),
[Android instrumentation from the command line](https://developer.android.com/studio/test/command-line),
[Apple Xcode command-line tools](https://developer.apple.com/documentation/xcode/xcode-command-line-tool-reference),
and [idb UI automation](https://fbidb.io/docs/idb/ui/).
