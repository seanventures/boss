from __future__ import annotations

from hashlib import sha256
from typing import Any

import yaml


class CIConfigGenerator:
    """Deterministic CI configuration for repositories that have no pipeline.

    The generated deploy job only packages a BOSS release manifest. Provider
    actuation stays in the Resource Plane and therefore cannot bypass ChangeSet,
    reservation, approval, or ResourceReceipt controls.
    """

    def generate(
        self,
        *,
        language: str,
        framework: str | None = None,
        target: str = "github_actions",
    ) -> dict[str, Any]:
        normalized_language = language.strip().lower()
        normalized_target = target.strip().lower()
        commands = self._commands(normalized_language, framework)
        if normalized_target == "github_actions":
            path = ".github/workflows/ci.yml"
            content = self._github_actions(commands)
            parsed = yaml.safe_load(content)
            jobs = parsed.get("jobs") if isinstance(parsed, dict) else None
            if not isinstance(jobs, dict) or not {"test", "build", "deploy"}.issubset(
                jobs
            ):
                raise ValueError("Generated GitHub Actions workflow is incomplete.")
        elif normalized_target == "jenkins":
            path = "Jenkinsfile"
            content = self._jenkinsfile(commands)
        else:
            raise ValueError(f"Unsupported CI configuration target: {target}")
        return {
            "target": normalized_target,
            "path": path,
            "content": content,
            "checksum": sha256(content.encode("utf-8")).hexdigest(),
            "commands": commands,
        }

    @staticmethod
    def _commands(language: str, framework: str | None) -> dict[str, str]:
        normalized_framework = (framework or "").strip().lower()
        if language in {"javascript", "typescript", "node", "nodejs"} or any(
            name in normalized_framework
            for name in ("next", "react", "vite", "svelte", "vue")
        ):
            return {
                "setup": "npm ci",
                "typecheck": "npm run typecheck --if-present",
                "lint": "npm run lint --if-present",
                "test": "npm test --if-present",
                "build": "npm run build",
            }
        if language in {"python", "py"}:
            return {
                "setup": "python -m pip install -e .",
                "typecheck": "python -m compileall -q .",
                "lint": "python -m ruff check .",
                "test": "python -m pytest",
                "build": "python -m build",
            }
        if language in {"go", "golang"}:
            return {
                "setup": "go mod download",
                "typecheck": "go vet ./...",
                "lint": "go fmt ./...",
                "test": "go test ./...",
                "build": "go build ./...",
            }
        raise ValueError(f"Unsupported repository language: {language}")

    @staticmethod
    def _github_actions(commands: dict[str, str]) -> str:
        return f"""name: CI

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up runtime
        run: {commands['setup']}
      - name: Typecheck
        run: {commands['typecheck']}
      - name: Lint
        run: {commands['lint']}
      - name: Test
        run: {commands['test']}

  build:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up runtime
        run: {commands['setup']}
      - name: Build
        run: {commands['build']}
      - name: Record build evidence
        run: printf '%s\\n' '${{{{ github.sha }}}}' > boss-release-ref.txt
      - uses: actions/upload-artifact@v4
        with:
          name: boss-release-evidence
          path: boss-release-ref.txt

  deploy:
    if: github.event_name == 'workflow_dispatch'
    needs: build
    runs-on: ubuntu-latest
    environment: staging
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: boss-release-evidence
      - name: Hand off to governed BOSS promotion
        run: |
          test -s boss-release-ref.txt
          echo 'Build evidence ready. Provider deployment remains ChangeSet-gated in BOSS.'
"""

    @staticmethod
    def _jenkinsfile(commands: dict[str, str]) -> str:
        return f"""pipeline {{
  agent any
  stages {{
    stage('Test') {{ steps {{ sh '{commands['setup']}' ; sh '{commands['test']}' }} }}
    stage('Build') {{ steps {{ sh '{commands['build']}' }} }}
    stage('Deploy handoff') {{
      steps {{
        writeFile file: 'boss-release-ref.txt', text: env.GIT_COMMIT
        archiveArtifacts artifacts: 'boss-release-ref.txt', fingerprint: true
        echo 'Provider deployment remains ChangeSet-gated in BOSS.'
      }}
    }}
  }}
}}
"""
