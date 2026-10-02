"""A docker compose command typed from the docs must start the images the installer built.

The installer exports BOSS_COMMUNITY_IMAGE_TAG (default: the compose project, boss-community)
and builds boss-community-*:boss-community. The compose file fell back to ":local" when the
variable was unset — and it is unset for every command the docs tell an operator to run by hand,
such as the "add a hostname to a running install" recreate. On a VM installed with v1.0-rc1 and
then upgraded, that recreate quietly put the month-old rc1 console back up (no sign-in page);
on a fresh install it fails for want of an image. Found 2026-09-30.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_compose_falls_back_to_the_tag_the_installer_builds():
    installer = (ROOT / "install.sh").read_text()
    project = re.search(r'COMPOSE_PROJECT_NAME="\$\{COMPOSE_PROJECT_NAME:-([^}]+)\}"', installer).group(1)
    assert 'BOSS_COMMUNITY_IMAGE_TAG="${BOSS_COMMUNITY_IMAGE_TAG:-$COMPOSE_PROJECT_NAME}"' in installer
    compose = (ROOT / "scripts" / "install" / "community-compose.yml").read_text()
    tags = re.findall(r"image: boss-community-\w+:\$\{BOSS_COMMUNITY_IMAGE_TAG:-([^}]+)\}", compose)
    assert len(tags) == 3, tags
    assert set(tags) == {project}
