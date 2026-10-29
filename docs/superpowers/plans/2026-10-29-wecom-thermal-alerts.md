# WeCom Thermal Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Windows-local, encrypted Enterprise WeChat webhook configuration and a separate reliable notifier for thermal state transitions.

**Architecture:** The existing thermal monitor remains the safety source and never performs network I/O. A new notifier process polls the atomic thermal state, persists deduplication/retry state, and sends minimal Markdown alerts through a validated official WeCom webhook. Windows DPAPI protects the webhook at rest outside the repository.

**Tech Stack:** Python 3.11 standard library, Windows DPAPI via `ctypes`, PowerShell launchers, pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-wecom-thermal-alert-design.md`

## Global Constraints

- Only `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` is accepted.
- The webhook never appears in Git, process arguments, UI, or logs.
- Notification failures never block five-second thermal sampling or dispatch protection.
- Send only thermal/device metadata; never send prompts or media.
- Existing local model, queue, ComfyUI, and recovery behavior remains unchanged.

## Review Focus

- Malformed, redirected, proxied, or non-official webhook URLs must be rejected without leaking the secret.
- Restarting the notifier must not resend every historical event.
- Repeated five-second samples in one state must not spam the group.
- A network failure must persist a pending alert and retry without blocking the monitor.
- Missing/corrupt encrypted configuration must disable notifications safely while leaving thermal protection active.

---

### Task 1: Secure webhook configuration and delivery client

**Files:**
- Create: `src/video_workstation/wecom.py`
- Create: `tests/test_wecom.py`
- Modify: `src/video_workstation/cli.py`

**Interfaces:**
- Produces: `validate_wecom_webhook(url: str) -> str`, `save_wecom_webhook(data_dir: Path, url: str) -> Path`, `load_wecom_webhook(data_dir: Path) -> str | None`, `remove_wecom_webhook(data_dir: Path) -> bool`, `WeComClient.send_markdown(content: str) -> None`.
- Produces CLI commands `configure-wecom`, `test-wecom`, and `disable-wecom`; configuration reads the secret interactively, never from an argument.

- [ ] Write URL validation, secret redaction, DPAPI round-trip, official response and redirect/proxy rejection tests.
- [ ] Run the focused tests and verify they fail before implementation.
- [ ] Implement URL validation, DPAPI storage, atomic file replacement, strict no-proxy/no-redirect HTTP client and controlled errors.
- [ ] Add interactive CLI commands and run the focused tests to green.
- [ ] Commit the task.

### Task 2: Independent notification state machine

**Files:**
- Create: `src/video_workstation/wecom_notifier.py`
- Create: `tests/test_wecom_notifier.py`
- Modify: `src/video_workstation/cli.py`

**Interfaces:**
- Consumes: Task 1 secret loading and `WeComClient`.
- Produces: `ThermalNotifier.tick(status: dict, now: datetime) -> None`, persistent `wecom-notifier-state.json`, public status `wecom-notifier-status.json`, and CLI command `notify-wecom`.

- [ ] Write transition, deduplication, emergency-repeat, unavailable/recovery, retry and restart tests.
- [ ] Run the focused tests and verify they fail before implementation.
- [ ] Implement deterministic message formatting, bounded persistent outbox, exponential retry and atomic state/status files.
- [ ] Add the long-running notifier command with a five-second poll and run focused tests to green.
- [ ] Commit the task.

### Task 3: Windows configuration and automatic startup

**Files:**
- Create: `scripts/configure_wecom_alerts.ps1`
- Create: `scripts/disable_wecom_alerts.ps1`
- Modify: `scripts/start_local_workstation.ps1`
- Modify: `README.md`
- Create outside Git delivery root: `配置企业微信温度提醒.cmd`, `关闭企业微信温度提醒.cmd`

**Interfaces:**
- Consumes: Task 1 CLI configuration and Task 2 notifier command.
- Produces: beginner-facing Windows entry points and automatic owned notifier startup when encrypted configuration exists.

- [ ] Write launcher contract tests for secret-free invocation, owned-process detection, configured-only startup and no command-line webhook.
- [ ] Run the focused tests and verify they fail before implementation.
- [ ] Add repository PowerShell wrappers, notifier process ownership/status logging and beginner-facing delivery wrappers.
- [ ] Parse every PowerShell script, run focused tests, and run a local fake-transport configuration/notifier smoke test without revealing a webhook.
- [ ] Update operations documentation and commit the task.

### Task 4: Full verification and independent review

**Files:**
- Review all files changed by Tasks 1–3.

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: verified implementation ready for local webhook entry.

- [ ] Run the complete pytest suite.
- [ ] Run PowerShell parser validation for all modified scripts.
- [ ] Verify Git diff contains no webhook key pattern or secret value.
- [ ] Perform an independent code review focused on the five Review Focus items and fix Critical/Important findings with RED→GREEN tests.
- [ ] Commit verified fixes and prepare the local interactive configuration window.
