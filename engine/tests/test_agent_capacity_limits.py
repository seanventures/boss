from boss_engine.services import admission


def test_pinned_builds_cannot_exceed_host_capacity(monkeypatch):
    monkeypatch.setattr(admission, "_state", lambda *_: {"value": 4})
    monkeypatch.setattr(admission, "running_previews_mb", lambda *_: 0)
    monkeypatch.setattr(admission, "derived_slots", lambda *_args, **_kwargs: 2)
    result = admission.builds_at_once(None, admission.Capacity(8192, 4096, cores=4), 1024)
    assert result["value"] == 2
    assert result["pinned"] == 4
    assert result["warning"]


def test_an_idle_queue_still_runs_one_build_under_load_until_the_doctor_pauses():
    """Nothing running means one build may run, even on a loaded host (never none forever);
    the system doctor's L2 still holds it, and a person's "build anyway" passes L2."""
    from boss_engine.services.workload_profiles import DEFAULTS

    profile = DEFAULTS["code.task"]
    for capacity in (admission.Capacity(8192, 4096, cores=4, load_per_core=2),
                     admission.Capacity(8192, 4096, cores=4, psi_full_avg60=10)):
        assert admission.decide(capacity, profile, in_flight=0, slots=3).admitted
        assert not admission.decide(capacity, profile, in_flight=0, slots=3,
                                    level=admission.L2).admitted
        assert admission.decide(capacity, profile, in_flight=0, slots=3, level=admission.L2,
                                force_one=True).admitted
        assert not admission.decide(capacity, profile, in_flight=1, slots=3).admitted


def test_job_slots_are_measured_until_the_operator_saves_them(monkeypatch):
    state = {}
    monkeypatch.setattr(admission, "_state", lambda _c, key: state.get(key, {}))
    monkeypatch.setattr(admission, "load_profile", lambda *_: type("Pr", (), {"request_mb": 1024})())
    monkeypatch.setattr(admission, "builds_at_once", lambda *_a, **_k: {"value": 2, "auto": True})
    monkeypatch.setattr(admission, "heavy_in_flight", lambda *_: 0)
    monkeypatch.setattr(admission, "current_level", lambda *_: 0)
    probe = type("P", (), {"shortfall": None, "as_dict": lambda self: {}})()
    monkeypatch.setattr(admission, "decide", lambda *_a, **_k: probe)

    class Conn:
        def execute(self, *_a, **_k):
            return type("R", (), {"scalar": lambda self: 0})()

    cap = admission.Capacity(8192, 4096, cores=4)
    # Unsaved: measured (2 heavy at 1 GB leave ~4.8 GB for thinking builds), at most 4.
    assert admission.snapshot(Conn(), cap=cap)["worker_slots"] == 4
    small = admission.Capacity(3072, 2048, cores=2)
    assert admission.snapshot(Conn(), cap=small)["worker_slots"] == 2   # never below the heavy
    state["agent_capacity"] = {"worker_slots": 4}
    assert admission.snapshot(Conn(), cap=cap)["worker_slots"] == 4
