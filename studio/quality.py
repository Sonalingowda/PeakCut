"""Ad breaks and static screens.

Twitch and YouTube stitch commercials into a stream, and a "be right back" slate
can sit where the good moment is. A clip taken from there is worthless, so those
seconds are found and left out.

Metric: mean absolute luma difference between consecutive frames
(tblend=difference + signalstats). Measured on real captures:

    ad slate         ~0.19
    gameplay clip    ~1.6 - 2.5

A flat 0-255 luma difference separates them with a wide margin, so no OCR, no
reference image and no model is needed. The frames are read once for the whole
video, and clips are then checked against that map.
"""

from __future__ import annotations

import re
import subprocess
import json
import math
from pathlib import Path

from . import config

_FFMPEG = "ffmpeg"

_PTS = re.compile(r"pts_time:([0-9.]+)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def validate_clip(path: str | Path, metadata: dict | None = None, captions: bool = True) -> dict:
    """Validate a rendered clip before it can enter the posting queue.

    This deliberately uses ffprobe rather than trusting the encoder return code: an
    encoder can succeed while producing a stream with no audio, a bad duration, or
    the wrong geometry.  The returned object is stable enough to show in the UI and
    to persist as a rejection reason.
    """
    p = Path(path)
    errors: list[str] = []
    result = {"ok": False, "errors": errors, "path": str(p)}
    if not p.is_file() or p.stat().st_size < 1024:
        errors.append("The rendered video is missing or empty.")
        return result
    try:
        raw = subprocess.run(["ffprobe", "-v", "error", "-of", "json", "-show_streams", "-show_format", str(p)],
                             capture_output=True, text=True, timeout=30, check=True).stdout
        info = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        errors.append(f"The rendered video cannot be opened: {e}")
        return result
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((info.get("format") or {}).get("duration") or 0)
    width, height = int((video or {}).get("width") or 0), int((video or {}).get("height") or 0)
    result.update(duration=duration, width=width, height=height, has_audio=bool(audio))
    if not video:
        errors.append("The rendered file has no video stream.")
    if width < 320 or height < 480 or abs((width / max(height, 1)) - 9 / 16) > 0.03:
        errors.append("The rendered video is not a valid vertical 9:16 video.")
    if not math.isfinite(duration) or duration < 2 or duration > 181:
        errors.append("The rendered video has an invalid duration.")
    if not audio:
        errors.append("The rendered video has no audio stream.")
    caption_file = (metadata or {}).get("caption_file") or p.with_suffix(".ass")
    if captions and not Path(caption_file).is_file():
        errors.append("Captions were requested but the caption file is missing.")
    result["ok"] = not errors
    return result


def duplicate_candidates(clips: list[dict], threshold: float = 0.72) -> list[tuple[str, str]]:
    """Return pairs whose source spans substantially overlap."""
    pairs = []
    for i, a in enumerate(clips):
        for b in clips[i + 1:]:
            overlap = max(0.0, min(float(a.get("end", 0)), float(b.get("end", 0))) -
                         max(float(a.get("start", 0)), float(b.get("start", 0))))
            shorter = min(float(a.get("end", 0)) - float(a.get("start", 0)),
                          float(b.get("end", 0)) - float(b.get("start", 0)))
            if shorter > 0 and overlap / shorter >= threshold:
                pairs.append((str(a.get("id", i)), str(b.get("id", i + 1))))
    return pairs


def motion_map(path: str | Path, bucket: float = 2.0) -> list[tuple[float, float]]:
    """(time, motion) for every bucket of the video, or nothing if it cannot be read.

    Motion is the mean luma difference between consecutive frames: near zero for a
    frozen screen, high for anything actually moving."""
    try:
        proc = subprocess.run(
            [_FFMPEG, "-hide_banner", "-loglevel", "error",
             "-i", str(path),
             "-vf", "tblend=all_mode=difference,signalstats,metadata=print:file=-",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=900,
        )
    except Exception:  # noqa: BLE001 - a missing ffmpeg must not stop a job
        return []
    times: list[float] = []
    vals: list[float] = []
    at = 0.0
    for line in (proc.stdout or "").splitlines():
        m = _PTS.search(line)
        if m:
            at = float(m.group(1))
            continue
        m = _YAVG.search(line)
        if m:
            times.append(at)
            vals.append(float(m.group(1)))
    if len(vals) > 1:
        times, vals = times[1:], vals[1:]      # the first blended frame is against black
    if not vals:
        return []
    buckets: dict[int, list[float]] = {}
    for t, v in zip(times, vals):
        buckets.setdefault(int(t / bucket), []).append(v)
    return [(i * bucket, sum(v) / len(v)) for i, v in sorted(buckets.items())]


def static_ranges(path: str | Path, bucket: float = 2.0, threshold: float | None = None,
                  min_seconds: float | None = None) -> list[tuple[float, float]]:
    """The stretches that look like an ad break, a slate or a frozen screen."""
    threshold = config.STATIC_THRESHOLD if threshold is None else threshold
    min_seconds = config.STATIC_MIN_SECONDS if min_seconds is None else min_seconds
    pairs = motion_map(path, bucket)
    if not pairs:
        return []
    runs: list[tuple[float, float]] = []
    start = None
    for t, v in pairs:
        if v < threshold:
            if start is None:
                start = t
        elif start is not None:
            runs.append((start, t))
            start = None
    if start is not None:
        runs.append((start, pairs[-1][0] + bucket))
    return [(a, b) for a, b in runs if b - a >= min_seconds]


def static_overlap(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    """How much of a clip (in seconds) sits inside a static stretch."""
    total = 0.0
    for a, b in ranges:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += hi - lo
    return total
