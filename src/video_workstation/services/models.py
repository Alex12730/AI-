from __future__ import annotations

from typing import Any
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ModelProfile
from ..offline import validate_offline_profile


class ModelNotAdmitted(ValueError):
    pass


PROFILE_DEFINITIONS = [
    {
        "slug": "demo",
        "display_name": "Demo 本地适配器",
        "adapter_type": "demo",
        "enabled": True,
        "is_heavy": False,
        "capabilities_json": {"demo": True, "notice": "不生成视频"},
        "validated_presets_json": [
            {"duration_seconds": 5, "aspect_ratio": "16:9", "success_rate": 1.0},
            {"duration_seconds": 5, "aspect_ratio": "9:16", "success_rate": 1.0},
        ],
    },
    {
        "slug": "qwen-local",
        "display_name": "Qwen 本地文本模型",
        "adapter_type": "local_command",
        "enabled": False,
        "is_heavy": False,
        "capabilities_json": {"modes": ["script_breakdown", "prompt_draft"]},
    },
    {
        "slug": "wan-2.2",
        "display_name": "Wan 2.2",
        "adapter_type": "wan",
        "enabled": False,
        "capabilities_json": {"modes": ["broll", "environment", "motion"]},
    },
    {
        "slug": "minimax-h3-fl2va",
        "display_name": "MiniMax H3",
        "adapter_type": "minimax_h3",
        "enabled": True,
        "model_version": "H3-Base",
        "quantization": "pruned-int8-pending-install",
        "capabilities_json": {
            "modes": ["text_to_av", "first_frame_to_av", "first_last_frame_to_av"],
            "short_edge": 768,
            "fps": 24,
            "audio_hz": 32000,
            "audio_channels": 2,
            "deployment": "ComfyUI native + CPU offload",
            "ref2va": False,
            "cloud_postprocessing": False,
        },
        "license_name": "MiniMax H3 社区许可证",
        "license_version": "repository-current-at-install",
        "license_url": "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE",
        "notice": "公开发布需保留 AI 生成标识；年收入超过 2000 万美元或跨限制地域使用前需重新取得授权。",
    },
    {
        "slug": "ltx-2.3",
        "display_name": "LTX-2.3",
        "adapter_type": "ltx",
        "enabled": False,
        "capabilities_json": {"modes": ["longer_character_video"], "phase": 2},
    },
    {
        "slug": "cosyvoice-3",
        "display_name": "CosyVoice 3",
        "adapter_type": "local_command",
        "enabled": False,
        "is_heavy": False,
        "capabilities_json": {"modes": ["exact_voiceover"]},
    },
    {
        "slug": "musetalk-1.5",
        "display_name": "MuseTalk 1.5",
        "adapter_type": "local_command",
        "enabled": False,
        "capabilities_json": {"modes": ["lip_sync"]},
    },
    {
        "slug": "video2x",
        "display_name": "Video2X",
        "adapter_type": "local_command",
        "enabled": False,
        "capabilities_json": {"modes": ["upscale", "interpolation"], "default_off": True},
    },
]


def seed_model_profiles(session: Session) -> list[ModelProfile]:
    profiles: list[ModelProfile] = []
    for definition in PROFILE_DEFINITIONS:
        existing = session.scalar(select(ModelProfile).where(ModelProfile.slug == definition["slug"]))
        if existing is not None:
            if existing.slug == "demo" and existing.validated_presets_json:
                fingerprint = model_config_fingerprint(existing)
                existing.validated_presets_json = [
                    {**preset, "config_fingerprint": fingerprint}
                    for preset in existing.validated_presets_json
                ]
            profiles.append(existing)
            continue
        profile = ModelProfile(**definition)
        session.add(profile)
        session.flush()
        if profile.validated_presets_json:
            fingerprint = model_config_fingerprint(profile)
            profile.validated_presets_json = [{**preset, "config_fingerprint": fingerprint} for preset in profile.validated_presets_json]
        profiles.append(profile)
    session.flush()
    return profiles


def configure_local_profile(
    profile: ModelProfile,
    *,
    command: list[str],
    version: str,
    quantization: str,
) -> ModelProfile:
    profile.provider = "local"
    profile.runtime_config_json = {"command": command}
    profile.model_version = version.strip() or "unconfigured"
    profile.quantization = quantization.strip() or "unconfigured"
    validate_offline_profile(profile)
    profile.validated_presets_json = []
    return profile


def model_config_fingerprint(profile: ModelProfile) -> str:
    payload = {"version": profile.model_version, "quantization": profile.quantization, "runtime": profile.runtime_config_json}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_benchmark(
    profile: ModelProfile,
    duration_seconds: float,
    aspect_ratio: str,
    runs: list[dict[str, Any]],
) -> bool:
    history = list(profile.capabilities_json.get("benchmark_history", []))
    for run in runs:
        for key in ("success", "oom", "corrupt"):
            if type(run.get(key)) is not bool:
                raise ValueError(f"基准字段 {key} 必须是布尔值")
        if run["success"]:
            for key in ("elapsed_seconds", "vram_peak_gb", "system_ram_peak_gb", "temperature_c", "av_sync_score"):
                if not isinstance(run.get(key), (int, float)):
                    raise ValueError(f"成功样本缺少数值指标: {key}")
    success_count = sum(run["success"] for run in runs)
    has_blocking_failure = any(run["oom"] or run["corrupt"] for run in runs)
    qualified = len(runs) == 10 and success_count / 10 >= 0.9 and not has_blocking_failure
    record = {
        "duration_seconds": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "run_count": len(runs),
        "success_count": success_count,
        "qualified": qualified,
        "runs": runs,
    }
    history.append(record)
    capabilities = dict(profile.capabilities_json)
    capabilities["benchmark_history"] = history
    profile.capabilities_json = capabilities

    presets = [
        preset
        for preset in profile.validated_presets_json
        if not (
            float(preset.get("duration_seconds", -1)) == float(duration_seconds)
            and preset.get("aspect_ratio") == aspect_ratio
        )
    ]
    if qualified:
        presets.append(
            {
                "duration_seconds": duration_seconds,
                "aspect_ratio": aspect_ratio,
                "success_rate": success_count / len(runs),
                "run_count": len(runs),
                "config_fingerprint": model_config_fingerprint(profile),
            }
        )
    profile.validated_presets_json = presets
    return qualified


def admit_generation(profile: ModelProfile, *, duration_seconds: float, aspect_ratio: str) -> dict[str, Any]:
    if not profile.enabled:
        raise ModelNotAdmitted(f"模型 {profile.display_name} 未启用")
    for preset in profile.validated_presets_json:
        if (
            float(preset.get("duration_seconds", -1)) == float(duration_seconds)
            and preset.get("aspect_ratio") == aspect_ratio
        ):
            if preset.get("config_fingerprint") == model_config_fingerprint(profile):
                return preset
    raise ModelNotAdmitted(
        f"{profile.display_name} 的 {duration_seconds:g} 秒 {aspect_ratio} 档位未通过本机连续基准测试"
    )


def validate_model_for_shot(shot, profile: ModelProfile) -> None:
    if shot.scene_type == "product_ui" and profile.adapter_type != "demo":
        raise ModelNotAdmitted("产品界面镜头必须使用真实录屏或截图动效，不能使用生成模型")


def route_model_slug(scene_type: str, *, exact_dialogue: bool = False) -> str:
    if scene_type == "product_ui":
        return "real_capture"
    if exact_dialogue:
        return "cosyvoice-3+musetalk-1.5"
    if scene_type == "character":
        return "minimax-h3-fl2va"
    return "wan-2.2"
