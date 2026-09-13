"""Video editing as a BOSS capability, not as a command somebody typed.

WHY THESE ARE `builtin` AND NOT `sandboxed_command`
----------------------------------------------------
`sandboxed_command` exists for operator-authored command lines, and it refuses to run without
real isolation for a good reason: an arbitrary ffmpeg line executing on the control plane is
remote code execution against the host that holds every project's credentials.

Nothing here is operator-authored. BOSS owns every argument vector below; the operator supplies
a corner, a scale, two timecodes — values that are validated against a fixed set or a numeric
range before they are used, and which are passed as ARGV ELEMENTS, never interpolated into a
shell string. There is no shell at all: `subprocess.run` with a list, `shell=False`.

That makes these the same kind of thing as `github.create_pr_real` — BOSS's own code doing a
bounded thing — which is what the operator asked for: inbuilt capabilities, not a command
console wearing a workflow step's clothes.

WHAT EACH ONE PRESERVES
-----------------------
Every operation writes a NEW file and leaves its input untouched, because a pipeline that edits
in place cannot be retried and cannot be inspected after a bad result. Audio is preserved and
never re-encoded where the operation does not touch it: re-encoding audio to overlay a logo
costs quality for nothing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Optional

#: Where a watermark may go. A fixed set, not free text: these become filter expressions, and
#: "top-right" is the whole vocabulary anybody needs.
CORNERS = {
    # x, y as ffmpeg overlay expressions. `m` is the margin, `W/H` the video, `w/h` the logo.
    "top_right": ("W-w-{m}", "{m}"),
    "top_left": ("{m}", "{m}"),
    "bottom_right": ("W-w-{m}", "H-h-{m}"),
    "bottom_left": ("{m}", "H-h-{m}"),
}

#: A timecode BOSS will accept: seconds, or HH:MM:SS(.ms). Anything else is refused rather than
#: passed to ffmpeg, which interprets several other forms in ways an operator would not expect.
_TIMECODE = re.compile(r"^(?:\d+(?:\.\d+)?|\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)$")

#: Long enough for a real render, short enough that a wedged ffmpeg does not hold a worker slot
#: for the rest of the day.
DEFAULT_TIMEOUT_SECONDS = 1800


class VideoEditError(RuntimeError):
    """Something the operator can fix, phrased for them."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _require_ffmpeg() -> None:
    if not ffmpeg_available():
        raise VideoEditError(
            "ffmpeg is not installed in this worker image. Video editing steps cannot run "
            "until it is — see infra/containers/worker.Dockerfile.")


