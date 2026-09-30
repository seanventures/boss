"""Community Cloud startup must not load the private hosted services."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parents[2]


def test_community_cloud_services_import_without_private_modules():
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

                class BlockPrivateServices(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname in {
                            'boss_engine.services.commercial',
                            'boss_engine.services.cloud_product',
                        }:
                            raise ImportError('private service deliberately unavailable')
                        return None

                sys.meta_path.insert(0, BlockPrivateServices())
                from boss_engine.services.community_edition import CommunityEditionService
                from boss_engine.services.community_cloud import CommunityCloudService

                settings = get_settings().model_copy(update={'boss_edition': 'community'})
                edition = CommunityEditionService(settings)
                cloud = CommunityCloudService(settings, edition)
                assert edition.normalize_edition() == 'community'
                assert cloud.edition_service is edition
                for method in ('catalog', 'status', 'comparison', 'capabilities',
                               'easy_connect', 'poll_easy_connect', 'complete_easy_connect', 'unlink'):
                    assert callable(getattr(cloud, method))
                assert 'boss_engine.services.commercial' not in sys.modules
                assert 'boss_engine.services.cloud_product' not in sys.modules
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
