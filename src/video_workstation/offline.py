from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import ModelProfile


class OfflinePolicyError(ValueError):
    pass


SENSITIVE_KEYS = {"api_key", "apikey", "token", "secret", "password", "cookie", "authorization"}
NETWORK_CLIENTS = {"curl", "curl.exe", "wget", "wget.exe", "http", "https"}
SHELL_EXECUTABLES = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "bash.exe", "sh"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _walk(value: Any, path: str = "runtime"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, key.lower(), child
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def validate_offline_profile(profile: ModelProfile) -> None:
    if profile.provider != "local":
        raise OfflinePolicyError("模型 provider 必须是 local")
    config = profile.runtime_config_json or {}
    for path, key, value in _walk(config):
        if key in SENSITIVE_KEYS:
            raise OfflinePolicyError(f"本地模型配置不得包含敏感字段: {path}")
        if key in {"endpoint", "url", "base_url"} and isinstance(value, str):
            parsed = urlparse(value)
            if parsed.scheme in {"http", "https"} and parsed.hostname not in LOOPBACK_HOSTS:
                raise OfflinePolicyError(f"运行期端点只能使用 loopback: {path}")
    command = config.get("command")
    if command is not None:
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise OfflinePolicyError("command 必须是非空参数数组")
        executable = Path(command[0]).name.lower()
        if executable in NETWORK_CLIENTS:
            raise OfflinePolicyError("本地模型命令不能直接调用网络下载器")
        if executable in SHELL_EXECUTABLES:
            raise OfflinePolicyError("本地模型命令不能通过通用 Shell 启动")
        lowered = [item.lower() for item in command]
        if executable.startswith("python") and any(item in {"-c", "-m"} for item in lowered[1:]):
            raise OfflinePolicyError("Python 适配器必须指向固定脚本文件，不能使用 -c/-m")
        for item in command:
            parsed = urlparse(item)
            if parsed.scheme in {"http", "https"} and parsed.hostname not in LOOPBACK_HOSTS:
                raise OfflinePolicyError("命令参数不得包含外网 URL")
