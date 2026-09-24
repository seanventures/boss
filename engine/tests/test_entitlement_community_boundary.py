"""The local entitlement broker must import without hosted billing code."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[2]


def test_local_broker_import_and_pack_selection_without_paid_module():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = f"{ROOT}:{ROOT / 'engine'}"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            dedent(
                """
                import importlib.abc
                import sys
                from boss_engine.config import get_settings

                class BlockPaidBilling(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname in {
                            'boss_engine.services.commercial',
                            'boss_engine.services.enterprise_gateway',
                        }:
                            raise ImportError('paid service deliberately unavailable')
                        return None

                sys.meta_path.insert(0, BlockPaidBilling())
                from boss_engine.services.entitlements import EntitlementBrokerService
                settings = get_settings().model_copy(update={
                    'boss_edition': 'community',
                    'local_model_enabled': True,
                    'local_model_id': 'local-test-model',
                })
                broker = EntitlementBrokerService(settings)
                assert broker._edition_skill_bundles(None, 'project') == broker._pack_skill_bundles('community_edition')
                assert broker._runtime_flags('openai_codex_chatgpt')[0] == bool(settings.openai_codex_oauth_enabled)
                assert broker._runtime_flags('approved_local_model_host') == (True, True)
                assert broker._paid_gateway is None
                assert {'managed_cloud_model_gateway', 'approved_local_model_host'} <= set(broker._source_record_defaults())
                assert all(source.get('entitlement_kind') != 'enterprise_gateway' for source in broker._source_record_defaults().values())
                assert 'boss_engine.services.commercial' not in sys.modules
                assert 'boss_engine.services.enterprise_gateway' not in sys.modules
                """
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
