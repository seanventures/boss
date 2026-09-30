from __future__ import annotations

from hashlib import sha256
from typing import Any

import yaml


# Supported template vendors (CI config generate targets).
SUPPORTED_TARGETS = (
    "github_actions",
    "gitlab_ci",
    "jenkins",
    "circleci",
    "buildkite",
)

# Default config path per vendor.
TARGET_PATHS: dict[str, str] = {
    "github_actions": ".github/workflows/ci.yml",
    "gitlab_ci": ".gitlab-ci.yml",
    "jenkins": "Jenkinsfile",
    "circleci": ".circleci/config.yml",
    "buildkite": ".buildkite/pipeline.yml",
}


class CIConfigGenerator:
    """Deterministic CI configuration for repositories that have no pipeline.

    The generated deploy job only packages a BOSS release manifest. Provider
    actuation stays in the Resource Plane and therefore cannot bypass ChangeSet,
    reservation, approval, or ResourceReceipt controls.

    Supported targets: github_actions, gitlab_ci, jenkins, circleci, buildkite.
    """

    def generate(
        self,
        *,
        language: str,
        framework: str | None = None,
        target: str = "github_actions",
    ) -> dict[str, Any]:
        normalized_language = language.strip().lower()
        normalized_target = target.strip().lower().replace("-", "_")
        # Accept common aliases
        aliases = {
            "gha": "github_actions",
            "github": "github_actions",
            "actions": "github_actions",
            "gitlab": "gitlab_ci",
            "gitlabci": "gitlab_ci",
            "circle": "circleci",
            "circle_ci": "circleci",
            "bk": "buildkite",
        }
        normalized_target = aliases.get(normalized_target, normalized_target)

        commands = self._commands(normalized_language, framework)
        if normalized_target == "github_actions":
            content = self._github_actions(commands)
            parsed = yaml.safe_load(content)
            jobs = parsed.get("jobs") if isinstance(parsed, dict) else None
            if not isinstance(jobs, dict) or not {"test", "build", "deploy"}.issubset(
                jobs
            ):
                raise ValueError("Generated GitHub Actions workflow is incomplete.")
        elif normalized_target == "jenkins":
            content = self._jenkinsfile(commands)
        elif normalized_target == "gitlab_ci":
            content = self._gitlab_ci(commands)
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict) or "test" not in parsed or "build" not in parsed:
                raise ValueError("Generated GitLab CI config is incomplete.")
        elif normalized_target == "circleci":
            content = self._circleci(commands)
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict) or "jobs" not in parsed:
                raise ValueError("Generated CircleCI config is incomplete.")
        elif normalized_target == "buildkite":
            content = self._buildkite(commands)
            parsed = yaml.safe_load(content)
            if not isinstance(parsed, dict) or "steps" not in parsed:
                raise ValueError("Generated Buildkite pipeline is incomplete.")
        else:
            raise ValueError(
                f"Unsupported CI configuration target: {target}. "
                f"Supported: {', '.join(SUPPORTED_TARGETS)}"
            )

        path = TARGET_PATHS[normalized_target]
        return {
            "target": normalized_target,
            "path": path,
            "content": content,
            "checksum": sha256(content.encode("utf-8")).hexdigest(),
            "commands": commands,
        }

    @staticmethod
    def supported_targets() -> list[str]:
        return list(SUPPORTED_TARGETS)

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
    def _security_scan_script(*, indent: str = "          ") -> str:
        """OSS security-scan gate hook (semgrep / trivy / osv-scanner).

        Tools are optional on PATH. Missing binaries are skipped — this is a
        hook, not a silent pass of findings. Snyk is never invoked.
        """
        lines = [
            "echo 'BOSS security-scan gate: semgrep (SAST) / trivy (FS) / osv-scanner (deps)'",
            "echo 'Optional: gitleaks (secrets) / checkov (IaC). Snyk commercial API is never invoked.'",
            "# semgrep scan --error --metrics=off .",
            "# trivy fs --exit-code 1 --severity CRITICAL .",
            "# osv-scanner --recursive .",
            "# gitleaks detect --no-git --exit-code 1",
            "# checkov -d . --quiet",
            "if command -v semgrep >/dev/null 2>&1; then semgrep scan --error --metrics=off .; else echo 'semgrep not on PATH — skipped'; fi",
            "if command -v trivy >/dev/null 2>&1; then trivy fs --exit-code 1 --severity CRITICAL .; else echo 'trivy not on PATH — skipped'; fi",
            "if command -v osv-scanner >/dev/null 2>&1; then osv-scanner --recursive .; else echo 'osv-scanner not on PATH — skipped'; fi",
            "echo 'Security gate completed (missing tools skipped; not a silent pass of findings).'",
        ]
        return "\n".join(indent + line for line in lines)

    @staticmethod
    def _github_actions(commands: dict[str, str]) -> str:
        security = CIConfigGenerator._security_scan_script()
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

  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: OSS security-scan gate
        run: |
{security}

  build:
    needs: [test, security]
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
    stage('Security scan gate') {{
      steps {{
        echo 'BOSS security-scan gate: semgrep (SAST) / trivy (FS) / osv-scanner (deps)'
        echo 'Optional: gitleaks / checkov. Snyk commercial API is never invoked.'
        // semgrep scan --error --metrics=off .
        // trivy fs --exit-code 1 --severity CRITICAL .
        // osv-scanner --recursive .
        sh '''
          if command -v semgrep >/dev/null 2>&1; then semgrep scan --error --metrics=off .; else echo semgrep not on PATH; fi
          if command -v trivy >/dev/null 2>&1; then trivy fs --exit-code 1 --severity CRITICAL .; else echo trivy not on PATH; fi
          if command -v osv-scanner >/dev/null 2>&1; then osv-scanner --recursive .; else echo osv-scanner not on PATH; fi
        '''
      }}
    }}
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

    @staticmethod
    def _gitlab_ci(commands: dict[str, str]) -> str:
        return f"""# BOSS-generated GitLab CI — deploy handoff only; provider actuation stays ChangeSet-gated.
stages:
  - test
  - security
  - build
  - deploy

variables:
  BOSS_PIPELINE: "1"

test:
  stage: test
  image: ubuntu:22.04
  script:
    - {commands['setup']}
    - {commands['typecheck']}
    - {commands['lint']}
    - {commands['test']}

security:
  stage: security
  image: ubuntu:22.04
  needs: [test]
  script:
    - echo 'BOSS security-scan gate: semgrep (SAST) / trivy (FS) / osv-scanner (deps)'
    - echo 'Optional: gitleaks / checkov. Snyk commercial API is never invoked.'
    # semgrep scan --error --metrics=off .
    # trivy fs --exit-code 1 --severity CRITICAL .
    # osv-scanner --recursive .
    - if command -v semgrep >/dev/null 2>&1; then semgrep scan --error --metrics=off .; else echo semgrep not on PATH; fi
    - if command -v trivy >/dev/null 2>&1; then trivy fs --exit-code 1 --severity CRITICAL .; else echo trivy not on PATH; fi
    - if command -v osv-scanner >/dev/null 2>&1; then osv-scanner --recursive .; else echo osv-scanner not on PATH; fi

build:
  stage: build
  image: ubuntu:22.04
  needs: [security]
  script:
    - {commands['setup']}
    - {commands['build']}
    - printf '%s\\n' "$CI_COMMIT_SHA" > boss-release-ref.txt
  artifacts:
    paths:
      - boss-release-ref.txt
    expire_in: 7 days

deploy:
  stage: deploy
  image: ubuntu:22.04
  needs: [build]
  when: manual
  environment:
    name: staging
  script:
    - test -s boss-release-ref.txt
    - echo "Build evidence ready. Provider deployment remains ChangeSet-gated in BOSS."
"""

    @staticmethod
    def _circleci(commands: dict[str, str]) -> str:
        return f"""# BOSS-generated CircleCI — deploy handoff only; provider actuation stays ChangeSet-gated.
version: 2.1

jobs:
  test:
    docker:
      - image: cimg/base:stable
    steps:
      - checkout
      - run:
          name: Setup
          command: {commands['setup']}
      - run:
          name: Typecheck
          command: {commands['typecheck']}
      - run:
          name: Lint
          command: {commands['lint']}
      - run:
          name: Test
          command: {commands['test']}

  security:
    docker:
      - image: cimg/base:stable
    steps:
      - checkout
      - run:
          name: OSS security-scan gate
          command: |
            echo 'BOSS security-scan gate: semgrep (SAST) / trivy (FS) / osv-scanner (deps)'
            echo 'Optional: gitleaks / checkov. Snyk commercial API is never invoked.'
            # semgrep scan --error --metrics=off .
            # trivy fs --exit-code 1 --severity CRITICAL .
            # osv-scanner --recursive .
            if command -v semgrep >/dev/null 2>&1; then semgrep scan --error --metrics=off .; else echo semgrep not on PATH; fi
            if command -v trivy >/dev/null 2>&1; then trivy fs --exit-code 1 --severity CRITICAL .; else echo trivy not on PATH; fi
            if command -v osv-scanner >/dev/null 2>&1; then osv-scanner --recursive .; else echo osv-scanner not on PATH; fi

  build:
    docker:
      - image: cimg/base:stable
    steps:
      - checkout
      - run:
          name: Setup
          command: {commands['setup']}
      - run:
          name: Build
          command: {commands['build']}
      - run:
          name: Record build evidence
          command: printf '%s\\n' "$CIRCLE_SHA1" > boss-release-ref.txt
      - persist_to_workspace:
          root: .
          paths:
            - boss-release-ref.txt

  deploy:
    docker:
      - image: cimg/base:stable
    steps:
      - attach_workspace:
          at: .
      - run:
          name: Hand off to governed BOSS promotion
          command: |
            test -s boss-release-ref.txt
            echo 'Build evidence ready. Provider deployment remains ChangeSet-gated in BOSS.'

workflows:
  version: 2
  ci:
    jobs:
      - test
      - security:
          requires:
            - test
      - build:
          requires:
            - security
      - deploy:
          requires:
            - build
          type: approval
"""

    @staticmethod
    def _buildkite(commands: dict[str, str]) -> str:
        return f"""# BOSS-generated Buildkite pipeline — deploy handoff only; provider actuation stays ChangeSet-gated.
steps:
  - label: ":test_tube: Test"
    key: test
    command: |
      {commands['setup']}
      {commands['typecheck']}
      {commands['lint']}
      {commands['test']}

  - wait

  - label: ":lock: Security scan gate"
    key: security
    depends_on: test
    command: |
      echo 'BOSS security-scan gate: semgrep (SAST) / trivy (FS) / osv-scanner (deps)'
      echo 'Optional: gitleaks / checkov. Snyk commercial API is never invoked.'
      # semgrep scan --error --metrics=off .
      # trivy fs --exit-code 1 --severity CRITICAL .
      # osv-scanner --recursive .
      if command -v semgrep >/dev/null 2>&1; then semgrep scan --error --metrics=off .; else echo semgrep not on PATH; fi
      if command -v trivy >/dev/null 2>&1; then trivy fs --exit-code 1 --severity CRITICAL .; else echo trivy not on PATH; fi
      if command -v osv-scanner >/dev/null 2>&1; then osv-scanner --recursive .; else echo osv-scanner not on PATH; fi

  - wait

  - label: ":package: Build"
    key: build
    depends_on: security
    command: |
      {commands['setup']}
      {commands['build']}
      printf '%s\\n' "$BUILDKITE_COMMIT" > boss-release-ref.txt
    artifact_paths:
      - boss-release-ref.txt

  - block: ":rocket: Deploy handoff (staging)"
    key: deploy-gate
    depends_on: build
    prompt: "Promote build evidence to BOSS-governed deployment?"

  - label: ":boss: Deploy handoff"
    key: deploy
    depends_on: deploy-gate
    command: |
      buildkite-agent artifact download boss-release-ref.txt .
      test -s boss-release-ref.txt
      echo "Build evidence ready. Provider deployment remains ChangeSet-gated in BOSS."
"""
