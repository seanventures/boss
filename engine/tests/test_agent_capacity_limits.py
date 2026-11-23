from boss_engine.services import admission


def test_pinned_builds_cannot_exceed_host_capacity(monkeypatch):
    monkeypatch.setattr(admission, "_state", lambda *_: {"value": 4})
    monkeypatch.setattr(admission, "running_previews_mb", lambda *_: 0)
    monkeypatch.setattr(admission, "derived_slots", lambda *_args, **_kwargs: 2)
    result = admission.builds_at_once(None, admission.Capacity(8192, 4096, cores=4), 1024)
    assert result["value"] == 2
    assert result["pinned"] == 4
    assert result["warning"]


def test_idle_queue_does_not_bypass_cpu_or_memory_pressure():
    from boss_engine.services.workload_profiles import DEFAULTS

    profile = DEFAULTS["code.task"]
    for capacity in (admission.Capacity(8192, 4096, cores=4, load_per_core=2),
                     admission.Capacity(8192, 4096, cores=4, psi_full_avg60=10)):
        assert not admission.decide(capacity, profile, in_flight=0, slots=3).admitted
    recovered = admission.Capacity(8192, 7000, cores=4)
    assert admission.decide(recovered, profile, in_flight=0, slots=3).admitted
