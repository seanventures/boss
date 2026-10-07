"""checkup.sh named its containers boss-community-<svc>-1, so on a machine with a second install
(another COMPOSE_PROJECT_NAME — a test stack, a staging copy) it checked the WRONG stack and called
it healthy (R2). Every container it touches derives from the compose project name."""
from __future__ import annotations

import re
from pathlib import Path

CHECKUP = Path(__file__).resolve().parents[1] / "install" / "checkup.sh"


def test_checkup_names_no_container_of_a_fixed_stack():
    text = CHECKUP.read_text(encoding="utf-8")
    fixed = re.findall(r"boss-community-(?:api|web|db|worker|infisical|egress)\b", text)
    assert not fixed, f"hardcoded container names in checkup.sh: {sorted(set(fixed))}"
    assert 'STACK="${COMPOSE_PROJECT_NAME:-boss-community}"' in text
    assert "COMPOSE_PROJECT_NAME" in text.split("for key in", 1)[1].split("\n", 1)[0], \
        "checkup.sh must read COMPOSE_PROJECT_NAME from .env like the other settings"
