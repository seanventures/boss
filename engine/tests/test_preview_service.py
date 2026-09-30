

def test_finished_previews_are_eventually_forgotten():
    """Expiring marked rows but never removed them, so the table only grew.

    Measured on goldeneye: 50 rows, 47 of them expired. That is not merely untidy — the list
    returns the newest N rows WHATEVER their status and callers filter for live ones after, so
    a window that is 94% dead can push a genuinely running preview for an older app out of it.
    The preview exists and the app's card cannot see it.
    """
    import inspect
    from boss_engine.services import preview_service

    src = inspect.getsource(preview_service.list_previews)
    assert "DELETE FROM app_previews" in src
    # Only finished ones. Deleting a running preview would orphan the process it tracks.
    assert "status IN ('expired','stopped','failed')" in src
    assert "running" not in src.split("DELETE FROM app_previews")[1].split(")")[0]
    # And only once they are old: a preview's life is an hour, so a week is well clear of it.
    assert "interval '7 days'" in src
