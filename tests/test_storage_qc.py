from __future__ import annotations

from types import SimpleNamespace

import pytest

from video_workstation.qc import parse_ffprobe
from video_workstation.storage import InsufficientStorage, StorageGuard, generated_asset_path


def test_storage_requires_fixed_floor_and_twice_estimated_temporary_space(tmp_path):
    low_fixed = StorageGuard(tmp_path, minimum_free_bytes=100, usage=lambda _path: SimpleNamespace(free=99))
    with pytest.raises(InsufficientStorage, match="最低保留空间"):
        low_fixed.ensure_capacity(estimated_temp_bytes=10)

    low_estimate = StorageGuard(tmp_path, minimum_free_bytes=100, usage=lambda _path: SimpleNamespace(free=150))
    with pytest.raises(InsufficientStorage, match="预计临时空间的 2 倍"):
        low_estimate.ensure_capacity(estimated_temp_bytes=80)

    enough = StorageGuard(tmp_path, minimum_free_bytes=100, usage=lambda _path: SimpleNamespace(free=200))
    assert enough.ensure_capacity(estimated_temp_bytes=80) == 200


def test_generated_asset_path_stays_inside_root(tmp_path):
    path = generated_asset_path(tmp_path, "a" * 32, "b" * 32, ".mp4")
    assert path.parent.parent == tmp_path.resolve()
    assert path.suffix == ".mp4"
    with pytest.raises(ValueError):
        generated_asset_path(tmp_path, "../outside", "b" * 32, ".mp4")
    with pytest.raises(ValueError):
        generated_asset_path(tmp_path, "a" * 32, "b" * 32, ".exe")


def test_ffprobe_parser_accepts_valid_h264_aac_and_flags_missing_audio_or_duration():
    valid = {
        "format": {"duration": "5.02"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000"},
        ],
    }
    result = parse_ffprobe(valid, expected_duration=5)
    assert result.passed is True
    assert result.width == 1920
    assert result.has_audio is True

    invalid = {
        "format": {"duration": "8.0"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920}],
    }
    result = parse_ffprobe(invalid, expected_duration=5)
    assert result.passed is False
    assert "missing_audio" in result.errors
    assert "duration_deviation" in result.errors


def test_ffprobe_parser_marks_unreadable_payload_as_corrupt():
    result = parse_ffprobe({}, expected_duration=5)
    assert result.passed is False
    assert result.errors == ["corrupt_file"]
