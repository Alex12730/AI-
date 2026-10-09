from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .adapters import AdapterResult
from .models import ModelProfile
from .offline import validate_offline_profile


DELIVERY_SIZES = {"16:9": (1920, 1080), "9:16": (1080, 1920)}


@dataclass(frozen=True, slots=True)
class PostproductionRequest:
    task_id: str
    input_paths: list[Path]
    output_path: Path
    aspect_ratio: str
    subtitle_path: Path | None = None
    input_durations: list[float] | None = None
    input_has_audio: list[bool] | None = None


def _subtitle_filter(path: Path) -> str:
    value = path.resolve().as_posix().replace("\\", "/")
    value = value.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,").replace("[", r"\[").replace("]", r"\]")
    return f"subtitles=filename='{value}'"


def build_compose_argv(request: PostproductionRequest, *, ffmpeg_bin: str = "ffmpeg") -> list[str]:
    if not request.input_paths:
        raise ValueError("合成至少需要一个视频素材")
    if request.aspect_ratio not in DELIVERY_SIZES:
        raise ValueError("只支持 16:9 或 9:16 交付画幅")
    count = len(request.input_paths)
    has_audio = request.input_has_audio or [True] * count
    durations = request.input_durations or [1.0] * count
    if len(has_audio) != count or len(durations) != count or any(value <= 0 for value in durations):
        raise ValueError("输入媒体探测结果不完整")

    argv = [ffmpeg_bin, "-hide_banner", "-nostdin", "-y"]
    for path in request.input_paths:
        argv.extend(["-i", str(path)])
    silent_indexes: dict[int, int] = {}
    next_input = count
    for index, present in enumerate(has_audio):
        if present:
            continue
        silent_indexes[index] = next_input
        next_input += 1
        argv.extend([
            "-f", "lavfi", "-t", f"{durations[index]:.6f}",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ])

    width, height = DELIVERY_SIZES[request.aspect_ratio]
    filters: list[str] = []
    concat_inputs: list[str] = []
    for index in range(count):
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            "setsar=1,fps=24,format=yuv420p,setpts=PTS-STARTPTS"
            f"[v{index}]"
        )
        audio_index = index if has_audio[index] else silent_indexes[index]
        filters.append(
            f"[{audio_index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad=whole_dur={durations[index]:.6f},atrim=duration={durations[index]:.6f},asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(concat_inputs) + f"concat=n={count}:v=1:a=1[vcat][acat]")
    if request.subtitle_path:
        filters.append(f"[vcat]{_subtitle_filter(request.subtitle_path)}[vout]")
    else:
        filters.append("[vcat]null[vout]")

    argv.extend([
        "-filter_complex", ";".join(filters),
        "-map", "[vout]", "-map", "[acat]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-r", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(request.output_path),
    ])
    return argv


def build_encode_argv(input_path: Path, output_path: Path, aspect_ratio: str, *, ffmpeg_bin: str = "ffmpeg") -> list[str]:
    if aspect_ratio not in DELIVERY_SIZES:
        raise ValueError("只支持 16:9 或 9:16 交付画幅")
    width, height = DELIVERY_SIZES[aspect_ratio]
    return [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-i", str(input_path),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=24,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
        str(output_path),
    ]


def _probe_inputs(paths: list[Path], *, run: Callable = subprocess.run, ffprobe_bin: str = "ffprobe") -> tuple[list[float], list[bool]]:
    durations: list[float] = []
    has_audio: list[bool] = []
    for path in paths:
        completed = run(
            [ffprobe_bin, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            shell=False, check=False, capture_output=True, text=True, timeout=60,
        )
        if completed.returncode != 0:
            raise ValueError("输入素材无法读取")
        try:
            payload = json.loads(completed.stdout)
            duration = float(payload["format"]["duration"])
            audio = any(stream.get("codec_type") == "audio" for stream in payload["streams"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("输入素材探测结果无效") from exc
        if duration <= 0:
            raise ValueError("输入素材时长无效")
        durations.append(duration)
        has_audio.append(audio)
    return durations, has_audio


def execute_compose(
    request: PostproductionRequest,
    *,
    run: Callable = subprocess.run,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    timeout_seconds: int = 4 * 60 * 60,
) -> AdapterResult:
    try:
        for path in request.input_paths:
            if not Path(path).is_file():
                raise ValueError("输入素材不存在")
        if request.subtitle_path and not request.subtitle_path.is_file():
            raise ValueError("字幕素材不存在")
        durations, has_audio = _probe_inputs(request.input_paths, run=run, ffprobe_bin=ffprobe_bin)
        resolved = replace(request, input_durations=durations, input_has_audio=has_audio)
        argv = build_compose_argv(resolved, ffmpeg_bin=ffmpeg_bin)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        completed = run(argv, shell=False, check=False, capture_output=True, text=True, timeout=timeout_seconds)
        if completed.returncode != 0 or not request.output_path.is_file():
            request.output_path.unlink(missing_ok=True)
            return AdapterResult(False, request.output_path, argv=argv, error_class="infrastructure", error_message="本地 FFmpeg 合成失败")
        return AdapterResult(
            True,
            request.output_path,
            metadata={"input_count": len(request.input_paths), "expected_duration": sum(durations)},
            argv=argv,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        request.output_path.unlink(missing_ok=True)
        return AdapterResult(False, request.output_path, error_class="infrastructure", error_message="本地 FFmpeg 合成失败")


PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def execute_video2x(
    profile: ModelProfile,
    input_path: Path,
    output_path: Path,
    *,
    scale: int = 2,
    run: Callable = subprocess.run,
    timeout_seconds: int = 4 * 60 * 60,
) -> AdapterResult:
    try:
        validate_offline_profile(profile)
        command = profile.runtime_config_json.get("command")
        if not isinstance(command, list) or not command:
            raise ValueError("Video2X 尚未配置")
        placeholders = {name for part in command for name in PLACEHOLDER.findall(part)}
        if not {"input", "output"}.issubset(placeholders) or not placeholders.issubset({"input", "output", "scale"}):
            raise ValueError("Video2X 命令占位符无效")
        values = {"input": str(input_path), "output": str(output_path), "scale": str(scale)}
        argv = [PLACEHOLDER.sub(lambda match: values[match.group(1)], part) for part in command]
        if not input_path.is_file():
            raise ValueError("超分输入不存在")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        completed = run(argv, shell=False, check=False, capture_output=True, text=True, timeout=timeout_seconds)
        if completed.returncode != 0 or not output_path.is_file():
            output_path.unlink(missing_ok=True)
            return AdapterResult(False, output_path, argv=argv, error_class="infrastructure", error_message="本地 Video2X 执行失败")
        return AdapterResult(True, output_path, metadata={"scale": scale}, argv=argv)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        output_path.unlink(missing_ok=True)
        return AdapterResult(False, output_path, error_class="configuration", error_message="本地 Video2X 配置无效")
