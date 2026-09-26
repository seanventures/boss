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


# ---------------------------------------------------------------------------------------------
# The vibe-editing primitives: cut, reframe, overlay_image, caption.
#
# These tests BUILD THEIR OWN SOURCE rather than reaching into the operator's Downloads folder,
# because the four above them skip on a machine that lacks those files — and a safety test that
# skips in CI is a safety test that does not exist. ffmpeg can synthesise a clip with a real
# video stream and real audio, which is all any of these need.
# ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A 12-second 640x360 landscape clip with a tone, so cuts and crops are measurable."""
    path = str(tmp_path_factory.mktemp("clip") / "src.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=12",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path],
        check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def logo(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("logo") / "logo.png")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:size=64x64:duration=1",
                    "-frames:v", "1", path], check=True, capture_output=True)
    return path


def test_a_cut_removes_the_ranges_and_keeps_the_rest(clip, tmp_path):
    """"Take out the bit where I fumble" is a removal, and the result has to be shorter.

    The arithmetic is the point. 12 seconds minus 3–5 and 8–9 is 9, and a filter graph that
    silently kept everything would still exit 0 and still play — which is how a cut that did
    nothing would otherwise reach a human reviewer looking correct.
    """
    out = v.cut(clip, remove=[{"start": "3", "end": "5"}, {"start": "8", "end": "9"}],
                work_dir=str(tmp_path))
    info = v.verify_playable(out)
    assert abs(info["duration_seconds"] - 9.0) < 0.35, info["duration_seconds"]
    assert info["has_audio"] is True, "a cut must not drop the audio track"


def test_overlapping_removals_remove_their_union_rather_than_failing(clip, tmp_path):
    """An operator writing ranges by hand overlaps them, and that is not an error.

    Naively this produces a zero-length keep between the two, which ffmpeg refuses to encode —
    so the editing step would fail on an instruction whose meaning was never ambiguous.
    """
    out = v.cut(clip, remove=[{"start": "3", "end": "6"}, {"start": "5", "end": "8"}],
                work_dir=str(tmp_path))
    # 12 - (3..8) = 7, not 12 - 3 - 3 = 6.
    assert abs(v.probe(out)["duration_seconds"] - 7.0) < 0.35


def test_a_cut_that_would_leave_nothing_is_refused(clip, tmp_path):
    with pytest.raises(v.VideoEditError):
        v.cut(clip, remove=[{"start": "0", "end": "12"}], work_dir=str(tmp_path))


def test_a_removal_range_is_validated_like_every_other_timecode(clip, tmp_path):
    for bad in ([{"start": "5", "end": "3"}],          # backwards
                [{"start": "$(id)", "end": "5"}],      # not a timecode
                [{"start": "1"}],                      # half a range
                ["3-5"],                               # not a range at all
                []):                                   # nothing to do
        with pytest.raises(v.VideoEditError):
            v.cut(clip, remove=bad, work_dir=str(tmp_path))


def test_reframe_makes_a_landscape_clip_vertical(clip, tmp_path):
    """"Make it portrait" is the most common instruction in the set, and it must CROP.

    Padding would technically produce a 9:16 file — with the original letterboxed inside it and
    black bars top and bottom, which is the thing vertical video exists to avoid.
    """
    out = v.reframe(clip, aspect="9:16", work_dir=str(tmp_path))
    info = v.verify_playable(out)
    ratio = info["width"] / info["height"]
    assert abs(ratio - 9 / 16) < 0.02, (info["width"], info["height"])
    # Cropping, not padding: the tall side keeps the source's height.
    assert info["height"] == v.probe(clip)["height"]
    assert info["has_audio"] is True


def test_an_aspect_is_a_fixed_vocabulary(clip, tmp_path):
    """The ratio becomes part of a crop expression, so it may never be free text."""
    assert set(v.ASPECTS) == {"9:16", "16:9", "1:1", "4:5"}
    for bad in ("9x16", "1920:1080", "iw/2:ih", ""):
        with pytest.raises(v.VideoEditError):
            v.reframe(clip, aspect=bad, work_dir=str(tmp_path))


def test_a_strategy_this_cannot_honour_is_refused_rather_than_approximated(clip, tmp_path):
    """Silently centre-cropping when asked to follow the speaker would crop out the speaker.

    Refusing names the gap; doing something else hides it in a file a human then approves.
    """
    with pytest.raises(v.VideoEditError):
        v.reframe(clip, aspect="9:16", work_dir=str(tmp_path), strategy="face")


