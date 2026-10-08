from __future__ import annotations

import pytest

from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import ModelProfile
from video_workstation.services.models import (
    ModelNotAdmitted,
    admit_generation,
    record_benchmark,
    seed_model_profiles,
)


@pytest.fixture()
def session(tmp_path):
    db = Database(Settings(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}"))
    db.create_schema()
    with db.session() as current:
        yield current


def passing_runs(count=10):
    return [
        {
            "success": True,
            "oom": False,
            "corrupt": False,
            "elapsed_seconds": 120 + index,
            "vram_peak_gb": 42.1,
            "system_ram_peak_gb": 58.0,
            "temperature_c": 72,
            "av_sync_score": 0.92,
        }
        for index in range(count)
    ]


def test_h3_profile_is_local_fl2va_only_and_unverified_by_default(session):
    seed_model_profiles(session)
    h3 = session.query(ModelProfile).filter_by(slug="minimax-h3-fl2va").one()

    assert h3.provider == "local"
    assert h3.adapter_type == "minimax_h3"
    assert h3.capabilities_json["modes"] == ["text_to_av", "first_frame_to_av", "first_last_frame_to_av"]
    assert h3.capabilities_json["short_edge"] == 768
    assert h3.validated_presets_json == []
    assert "社区许可证" in h3.license_name
    with pytest.raises(ModelNotAdmitted, match="未通过"):
        admit_generation(h3, duration_seconds=5, aspect_ratio="16:9")


def test_h3_preset_requires_ten_runs_ninety_percent_and_no_oom(session):
    seed_model_profiles(session)
    h3 = session.query(ModelProfile).filter_by(slug="minimax-h3-fl2va").one()
    h3.enabled = True

    nine_success = passing_runs(9) + [{"success": False, "oom": False, "corrupt": False}]
    assert record_benchmark(h3, 5, "16:9", nine_success) is True
    assert admit_generation(h3, duration_seconds=5, aspect_ratio="16:9")["duration_seconds"] == 5

    with_oom = passing_runs(9) + [{"success": False, "oom": True, "corrupt": False}]
    assert record_benchmark(h3, 10, "9:16", with_oom) is False
    with pytest.raises(ModelNotAdmitted):
        admit_generation(h3, duration_seconds=10, aspect_ratio="9:16")


def test_unlisted_duration_is_never_admitted(session):
    seed_model_profiles(session)
    h3 = session.query(ModelProfile).filter_by(slug="minimax-h3-fl2va").one()
    h3.enabled = True
    record_benchmark(h3, 5, "16:9", passing_runs())

    with pytest.raises(ModelNotAdmitted):
        admit_generation(h3, duration_seconds=15, aspect_ratio="16:9")

