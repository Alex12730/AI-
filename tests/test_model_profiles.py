from __future__ import annotations

import pytest

from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import ModelProfile, Shot
from video_workstation.services import models as model_service
from video_workstation.services.models import (
    ModelNotAdmitted,
    admit_generation,
    configure_local_profile,
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


def test_local_profile_configuration_is_an_auditable_argv_array(session):
    seed_model_profiles(session)
    h3 = session.query(ModelProfile).filter_by(slug="minimax-h3-fl2va").one()
    configured = configure_local_profile(
        h3,
        command=["python", "/opt/ComfyUI/run_h3.py", "--prompt", "{prompt}", "--output", "{output}"],
        version="H3-Base-2026-09",
        quantization="pruned-int8-ampere",
    )
    assert configured.runtime_config_json["command"][0] == "python"
    assert configured.model_version == "H3-Base-2026-09"
    assert configured.quantization == "pruned-int8-ampere"


def test_reconfiguration_invalidates_old_presets_and_string_booleans_are_rejected(session):
    seed_model_profiles(session)
    h3 = session.query(ModelProfile).filter_by(slug="minimax-h3-fl2va").one()
    record_benchmark(h3, 5, "16:9", passing_runs())
    assert h3.validated_presets_json
    configure_local_profile(h3, command=["python", "run_h3.py", "{prompt}", "{output}"], version="new", quantization="int8")
    assert h3.validated_presets_json == []
    with pytest.raises(ValueError, match="布尔"):
        record_benchmark(h3, 5, "16:9", [{"success": "false", "oom": False, "corrupt": False}] * 10)


def test_demo_profile_syncs_all_eight_workflow_presets_without_reenabling(session):
    seed_model_profiles(session)
    demo = session.query(ModelProfile).filter_by(slug="demo").one()
    demo.enabled = False

    seed_model_profiles(session)

    assert demo.enabled is False
    assert {
        (float(preset["duration_seconds"]), preset["aspect_ratio"])
        for preset in demo.validated_presets_json
    } == {
        (duration, aspect_ratio)
        for duration in (5, 10, 15, 20)
        for aspect_ratio in ("16:9", "9:16")
    }
    assert all(
        preset["config_fingerprint"] == model_service.model_config_fingerprint(demo)
        for preset in demo.validated_presets_json
    )


def test_matching_profiles_require_scene_and_exact_admitted_preset(session):
    seed_model_profiles(session)
    demo = session.query(ModelProfile).filter_by(slug="demo").one()
    h3 = session.query(ModelProfile).filter_by(slug="minimax-h3-fl2va").one()
    record_benchmark(h3, 10, "9:16", passing_runs())
    shot = Shot(
        sequence_no=1,
        title="人物镜头",
        prompt="人物表演",
        scene_type="character",
        duration_seconds=10,
        aspect_ratio="9:16",
    )

    matches = model_service.matching_profiles_for_shot(shot, [h3, demo])
    assert [profile.slug for profile in matches] == ["demo", "minimax-h3-fl2va"]

    shot.scene_type = "product_ui"
    assert [profile.slug for profile in model_service.matching_profiles_for_shot(shot, [h3, demo])] == ["demo"]

    demo.enabled = False
    assert model_service.matching_profiles_for_shot(shot, [h3, demo]) == []


def test_twenty_second_benchmark_is_ltx_only(session):
    seed_model_profiles(session)
    h3 = session.query(ModelProfile).filter_by(slug="minimax-h3-fl2va").one()
    ltx = session.query(ModelProfile).filter_by(slug="ltx-2.3").one()

    with pytest.raises(ValueError, match="最长只开放 15 秒"):
        record_benchmark(h3, 20, "16:9", passing_runs())

    ltx.enabled = True
    assert record_benchmark(ltx, 20, "9:16", passing_runs()) is True
    assert admit_generation(ltx, duration_seconds=20, aspect_ratio="9:16")["duration_seconds"] == 20
