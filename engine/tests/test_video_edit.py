"""Video editing as a BOSS capability.

These are `builtin`, not `sandboxed_command`, and the distinction is the whole safety argument:
BOSS owns every argument vector, the operator supplies only values that are validated against a
fixed set or a numeric range, and nothing is ever interpolated into a shell string.
"""
import os
import subprocess

import pytest

from boss_engine.services import video_edit as v


pytestmark = pytest.mark.skipif(
    not v.ffmpeg_available(), reason="ffmpeg is not installed in this environment")

D = "/Users/param/Downloads"
FIXTURES = pytest.mark.skipif(
    not os.path.exists(f"{D}/title-intro.mp4"),
    reason="the operator's sample clips are not present")


def test_a_timecode_that_is_not_a_timecode_is_refused():
    """The values that reach ffmpeg are validated, not quoted.

    Quoting is what you do when a string will be parsed by a shell. Nothing here is: the
    refusal is what makes an operator value unable to become anything but a timecode.
    """
    for bad in ("; rm -rf /", "10s", "$(whoami)", "1:2", "", "--help"):
        with pytest.raises(v.VideoEditError):
            v._checked_timecode(bad, "start")
    for good in ("0", "90", "90.5", "00:01:30", "01:02:03.250"):
        assert v._checked_timecode(good, "start") == good


def test_a_corner_is_a_fixed_vocabulary():
    """`corner` becomes part of a filter expression, so it may never be free text."""
    assert set(v.CORNERS) == {"top_right", "top_left", "bottom_right", "bottom_left"}
    with pytest.raises(v.VideoEditError):
        v.watermark("/nonexistent.mp4", image="/nonexistent.png",
                    work_dir="/tmp", corner="top-right")


def test_no_shell_is_ever_used():
    """The safety argument rests entirely on this.

    An argv list with shell=False means an operator value containing a semicolon is a filename
    containing a semicolon, not a second command.
    """
    import inspect

    source = inspect.getsource(v._run)
    assert "shell=False" in source
    assert "shell=True" not in source
    # And the only executables named anywhere are ffmpeg's own.
    whole = inspect.getsource(v)
    for argv_head in ('"ffmpeg"', '"ffprobe"'):
        assert argv_head in whole
    assert "os.system" not in whole
    assert "subprocess.call" not in whole


def test_scale_and_margin_are_bounded():
    for kwargs in ({"scale": 2.0}, {"scale": 0.0}, {"margin": -1}, {"margin": 10_000},
                   {"opacity": 1.5}):
        with pytest.raises(v.VideoEditError):
            v.watermark("/nonexistent.mp4", image="/nonexistent.png",
                        work_dir="/tmp", **kwargs)


@FIXTURES
def test_a_silent_clip_gets_silence_rather_than_an_error(tmp_path):
    """A thank-you slide usually has no audio track.

    Referring to its audio stream fails with "Stream specifier ':a' … matches no streams",
    which reads as a broken filter rather than as an ordinary silent clip. concat requires
    every segment to carry the same streams, so the missing one is synthesised at the right
    length. This is a real file from the operator's own channel, not a constructed case.
    """
    outro = f"{D}/nandri_thankyou_with_logo.mp4"
    assert v.probe(outro)["has_audio"] is False, "fixture changed; it used to be silent"

    out = v.bookend(f"{D}/title-intro.mp4", work_dir=str(tmp_path), outro=outro)
    info = v.verify_playable(out)
    # The joined result HAS audio — the intro's, plus silence where the outro had none.
    assert info["has_audio"] is True
    assert info["duration_seconds"] > 14   # 10s intro + 5s outro


@FIXTURES
def test_the_whole_chain_leaves_its_inputs_untouched(tmp_path):
    """Every step writes a NEW file.

    A pipeline that edits in place cannot be retried and cannot be inspected after a bad
    result — and here the input is the only copy of somebody's video.
    """
    source = f"{D}/title-intro.mp4"
    before = (os.path.getsize(source), os.path.getmtime(source))

    wm = v.watermark(source, image=f"{D}/logo.png", work_dir=str(tmp_path))
    assert wm != source
    assert os.path.getsize(source), os.path.getmtime(source) == before

    final = v.bookend(wm, work_dir=str(tmp_path), outro=f"{D}/nandri_thankyou_with_logo.mp4")
    assert final not in (source, wm)
    assert (os.path.getsize(source), os.path.getmtime(source)) == before


@FIXTURES
def test_a_logo_is_sized_relative_to_the_video_not_in_pixels(tmp_path):
    """A logo sized for 1080p is a sticker on 4K and unreadable on a vertical crop."""
    out = v.watermark(f"{D}/title-intro.mp4", image=f"{D}/logo.png",
                      work_dir=str(tmp_path), scale=0.12)
    info = v.verify_playable(out)
    # Overlaying a logo must not change the frame, and must not touch the audio.
    source_info = v.probe(f"{D}/title-intro.mp4")
    assert (info["width"], info["height"]) == (source_info["width"], source_info["height"])
    assert info["has_audio"] is True


def test_a_render_that_produced_no_video_is_not_reported_as_success(tmp_path):
    """ffmpeg exits 0 on outputs that are empty, silent, or a fraction of the intended length.

    `verify_playable` is what turns "the step succeeded" into something worth believing, and
    it is why the publish step can require evidence rather than a status.
    """
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(v.VideoEditError):
        v.verify_playable(str(empty))

    missing = tmp_path / "nothing.mp4"
    with pytest.raises(v.VideoEditError):
        v.verify_playable(str(missing))


@FIXTURES
def test_a_silent_render_fails_rather_than_uploads(tmp_path):
    """A silent publish is almost never intended."""
    silent = f"{D}/nandri_thankyou_with_logo.mp4"
    with pytest.raises(v.VideoEditError, match="no audio"):
        v.verify_playable(silent, expect_audio=True)
    # And the caller can opt out deliberately when silence IS the point.
    assert v.verify_playable(silent, expect_audio=False)["duration_seconds"] > 0