def test_an_image_pops_up_while_the_audio_keeps_playing(clip, logo, tmp_path):
    """This is the operator's own use case, and the audio is the half that gets lost.

    Re-encoding audio for a video-only change is both slower and lossy; the risk is a filter
    graph that drops the track entirely, which looks like success until somebody watches it.
    """
    out = v.overlay_image(clip, image=logo, start="2", end="4", work_dir=str(tmp_path))
    info = v.verify_playable(out)
    assert info["has_audio"] is True, "the audio must survive a visual-only edit"
    source = v.probe(clip)
    assert (info["width"], info["height"]) == (source["width"], source["height"])
    assert abs(info["duration_seconds"] - source["duration_seconds"]) < 0.35


def test_a_position_is_a_fixed_vocabulary(clip, logo, tmp_path):
    assert "center" in v.POSITIONS and set(v.CORNERS) <= set(v.POSITIONS)
    with pytest.raises(v.VideoEditError):
        v.overlay_image(clip, image=logo, start="2", end="4",
                        work_dir=str(tmp_path), position="middle-ish")


def test_an_overlay_window_must_be_a_window(clip, logo, tmp_path):
    for start, end in (("4", "2"), ("4", "4")):
        with pytest.raises(v.VideoEditError):
            v.overlay_image(clip, image=logo, start=start, end=end, work_dir=str(tmp_path))


def test_caption_text_never_reaches_a_filter_expression(clip, tmp_path):
    """Caption text is the one input that is genuinely free-form prose.

    drawtext parses its own options, so a colon or a single quote in ordinary English ends the
    option list — and `text=` is where an instruction file could otherwise smuggle in
    `drawtext`'s own `textfile` or a second filter. BOSS writes the prose to a file and passes
    the PATH, which makes the content unparseable as options no matter what it says.
    """
    import inspect
    import re

    source = inspect.getsource(v.caption)
    assert "textfile=" in source, "caption must pass a path, not the prose"

    # The filter chain is the only string that reaches ffmpeg's parser. The prose variable must
    # not appear anywhere in it — `drawtext=textfile=<path>` is the whole point, and the path is
    # generated here rather than supplied.
    chain = re.search(r"chain = \((.*?)\n    \)", source, re.S)
    assert chain, "the filter chain is no longer a single assignment; re-check this by hand"
    assert "{body}" not in chain.group(1), "the caption prose must never be interpolated"
    assert "{text}" not in chain.group(1), "the caption prose must never be interpolated"
    assert "textfile={text_path}" in chain.group(1)


def test_a_caption_style_is_a_fixed_vocabulary(clip, tmp_path):
    assert set(v.CAPTION_STYLES) == {"lower_third", "bottom", "top"}
    with pytest.raises(v.VideoEditError):
        v.caption(clip, text="hello", start="1", end="3",
                  work_dir=str(tmp_path), style="wherever")


def test_caption_says_what_is_missing_when_this_ffmpeg_cannot_burn_text():
    """An ffmpeg without libavfilter-extra fails on REACHING drawtext, not on being called.

    That is after the render has spent its minutes, and the message names the filter rather
    than the missing package — so the operator learns "No such filter" and not "this image
    needs libavfilter-extra". The images install it; a hand-built environment may not.
    """
    if v.drawtext_available():
        pytest.skip("this ffmpeg has drawtext, so there is no failure to describe")
    with pytest.raises(v.VideoEditError, match="libavfilter"):
        v.caption("/nonexistent.mp4", text="hello", start="1", end="3", work_dir="/tmp")


@pytest.mark.skipif(not v.ffmpeg_available() or not v.drawtext_available(),
                    reason="this ffmpeg cannot burn text (needs libavfilter-extra)")
def test_a_caption_burns_in_without_disturbing_the_rest(clip, tmp_path):
    out = v.caption(clip, text="Thanks for watching: it's free!", start="1", end="3",
                    work_dir=str(tmp_path))
    info = v.verify_playable(out)
    source = v.probe(clip)
    assert (info["width"], info["height"]) == (source["width"], source["height"])
    assert info["has_audio"] is True


# ---------------------------------------------------------------------------------------------
# Edit plans. A model writes these from prose, which is why the interesting tests are the
# refusals: the plan is the one place where something a model invented reaches ffmpeg.
# ---------------------------------------------------------------------------------------------

