# Mobile App Operator: runner contract

BOSS-435 adds a selectable Mobile App Operator definition to the Org Chart and Agent Studio catalog. It is a **governed template**, initially in `deterministic_brief` mode. Installing it creates a versioned project-scoped AgentDefinition and a receipt. It does not install an SDK, reserve a device, execute a build, or claim a test passed. The other 20 new templates follow the same posture.

## Execution path to make the role operational

The mobile role is stack agnostic. A project supplies its build and test commands, package/bundle identifier, and artifact locations. A future mobile runner adapter must expose these typed operations for each target: `build`, `test`, `launch`, `interact`, `screenshot`, and `logs`. Its input must include a project binding, source revision, platform, device identifier, command allowlist, timeout, data boundary, and output artifact location. Its output must include exit status, tool versions, device identity, logs, screenshots, test report, and artifact hashes. A failed operation must be reported as failed or deadlettered, never represented by a generated brief.

| Target | Build and test host | Device control | Current template state |
| --- | --- | --- | --- |
| Android emulator | Android SDK, JDK, configured project build/test command, emulator image and virtualization | ADB plus open-source Appium UiAutomator2; optional scrcpy for operator viewing | Adapter required |
| Android physical device | Android SDK, JDK, configured project build/test command | Authorized ADB device plus Appium UiAutomator2; optional scrcpy | Adapter required |
| iOS Simulator | **macOS with full Xcode**, configured project build/test command, installed Simulator runtime | `xcrun simctl` plus open-source Appium XCUITest/WebDriverAgent | Adapter required |
| iOS physical device | **macOS with full Xcode**, signing identity/provisioning and configured project build/test command | Paired, trusted device with Developer Mode plus Appium XCUITest/WebDriverAgent | Adapter required |

A Linux VM can host BOSS and an Android runner when its virtualization/device requirements are met. It cannot build iOS apps or run iOS Simulator locally. Connect a separate macOS/Xcode execution node for that target. Appium and its drivers are open source; Android SDK and Apple's Xcode/toolchain retain their own licenses. Do not present the installer as supplying iOS build capability on a Linux VM.

Before invoking any mobile tool, BOSS must resolve an installed SkillSpec and adapter against the agent's RoleSpec, project, data boundary, device lease, and policy. Every invocation needs a leased job and a unique dedupe key, a Receipt for an external/device effect, and a BudgetReservation before any charged model, cloud device, or infrastructure action. Physical-device setup and code signing stay in a scoped local runner; the agent receives references rather than secrets. Build output and test evidence attach to the task and review packet. Destructive device actions or production publishing require their own approval path.

## Sources

- [Android command-line tools](https://developer.android.com/tools) and [ADB](https://developer.android.com/tools/adb)
- [Apple Xcode command-line tools](https://developer.apple.com/documentation/xcode/xcode-command-line-tool-reference)
- [Appium](https://appium.io/) and its [UiAutomator2 driver](https://appium.io/docs/en/2.0/quickstart/uiauto2-driver/)
- [Appium XCUITest driver](https://github.com/appium/appium-xcuitest-driver) and [real-device setup](https://github.com/appium/appium-xcuitest-driver/blob/master/docs/getting-started/device-setup.md)
- [scrcpy](https://github.com/Genymobile/scrcpy)
