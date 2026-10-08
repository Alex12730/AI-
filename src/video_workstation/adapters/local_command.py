from __future__ import annotations

import string
import subprocess
from pathlib import Path

from .base import AdapterResult, GenerationRequest


ALLOWED_PLACEHOLDERS = {
    "task_id",
    "prompt",
    "negative_prompt",
    "output",
    "duration",
    "aspect_ratio",
    "seed",
    "first_frame",
    "last_frame",
}


class LocalCommandAdapter:
    def __init__(self, command: list[str], name: str, timeout_seconds: int = 7200):
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("本地模型命令必须是非空字符串数组")
        self.command = list(command)
        self.name = name
        self.timeout_seconds = timeout_seconds

    def render_argv(self, request: GenerationRequest) -> list[str]:
        values = {
            "task_id": request.task_id,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "output": str(request.output_path),
            "duration": str(request.duration_seconds),
            "aspect_ratio": request.aspect_ratio,
            "seed": str(request.seed),
            "first_frame": str(request.first_frame or ""),
            "last_frame": str(request.last_frame or ""),
        }
        rendered: list[str] = []
        formatter = string.Formatter()
        for part in self.command:
            for _literal, field_name, format_spec, conversion in formatter.parse(part):
                if field_name is None:
                    continue
                if field_name not in ALLOWED_PLACEHOLDERS or format_spec or conversion:
                    raise ValueError(f"不支持的命令占位符: {field_name}")
            rendered.append(part.format_map(values))
        return rendered

    def execute(self, request: GenerationRequest) -> AdapterResult:
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        argv = self.render_argv(request)
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            message = "本地模型执行超时" if isinstance(exc, subprocess.TimeoutExpired) else "无法启动本地模型进程"
            return AdapterResult(
                success=False,
                output_path=request.output_path,
                argv=argv,
                error_class="infrastructure",
                error_message=message,
            )
        if completed.returncode != 0:
            raw = completed.stderr or completed.stdout or ""
            error_class = "oom" if "out of memory" in raw.lower() else "infrastructure"
            return AdapterResult(
                success=False,
                output_path=request.output_path,
                argv=argv,
                error_class=error_class,
                error_message=f"本地模型进程退出码 {completed.returncode}",
            )
        if not request.output_path.exists():
            return AdapterResult(
                success=False,
                output_path=request.output_path,
                argv=argv,
                error_class="infrastructure",
                error_message="本地模型命令成功退出，但没有产生输出文件",
            )
        return AdapterResult(success=True, output_path=request.output_path, argv=argv)


class H3Adapter(LocalCommandAdapter):
    def __init__(self, command: list[str], timeout_seconds: int = 7200):
        super().__init__(command, "minimax-h3-fl2va", timeout_seconds)


class WanAdapter(LocalCommandAdapter):
    def __init__(self, command: list[str], timeout_seconds: int = 7200):
        super().__init__(command, "wan-2.2", timeout_seconds)


class LTXAdapter(LocalCommandAdapter):
    def __init__(self, command: list[str], timeout_seconds: int = 7200):
        super().__init__(command, "ltx-2.3", timeout_seconds)