def test_a_plan_a_model_invented_cannot_widen_what_boss_can_do():
    """An op BOSS does not have is refused, not skipped.

    Skipping is the dangerous choice: the instruction asked for something, the render finished,
    and the missing part is only discoverable by watching the whole video. Refusing turns it
    into "BOSS cannot do that", which is a true sentence somebody can act on.
    """
    for op in ("speed_up", "remove_silence", "exec", "system", ""):
        with pytest.raises(v.VideoEditError, match="unknown op"):
            v.validate_plan([{"op": op}])


def test_a_plan_is_refused_whole_rather_than_partway_through(clip, tmp_path):
    """Operation 4 being invalid must not leave 1 through 3 rendered.

    A half-edited file is indistinguishable from a finished one without watching it, and it is
    the file a reviewer would be shown.
    """
    plan = [
        {"op": "cut", "remove": [{"start": "1", "end": "2"}]},
        {"op": "reframe", "aspect": "9:16"},
        {"op": "caption", "text": "fine", "start": "1", "end": "2"},
        {"op": "reframe", "aspect": "vertical"},          # not a known aspect
    ]
    with pytest.raises(v.VideoEditError, match="aspect"):
        v.apply_plan(clip, plan, work_dir=str(tmp_path))
    # Nothing was written: the whole plan was checked before the first encode.
    assert not list(tmp_path.iterdir()), sorted(p.name for p in tmp_path.iterdir())


def test_a_plan_may_not_name_a_file_only_an_artifact(clip, tmp_path):
    """The overlay image is named as an artifact, so a plan cannot choose a host path.

    This is the difference between a model picking WHAT to overlay and a model picking which
    file on the control plane gets read.
    """
    with pytest.raises(v.VideoEditError, match="image_artifact_id"):
        v.validate_plan([{"op": "overlay_image", "start": "1", "end": "2",
                          "image": "/etc/passwd"}])


def test_a_plan_that_captions_is_refused_when_text_cannot_be_burned_in():
    """Better than rendering the other five operations and silently dropping the text."""
    plan = [{"op": "cut", "remove": [{"start": "1", "end": "2"}]},
            {"op": "caption", "text": "hello", "start": "3", "end": "5"}]
    if v.drawtext_available():
        assert len(v.validate_plan(plan)) == 2
    else:
        with pytest.raises(v.VideoEditError, match="drawtext"):
            v.validate_plan(plan)


def test_the_model_s_reason_is_carried_but_never_executed():
    """`why` is what makes a plan reviewable without watching the output.

    It is prose from a model, so the test that matters is that it stays data.
    """
    plan = v.validate_plan([{"op": "reframe", "aspect": "9:16",
                             "why": "for the vertical short; :drawtext=x"}])
    assert plan[0]["why"].startswith("for the vertical short")
    assert "aspect" in plan[0] and plan[0]["aspect"] == "9:16"


def test_a_plan_is_told_when_its_timecodes_will_not_mean_what_it_thinks():
    """Cut first, then caption at 5:00, and 5:00 is no longer where the author looked.

    Each operation applies to the output of the one before it, which is the only sane rule and
    is NOT what someone reading timecodes off the original recording means. BOSS says so rather
    than reordering the plan, because guessing at intent on somebody's video is worse.
    """
    timed = {"op": "overlay_image", "start": "300", "end": "304",
             "image_artifact_id": "art_diagram"}
    note = v.plan_timeline_warning(v.validate_plan([
        {"op": "cut", "remove": [{"start": "60", "end": "120"}]}, timed,
    ]))
    assert note and "SHORTENED" in note

    # The other order needs no warning: nothing has shifted the timeline yet.
    assert v.plan_timeline_warning(v.validate_plan([
        timed, {"op": "cut", "remove": [{"start": "60", "end": "120"}]},
    ])) is None


def test_an_absurdly_long_plan_is_refused():
    """Each operation re-encodes the whole video, so 200 of them is a mistake, not a request."""
    with pytest.raises(v.VideoEditError, match="25"):
        v.validate_plan([{"op": "reframe", "aspect": "9:16"}] * 40)


