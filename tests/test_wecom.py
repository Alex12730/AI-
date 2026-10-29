from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from video_workstation.wecom import (
    WeComClient,
    WeComConfigurationError,
    WeComDeliveryError,
    load_wecom_webhook,
    remove_wecom_webhook,
    save_wecom_webhook,
    validate_wecom_webhook,
)


VALID = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa"


@pytest.mark.parametrize(
    "url",
    [
        "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa",
        "https://evil.example/cgi-bin/webhook/send?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa",
        "https://user@qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa",
        "https://qyapi.weixin.qq.com:444/cgi-bin/webhook/send?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=short",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa&x=1",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91f6-7abc-4bc4-97a0-0ec2f1fa5aaa#fragment",
    ],
)
def test_webhook_validation_accepts_only_official_send_endpoint(url):
    with pytest.raises(WeComConfigurationError, match="企业微信"):
        validate_wecom_webhook(url)


def test_webhook_validation_normalizes_surrounding_whitespace():
    assert validate_wecom_webhook(f"  {VALID}\n") == VALID


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI storage")
def test_webhook_is_dpapi_encrypted_at_rest_and_round_trips(tmp_path):
    saved = save_wecom_webhook(tmp_path, VALID)
    raw = saved.read_bytes()
    assert b"qyapi.weixin.qq.com" not in raw
    assert b"693a91f6" not in raw
    assert load_wecom_webhook(tmp_path) == VALID
    assert remove_wecom_webhook(tmp_path) is True
    assert load_wecom_webhook(tmp_path) is None
    assert remove_wecom_webhook(tmp_path) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI storage")
def test_corrupt_encrypted_webhook_fails_without_echoing_bytes(tmp_path):
    secret = tmp_path / "secrets" / "wecom-webhook.dpapi"
    secret.parent.mkdir()
    secret.write_bytes(b"not-a-secret")
    with pytest.raises(WeComConfigurationError) as raised:
        load_wecom_webhook(tmp_path)
    assert "not-a-secret" not in str(raised.value)


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class RecordingOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return Response(self.outcome)


def test_client_sends_utf8_markdown_with_short_timeout():
    opener = RecordingOpener({"errcode": 0, "errmsg": "ok"})
    WeComClient(VALID, opener=opener).send_markdown("GPU 温度 85℃")
    request, timeout = opener.requests[0]
    assert request.full_url == VALID
    assert request.get_method() == "POST"
    assert request.headers["Content-type"] == "application/json"
    assert json.loads(request.data) == {"msgtype": "markdown", "markdown": {"content": "GPU 温度 85℃"}}
    assert timeout == 5


@pytest.mark.parametrize("payload", [{"errcode": 93000, "errmsg": "invalid webhook"}, {}, []])
def test_client_rejects_non_success_responses_without_leaking_webhook(payload):
    with pytest.raises(WeComDeliveryError) as raised:
        WeComClient(VALID, opener=RecordingOpener(payload)).send_markdown("test")
    assert "key=" not in str(raised.value)


def test_client_hides_transport_error_and_webhook():
    error = urllib.error.URLError(f"failed to connect to {VALID}")
    with pytest.raises(WeComDeliveryError, match="发送失败") as raised:
        WeComClient(VALID, opener=RecordingOpener(error)).send_markdown("test")
    assert "key=" not in str(raised.value)


def test_default_client_builds_with_empty_proxy_and_no_redirect(monkeypatch):
    observed = []
    real_build_opener = urllib.request.build_opener

    def capture(*handlers):
        observed.extend(handlers)
        return real_build_opener(*handlers)

    monkeypatch.setattr(urllib.request, "build_opener", capture)
    WeComClient(VALID)
    proxy = next(handler for handler in observed if type(handler).__name__ == "ProxyHandler")
    assert proxy.proxies == {}
    assert any(type(handler).__name__ == "NoRedirect" for handler in observed)
