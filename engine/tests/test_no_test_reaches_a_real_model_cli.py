"""The engine suite never starts a real model CLI (conftest._no_real_model_cli)."""
import subprocess

import pytest

from .conftest import RealModelCliBlocked


@pytest.mark.parametrize("binary", ["claude", "codex", "/Users/x/.local/bin/claude"])
def test_the_guard_trips_when_a_test_starts_a_model_cli(binary):
    with pytest.raises(RealModelCliBlocked):
        subprocess.run([binary, "--version"], capture_output=True)


def test_other_programs_still_run():
    assert subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout.strip() == "ok"
