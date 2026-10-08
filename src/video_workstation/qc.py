from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class QCResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None


def parse_ffprobe(payload: dict[str, Any], *, expected_duration: float, require_audio: bool = True) -> QCResult:
    streams = payload.get("streams")
    format_data = payload.get("format")
    if not isinstance(streams, list) or not isinstance(format_data, dict):
        return QCResult(passed=False, errors=["corrupt_file"])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    try:
        duration = float(format_data["duration"])
    except (KeyError, TypeError, ValueError):
        return QCResult(passed=False, errors=["corrupt_file"])
    errors: list[str] = []
    if not videos:
        errors.append("missing_video")
    if require_audio and not audios:
        errors.append("missing_audio")
    tolerance = max(0.5, expected_duration * 0.05)
    if abs(duration - expected_duration) > tolerance:
        errors.append("duration_deviation")
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    return QCResult(
        passed=not errors,
        errors=errors,
        duration_seconds=duration,
        width=video.get("width"),
        height=video.get("height"),
        has_audio=bool(audios),
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name"),
    )


def probe_media(path: Path, *, expected_duration: float, ffprobe_bin: str = "ffprobe") -> QCResult:
    completed = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        return QCResult(passed=False, errors=["corrupt_file"])
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return QCResult(passed=False, errors=["corrupt_file"])
    return parse_ffprobe(payload, expected_duration=expected_duration)