def _run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run ffmpeg with an ARGUMENT LIST and no shell.

    The whole safety argument of this module rests on this function: there is no string being
    parsed, so an operator value containing a semicolon is a filename containing a semicolon
    and not a second command.
    """
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False, fixed executable
            argv, capture_output=True, text=True, timeout=timeout, shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise VideoEditError(
            f"ffmpeg did not finish within {timeout}s. The input may be far larger than "
            "expected, or the filter may be pathological."
        ) from error
    if completed.returncode != 0:
        # ffmpeg's last few lines carry the actual reason; the preceding hundred are banner.
        tail = "\n".join((completed.stderr or "").strip().splitlines()[-6:])
        raise VideoEditError(f"ffmpeg failed: {tail or 'no output'}")
    return completed.stdout


def probe(path: str) -> dict[str, Any]:
    """What a file actually is. Used to verify a render rather than assume it."""
    _require_ffmpeg()
    out = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration,size",
        "-of", "json", path,
    ], timeout=120)
    data = json.loads(out or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    fmt = data.get("format") or {}
    return {
        "width": video.get("width"),
        "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "duration_seconds": float(fmt["duration"]) if fmt.get("duration") else None,
        "bytes": int(fmt["size"]) if fmt.get("size") else None,
    }


def _checked_timecode(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _TIMECODE.match(text):
        raise VideoEditError(
            f"{field} must be seconds (90, 90.5) or HH:MM:SS — got {text!r}.")
    return text


def _output(work_dir: str, base: str, suffix: str) -> str:
    stem, ext = os.path.splitext(os.path.basename(base))
    return os.path.join(work_dir, f"{stem}.{suffix}{ext or '.mp4'}")


def trim(source: str, *, start: str, end: str, work_dir: str) -> str:
    """Cut a range out. Re-encodes, deliberately.

    A stream copy (`-c copy`) is faster and cuts only at keyframes, so the clip starts up to
    several seconds away from the timecode asked for. For a published video that is a visible
    defect, and "fast but not where you asked" is the wrong trade here.
    """
    _require_ffmpeg()
    begin, finish = _checked_timecode(start, "start"), _checked_timecode(end, "end")
    out = _output(work_dir, source, "trim")
    _run([
        "ffmpeg", "-nostdin", "-y",
        "-ss", begin, "-to", finish, "-i", source,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out,
    ])
    return out


def watermark(
    source: str,
    *,
    image: str,
    work_dir: str,
    corner: str = "top_right",
    scale: float = 0.12,
    margin: int = 24,
    opacity: float = 1.0,
) -> str:
    """Overlay a logo in a corner, scaled relative to the video's own width.

    RELATIVE, not a pixel size: a logo sized for 1080p is a sticker on a 4K frame and unreadable
    on a vertical crop. `scale` is a fraction of video width, so the same setting looks right on
    every aspect ratio the channel might use.
    """
    _require_ffmpeg()
    if corner not in CORNERS:
        raise VideoEditError(
            f"corner must be one of {', '.join(sorted(CORNERS))} — got {corner!r}.")
    if not 0.01 <= float(scale) <= 0.5:
        raise VideoEditError("scale must be between 0.01 and 0.5 of the video width.")
    if not 0 <= int(margin) <= 500:
        raise VideoEditError("margin must be between 0 and 500 pixels.")
    if not 0.0 <= float(opacity) <= 1.0:
        raise VideoEditError("opacity must be between 0 and 1.")
    if not os.path.exists(image):
        raise VideoEditError(f"No watermark image at {image}.")

    # THE TARGET WIDTH IS COMPUTED HERE, NOT EXPRESSED IN THE FILTER.
    #
    # `scale=iw*k*main_w/iw` looks right and is not: `main_w` is a scale2ref variable and a
    # plain `scale` filter rejects it outright ("Expressions with scale2ref variables are not
    # valid"). Reading the source's width and passing a literal is both simpler and
    # deterministic — the logo is the same size every run, rather than depending on how ffmpeg
    # resolves an expression.
    source_info = probe(source)
    video_width = int(source_info.get("width") or 0)
    if not video_width:
        raise VideoEditError("Could not read the video's width, so the logo cannot be sized.")
    # Even, because h264 requires even dimensions and an odd width fails at encode time with a
    # message about the pixel format rather than about the logo.
    logo_width = max(2, int(video_width * float(scale)) // 2 * 2)

    x_expr, y_expr = CORNERS[corner]
    m = str(int(margin))
    # Built from validated numbers and a fixed vocabulary — no operator string reaches this.
    chain = (
        f"[1:v]scale={logo_width}:-2,"
        f"format=rgba,colorchannelmixer=aa={float(opacity):.3f}[wm];"
        f"[0:v][wm]overlay={x_expr.format(m=m)}:{y_expr.format(m=m)}"
    )
    out = _output(work_dir, source, "wm")
    _run([
        "ffmpeg", "-nostdin", "-y", "-i", source, "-i", image,
        "-filter_complex", chain,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        # AUDIO UNTOUCHED. Overlaying a logo has nothing to do with sound, and re-encoding it
        # would cost quality for no reason.
        "-c:a", "copy",
        "-movflags", "+faststart",
        out,
    ])
    return out


def bookend(
    source: str,
    *,
    work_dir: str,
    intro: Optional[str] = None,
    outro: Optional[str] = None,
) -> str:
    """Put a title clip before and a thank-you clip after.

    Normalises every part to one format first. Concatenating clips that differ in resolution,
    frame rate or audio layout is the single most common way this produces a file that plays
    for three seconds and then freezes — and it does so SILENTLY, which is worse.
    """
    _require_ffmpeg()
    parts = [p for p in (intro, source, outro) if p]
    if len(parts) < 2:
        raise VideoEditError("bookend needs at least an intro or an outro.")
    for part in parts:
        if not os.path.exists(part):
            raise VideoEditError(f"No clip at {part}.")

    target = probe(source)
    width, height = target.get("width"), target.get("height")
    if not width or not height:
        raise VideoEditError("Could not read the source video's dimensions.")

    inputs: list[str] = []
    for part in parts:
        inputs.extend(["-i", part])

    # Every part is scaled and padded to the SOURCE's frame, given a common pixel format, frame
    # rate and audio layout, and only then concatenated.
    #
    # A PART WITH NO AUDIO GETS SILENCE, NOT AN ERROR.
    #
    # A title card and a thank-you slide usually have no audio track at all — the real
    # `nandri_thankyou_with_logo.mp4` does not. Referring to `[2:a]` then fails with "Stream
    # specifier ':a' in filtergraph description matches no streams", which reads as a broken
    # filter rather than as a perfectly ordinary silent clip. `concat` requires every segment
    # to have the same streams, so the missing one is synthesised at the right length.
    segments = []
    for index, part in enumerate(parts):
        segments.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{index}];"
        )
        info = probe(part)
        if info.get("has_audio"):
            segments.append(
                f"[{index}:a]aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}];"
            )
        else:
            seconds = float(info.get("duration_seconds") or 0)
            if seconds <= 0:
                raise VideoEditError(
                    f"'{os.path.basename(part)}' has neither audio nor a readable duration, so "
                    "silence cannot be matched to it.")
            segments.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=duration={seconds:.3f},asetpts=N/SR/TB[a{index}];"
            )
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    chain = "".join(segments) + f"{joined}concat=n={len(parts)}:v=1:a=1[v][a]"

    out = _output(work_dir, source, "final")
    _run([
        "ffmpeg", "-nostdin", "-y", *inputs,
        "-filter_complex", chain, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out,
    ])
    return out


def verify_playable(path: str, *, expect_audio: bool = True) -> dict[str, Any]:
    """A render that produced a file is not the same as a render that produced a VIDEO.

    ffmpeg exits 0 on outputs that are empty, silent, or a fraction of the intended length. This
    is the check that turns "the step succeeded" into something worth believing, and it is why
    the publish step can require evidence rather than a status.
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise VideoEditError("The render produced no file.")
    info = probe(path)
    if not info.get("duration_seconds"):
        raise VideoEditError("The rendered file has no duration — it is not a playable video.")
    if not info.get("width") or not info.get("height"):
        raise VideoEditError("The rendered file has no video stream.")
    if expect_audio and not info.get("has_audio"):
        raise VideoEditError(
            "The rendered file has no audio. A silent publish is almost never intended, so "
            "this fails rather than uploads.")
    return info
