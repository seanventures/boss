# Community GA installer proof (BOSS-436 / BOSS-455)

Worktree: `/Volumes/DataSSD1/boss-community-ga-integration/swarms/installer`
Branch: `ai/grok/ga-installer`
Base: `371a1c72a1fd49f3336f5790b55407d51886412a`

The Community installer lifecycle was already implemented in `install.sh` and `scripts/install/`. This change does not rewrite it. It disables the uncleared Claude session-token import, keeps CLI session material out of backup archives, and locks those behaviors with tests. CLI-owned Claude sign-in (`claude auth login` as `boss` inside the API container, then `--connect-claude-local`) stays.

## Counsel clearance was absent

Repo search found one counsel reference: `01049940` in `docs/operations/OSS_LICENSING_DECISION.md` (also cited from `docs/planning/oss-readiness/DECISION-oss-licensing-strategy.md` and `docs/operations/GA_LIVING_PLAN.md`). That decision is Apache-2.0 for the first-party Community export. It does not record provider or counsel clearance to copy a Claude session token into BOSS. No other clearance document was found. Legal approval was not invented. `docs/community/MODEL_CONNECTIONS.md` records the import as Disabled for that reason.

## Session-token import is disabled

These entry points refuse before they read or store a Claude setup token:

- `install.sh --connect-claude` dies immediately after argument parsing.
- `scripts/install/coding_subscriptions.py` (`--connect-claude` and `claude_credential()`) raises `RuntimeError`.
- `scripts/store_subscription_token.py` for provider `claude_code` exits before stdin or file read.
- `scripts/install/remote.sh subscription --provider claude_code` dies before stdin or `--token`.
- `engine/boss_engine/services/setup_service.py` `store_coding_agent_credential` raises `SetupError` for `claude_code` before `put_secret`.
- `apps/web/app/setup-ui.tsx` `storeTokenAndConnect` returns before the credential API when the provider is `claude_code`.

Codex auth-file import (`--connect-codex`, `CODEX_AUTH_JSON`) remains. A pre-existing `CLAUDE_CODE_OAUTH_TOKEN` environment value is not this import path; the harness spec still names that variable. Error text does not echo the token.

## Backup excludes CLI session material

`scripts/install/backup.sh` writes `CLAUDE_CODE_OAUTH_TOKEN=` and `CODEX_AUTH_JSON=` into the archive `.env` and leaves the live `.env` unchanged. `BOSS_CONSOLE_SESSION_SECRET` stays in the archive. Secret byte strings of length 20 or more are replaced in every backup file; if any remain, the backup directory is deleted and the command fails. The Claude CLI home volume (`/home/boss/.claude`) is not archived. There is no `restore.sh`. Restore is the manual compose / `pg_restore` procedure in `scripts/install/README.md`, plus the installer's own reinstall-from-backup path.

## Lifecycle behaviors proved

Tests use temp directories and fake `docker`, `curl`, `ssh`, and package commands. They do not call model providers, GitHub releases, or package registries, and they do not touch the host BOSS or Docker stack.

