"""Strict Enterprise WeChat webhook storage and delivery.

The webhook is a bearer secret. It is encrypted for the current Windows user
and never accepted through command-line arguments or written to logs.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


SECRET_RELATIVE_PATH = Path("secrets") / "wecom-webhook.dpapi"
_ENTROPY = b"AI Video Workstation: WeCom webhook:v1"
_DESCRIPTION = "AI Video Workstation WeCom webhook"


class WeComConfigurationError(ValueError):
    pass


class WeComDeliveryError(RuntimeError):
    pass


def validate_wecom_webhook(url: str) -> str:
    value = url.strip() if isinstance(url, str) else ""
    try:
        parsed = urllib.parse.urlsplit(value)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        port = parsed.port
    except (ValueError, TypeError):
        raise WeComConfigurationError("企业微信 Webhook 地址格式不正确") from None
    key = query.get("key", [])
    if (parsed.scheme != "https" or parsed.hostname != "qyapi.weixin.qq.com"
        or parsed.username or parsed.password or port is not None
        or parsed.path != "/cgi-bin/webhook/send" or parsed.fragment
        or set(query) != {"key"} or len(key) != 1
        or not 16 <= len(key[0]) <= 128
        or any(not (character.isascii() and (character.isalnum() or character in "_-")) for character in key[0])):
        raise WeComConfigurationError("请输入企业微信官方消息推送的完整 Webhook 地址")
    return value


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise WeComConfigurationError("企业微信密钥配置只支持当前 Windows 工作站")
    source, source_buffer = _input_blob(data)
    entropy, entropy_buffer = _input_blob(_ENTROPY)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    try:
        if decrypt:
            description = wintypes.LPWSTR()
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(source), ctypes.byref(description), ctypes.byref(entropy),
                None, None, flags, ctypes.byref(output),
            )
            if description:
                kernel32.LocalFree(description)
        else:
            ok = crypt32.CryptProtectData(
                ctypes.byref(source), _DESCRIPTION, ctypes.byref(entropy),
                None, None, flags, ctypes.byref(output),
            )
        if not ok:
            raise ctypes.WinError()
        return ctypes.string_at(output.pbData, output.cbData)
    except (OSError, ValueError):
        raise WeComConfigurationError("企业微信 Webhook 加密配置无法读取") from None
    finally:
        # Keep input buffers alive until the native call is complete.
        _ = source_buffer, entropy_buffer
        if output.pbData:
            kernel32.LocalFree(output.pbData)


def _secret_path(data_dir: Path) -> Path:
    return Path(data_dir).resolve() / SECRET_RELATIVE_PATH


def _clear_notifier_delivery_state(data_dir: Path) -> None:
    root = Path(data_dir).resolve()
    for name in ("wecom-notifier-state.json", "wecom-notifier-status.json"):
        try:
            (root / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise WeComConfigurationError("企业微信通知状态无法重置") from None


def save_wecom_webhook(data_dir: Path, url: str) -> Path:
    validated = validate_wecom_webhook(url)
    path = _secret_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = _dpapi(validated.encode("utf-8"), decrypt=False)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encrypted)
        os.replace(temporary, path)
        _clear_notifier_delivery_state(data_dir)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def load_wecom_webhook(data_dir: Path) -> str | None:
    path = _secret_path(data_dir)
    try:
        encrypted = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise WeComConfigurationError("企业微信 Webhook 加密配置无法读取") from None
    try:
        return validate_wecom_webhook(_dpapi(encrypted, decrypt=True).decode("utf-8"))
    except (UnicodeError, WeComConfigurationError):
        raise WeComConfigurationError("企业微信 Webhook 加密配置无法读取") from None


def remove_wecom_webhook(data_dir: Path) -> bool:
    path = _secret_path(data_dir)
    removed = False
    try:
        path.unlink()
        removed = True
    except FileNotFoundError:
        pass
    _clear_notifier_delivery_state(data_dir)
    return removed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise WeComDeliveryError("企业微信拒绝了消息请求")


class WeComClient:
    def __init__(self, webhook: str, *, opener=None):
        self.webhook = validate_wecom_webhook(webhook)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def send_markdown(self, content: str) -> None:
        if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 4096:
            raise WeComDeliveryError("企业微信消息内容无效")
        body = json.dumps({"msgtype": "markdown", "markdown": {"content": content}},
                          ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("errcode") != 0:
                raise WeComDeliveryError("企业微信未接受消息")
        except WeComDeliveryError:
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise WeComDeliveryError("企业微信消息发送失败") from None
