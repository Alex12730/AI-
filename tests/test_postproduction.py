from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from video_workstation.postproduction import PostproductionRequest, build_compose_argv, build_encode_argv, execute_compose
from video_workstation.qc import probe_media


def test_compose_argv_is_safe_letterboxed_and_adds_silence(tmp_path):
    first = tmp_path / "中文 clip [1].mp4"
    second = tmp_path / "clip,2.mp4"
    subtitle = tmp_path / "字幕 [最终],v1.srt"
    output = tmp_path / "成片.mp4"
    request = PostproductionRequest(
        "task",
        [first, second],
        output,
        "9:16",
        subtitle,
        input_durations=[1.0, 2.0],
        input_has_audio=[True, False],
    )
    argv = build_compose_argv(request)
    graph = argv[argv.index("-filter_complex") + 1]
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in graph
    assert "pad=1080:1920" in graph
    assert "concat=n=2:v=1:a=1" in graph
    assert "subtitles=filename=" in graph
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in argv
    assert str(first) in argv and str(second) in argv
    assert argv[-1] == str(output)
    assert [argv[argv.index("-c:v") + 1], argv[argv.index("-c:a") + 1]] == ["libx264", "aac"]
    assert "24" in argv


def test_compose_and_encode_reject_empty_or_invalid_aspect(tmp_path):
    with pytest.raises(ValueError, match="至少"):
        build_compose_argv(PostproductionRequest("t", [], tmp_path / "out.mp4", "16:9"))
    with pytest.raises(ValueError, match="画幅"):
        build_encode_argv(tmp_path / "in.mp4", tmp_path / "out.mp4", "1:1")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg runtime unavailable")
def test_ffmpeg_compose_integration_preserves_delivery_contract(tmp_path):
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    output = tmp_path / "delivery.mp4"
    commands = [
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=0.5", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(first)],
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=180x320:r=24:d=0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(second)],
    ]
    for command in commands:
        subprocess.run(command, shell=False, check=True, capture_output=True, timeout=60)
    result = execute_compose(PostproductionRequest("t", [first, second], output, "16:9"), timeout_seconds=120)
    assert result.success is True
    qc = probe_media(output, expected_duration=1.0)
    assert qc.passed is True
    assert (qc.width, qc.height) == (1920, 1080)
    assert qc.video_codec == "h264"
    assert qc.audio_codec == "aac"