| Behavior | Proof |
|---|---|
| Dependency provisioning | `tests/test_vm_installer_sudo.py::test_vm_dependencies_use_noninteractive_package_command` (Ubuntu 24.04 fixture, noninteractive `apt-get`, fake commands). Supported distro checks live in `install.sh` `install_vm_deps`. |
| Local secrets and safe rerun | `tests/test_installer_lifecycle.py::test_installer_rerun_preserves_existing_credentials`, `tests/test_a_rerun_keeps_the_secret_store.py`, `scripts/tests/test_installer_lifecycle_safety.py::test_installer_rerun_keeps_headless_choice_and_credentials`. |
| Loopback services | `tests/test_vm_installer_safety.py::test_public_bind_option_is_rejected_before_any_install_action`, `tests/test_a_rerun_keeps_the_secret_store.py::test_custom_ports_survive_and_legacy_public_bind_becomes_loopback`. Compose bind stays `127.0.0.1`; `--bind-address` is rejected. |
| Clear errors, no silent failure | Installer `die` paths, `test_seed_failure_leaves_worker_stopped`, `test_upgrade_backup_failure_leaves_checkout_and_env_unchanged`, `test_remote_model_error_with_provider_field_is_not_success`, and the import-refusal tests below. |
| Upgrade with backup | `engine/tests/test_community_installer_upgrade_backup.py` (`test_pasted_installer_backs_up_before_switching_source`, `test_backup_failure_leaves_checkout_and_credentials_untouched`, `test_upgrade_forwards_backup_location_and_uses_one_fresh_snapshot`). `upgrade.sh` requires `FETCH_HEAD:scripts/install/backup.sh` and runs the fetched `install.sh`, which creates the backup. |
| Backup and restore | `tests/test_installer_lifecycle.py::test_backup_output_excludes_cli_session_material`; `test_backup_preserves_running_database_and_worker_state`; `test_changed_source_reinstall_restores_stopped_database`. Manual restore commands are in `scripts/install/README.md`. |
| Safe uninstall and reinstall | Safe uninstall is `compose ... stop` (stopped containers and image IDs kept; no `down`, no `--volumes`): `test_dry_run_and_safe_uninstall_preserve_existing_env`, `test_piped_uninstall_uses_explicit_install_dir`, `test_safe_uninstall_keeps_stopped_container_for_a_later_backup`. Purge is a separate confirmed path (`test_purge_requires_confirmation_even_with_yes`, `test_failed_backup_cancels_purge_before_compose_down`). |
| Immutable installer script / checkout ref | `test_fresh_install_accepts_exact_commit_ref`, `test_published_one_liner_uses_one_commit_and_propagates_download_failure`, `test_unreleased_one_liner_placeholder_stops_before_download`. A 40-hex `BOSS_REPO_REF` is checked. The published one-liner still contains `REPLACE_WITH_REVIEWED_40_CHARACTER_SHA` and stops before download. |

Import refusal tests: `test_connect_claude_import_cannot_run`, `test_claude_setup_token_import_cannot_run` (coding subscriptions, store-subscription-token, and remote), `test_storing_refuses_a_claude_session_token`, and `apps/web/tests/setup-wizard.test.ts` ("does not let the console run the Claude setup-token import").

## Commands and results

From this worktree, 2026-09-29:

```
bash -n install.sh scripts/install/backup.sh scripts/install/remote.sh scripts/install/uninstall.sh scripts/install/upgrade.sh
```

Result: exit 0.

```
cd /Volumes/DataSSD1/boss-community-ga-integration/swarms/installer
PYTHONPATH="/Volumes/DataSSD1/boss-community-ga-integration/swarms/installer/engine${PYTHONPATH:+:$PYTHONPATH}" \
  /Volumes/DataSSD1/boss-community-ga-integration/boss/.venv311/bin/pytest -q \
  tests/test_installer_lifecycle.py \
  tests/test_installer_coding_subscriptions.py \
  tests/test_vm_installer_safety.py \
  tests/test_installer_startup_order.py \
  tests/test_a_rerun_keeps_the_secret_store.py \
  scripts/tests/test_installer_lifecycle_safety.py \
  engine/tests/test_store_subscription_token.py \
  engine/tests/test_a_subscription_travels_as_a_file.py \
  engine/tests/test_community_installer_coding_subscriptions.py \
  engine/tests/test_community_installer_upgrade_backup.py \
  engine/tests/test_claude_code_is_connectable_too.py \
  --tb=line
```

Result: `141 passed, 1 skipped in 29.01s`, exit 0. The skip is `test_real_compose_backup_keeps_stopped_database_image_with_drifted_tag` unless `BOSS_RUN_DOCKER_LIFECYCLE_TEST=1`. It was not run. `COMPOSE_PROJECT_NAME=boss-ga-installer` was not required.

```
PYTHONPATH=... pytest -q tests/test_vm_installer_sudo.py --tb=line
```

Result: `3 passed in 1.78s`, exit 0.

```
cd apps/web && ./node_modules/.bin/vitest run tests/setup-wizard.test.ts
```

`node_modules` was a temporary symlink to the integration checkout's `apps/web/node_modules` and was removed after the run. Result: `54 passed`, exit 0. The worktree `apps/web/node_modules` path is absent.

## Not run

- Live Claude, Codex, or Grok test-account connection. Fakes only.
- A public HTTPS one-line installer URL. The one-liner still uses the unreleased SHA placeholder.
- The real Docker compose backup/restore test. It stays skipped so this host's BOSS stack is untouched.
- Plane. `OPS_BASE_URL` and `OPS_TOKEN` were unset, so BOSS-436 / BOSS-455 were not updated. This file is not a Plane update.