def test_a_plan_reports_what_each_operation_changed(clip, tmp_path):
    """"The plan ran" is not evidence.

    A reviewer approving a publish needs to see the duration fall where a cut was asked for and
    the frame change where a reframe was — which is checkable, unlike a status.
    """
    out = v.apply_plan(clip, [
        {"op": "cut", "remove": [{"start": "3", "end": "5"}], "why": "the fumble"},
        {"op": "reframe", "aspect": "9:16", "why": "vertical short"},
    ], work_dir=str(tmp_path))

    info = v.verify_playable(out["path"])
    assert abs(info["duration_seconds"] - 10.0) < 0.35     # 12 - 2
    assert abs(info["width"] / info["height"] - 9 / 16) < 0.02

    applied = out["applied"]
    assert [a["op"] for a in applied] == ["cut", "reframe"]
    # The cut is visible as a duration that fell, and the reframe as a frame that changed.
    assert applied[0]["duration_after"] < applied[0]["duration_before"]
    assert applied[1]["size_after"] != applied[1]["size_before"]
    assert applied[0]["why"] == "the fumble"


def test_a_plan_naming_an_artifact_nobody_fetched_says_so(clip, tmp_path):
    """Rather than rendering everything up to that point and then failing."""
    with pytest.raises(v.VideoEditError, match="no file was fetched"):
        v.apply_plan(clip, [{"op": "overlay_image", "start": "1", "end": "3",
                             "image_artifact_id": "art_missing"}],
                     work_dir=str(tmp_path))


def test_the_timecode_people_actually_write_is_accepted():
    """MM:SS. "cut 2:20 to 2:25" is how an instruction arrives, and ffmpeg reads 2:10 as two
    minutes ten.

    Rejecting it was not a safety property, it was a gap: measured on a real install, the
    planning model produced a correct plan, had it refused for this alone, and spent a second
    model call rewriting the same timecodes as seconds. Every first attempt.
    """
    assert v._seconds(v._checked_timecode("2:10", "start")) == 130.0
    assert v._seconds(v._checked_timecode("2:10.5", "start")) == 130.5
    assert v._seconds(v._checked_timecode("0:05", "start")) == 5.0
    # Still unambiguous against the other two forms.
    assert v._seconds(v._checked_timecode("90", "start")) == 90.0
    assert v._seconds(v._checked_timecode("01:02:03.250", "start")) == 3723.25


def test_widening_the_timecode_did_not_widen_it_to_anything_else():
    """The validator is the only thing standing between an operator string and a filter
    expression, so a relaxation has to be checked in the other direction too."""
    for bad in ("2:10:", "90s", "1m30s", "2:1", ":30", "2:", "a:bc", "-1:00", "2:100"):
        with pytest.raises(v.VideoEditError):
            v._checked_timecode(bad, "start")


def test_an_ending_slide_can_be_part_of_a_plan(clip, tmp_path):
    """"Add the ending slide" is one of the three things a real edit asks for, and it was the
    one the plan could not express.

    `video.bookend` existed as a standalone skill the whole time — so the capability was there
    and unreachable from the step that does the editing. An operator whose instruction was
    "trim, make it portrait, add the end card" got two of three, silently.
    """
    import subprocess

    slide = str(tmp_path / "slide.mp4")
    # NO AUDIO, which is the normal case for an end card and the one that used to produce a
    # file that plays for three seconds and freezes.
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=navy:size=640x360:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", slide],
                   check=True, capture_output=True)
    assert v.probe(slide)["has_audio"] is False

    out = v.apply_plan(clip, [
        {"op": "trim", "start": "2", "end": "8", "why": "the section"},
        {"op": "reframe", "aspect": "9:16", "why": "portrait"},
        {"op": "bookend", "outro_artifact_id": "art_slide", "why": "the end card"},
    ], work_dir=str(tmp_path), artifact_paths={"art_slide": slide})

    info = v.verify_playable(out["path"])
    assert abs(info["duration_seconds"] - 9.0) < 0.6      # 6s kept + 3s slide
    assert abs(info["width"] / info["height"] - 9 / 16) < 0.02
    # The silent slide gets silence at its own length rather than breaking the join.
    assert info["has_audio"] is True


def test_a_bookend_with_no_clip_is_refused():
    """Naming the operation and no file is the mistake, and it is caught before rendering."""
    with pytest.raises(v.VideoEditError, match="intro_artifact_id"):
        v.validate_plan([{"op": "bookend"}])


def test_a_bookend_naming_a_clip_nobody_fetched_says_so(clip, tmp_path):
    with pytest.raises(v.VideoEditError, match="no file was fetched"):
        v.apply_plan(clip, [{"op": "bookend", "outro_artifact_id": "art_missing"}],
                     work_dir=str(tmp_path))
