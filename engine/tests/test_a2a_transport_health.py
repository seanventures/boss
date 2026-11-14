"""A2A transport health is honest: unconfigured is not degraded."""
from boss_engine.services.agent_bus import RedisStreamsTransport


def test_missing_redis_url_is_not_configured_not_degraded():
    health = RedisStreamsTransport(None).health()
    assert health["configured"] is False
    assert health["status"] == "not_configured"
    assert health["status"] != "degraded"


def test_blank_redis_url_is_not_configured():
    health = RedisStreamsTransport("").health()
    assert health["configured"] is False
    assert health["status"] == "not_configured"
