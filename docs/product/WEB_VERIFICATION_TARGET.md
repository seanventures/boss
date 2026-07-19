# Web-appropriate verification target (APP-3 / #93)

> Plane #93, part of the #90 first-time-output-quality epic. Spec `09 §3.3`.

## Why

The governed loop's authoritative check was hardcoded to `python3 -m unittest discover -v`. A
React/Next/Vite app's *actual* health signals — compile, typecheck, lint, render — were never run, so
the repair loop (Amplify) had no web-native signal: it could make a page "more correct" by unittest
while it stayed broken as a web app.

## What changed

`verification_target.py` resolves the command set from the **project type**:
- **web / TS** → `npm run build && npm run typecheck && npm run lint && npm run test:e2e` — exactly the
  scripts APP-1's scaffold defines (a test asserts they stay in sync), short-circuited so repair
  evidence points at the earliest broken stage.
- **python** → the existing `unittest` default, unchanged.

`detect_kind(spec, title)` uses an explicit `project_kind` first, then APP-2's `is_ui_bearing`.

## Wiring (additive)

The governed entry's fallback `test_cmd = (spec["test_commands"] or default_test_commands(spec, title))`
— explicit `test_commands` always win; Python is untouched. The regression stage uses the resolved
`test_cmd` too. Because the repair loop re-issues with whatever the check printed, switching the command
set automatically gives Amplify **build/typecheck/e2e failure evidence** for web tasks.

## Tests

**7 unit tests** (`test_verification_target.py`): kind detection, the web step set, Python default,
**sync with the APP-1 scaffold's scripts**, profile shape, explicit-commands-win. Governed
review-panel + microvm regression green.

## Next

Resolve from a detected `package.json` (not just `project_kind`/keywords) for existing repos; wire the
`playwright` e2e to APP-4's real render gate; per-package-manager (`pnpm`/`yarn`) command selection.
