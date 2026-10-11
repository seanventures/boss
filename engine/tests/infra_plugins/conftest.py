import pytest

from ._harness import docker_ok, teardown_run


@pytest.fixture(scope="session", autouse=True)
def _teardown_everything_this_run_labelled():
    yield
    if docker_ok():
        teardown_run()
