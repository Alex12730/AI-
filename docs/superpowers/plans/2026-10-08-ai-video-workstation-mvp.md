# AI Video Workstation MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** 交付一个能在 Windows/WSL2 启动的纯本地视频生产 MVP，完整演示账号、项目、分镜审批、持久化单 GPU 队列、模型准入、本地适配器、质检和审计流程。

**Architecture:** 使用 FastAPI 模块化单体承载 Web/API；SQLAlchemy + SQLite WAL 持久化业务和队列；独立 Worker 通过租约领取任务；模型执行封装为安全参数数组适配器；Jinja2 页面提供成员与管理员工作台。无权重环境使用明确标注的 Demo 适配器打通流程，真实模型必须通过配置和基准准入。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、Jinja2、argon2-cffi、Uvicorn、pytest、原生 JavaScript、FFmpeg/ffprobe。

**Spec:** `docs/superpowers/specs/2026-10-08-ai-video-workstation-design.md`

## Global Constraints

- 不包含云端视频 API 客户端或云端执行器。
- 不下载大模型权重，不把宣传参数当作本机验证结果。
- 所有生产代码遵守测试先行；外部命令必须 `shell=False` 且 argv 可审计。
- 保持单机、单 Worker、低维护复杂度；不引入 Redis、Celery、React。
- 不提交密钥、密码、Cookie、模型权重和生成素材。

## Review Focus

- 权限绕过、项目越权、P0 非管理员使用。
- SQLite 任务认领的重复执行、租约恢复与重试边界。
- 路径穿越、Shell 注入、日志敏感信息、低磁盘绕过。
- H3 未验证时长是否可能从 API 或 UI 入队。
- Web 服务是否会被 Worker 或模型异常拖垮。

### Task 1: Scaffold and persistence foundation

**Files:** `pyproject.toml`, `.gitignore`, `src/video_workstation/config.py`, `src/video_workstation/db.py`, `src/video_workstation/models.py`, `alembic.ini`, `migrations/*`, `tests/test_database.py`

1. Write tests that assert all nine entities persist and SQLite uses WAL/foreign keys.
2. Run tests and verify they fail because the package does not exist.
3. Implement the package, models, engine factory, schema bootstrap and initial migration.
4. Run targeted tests, then the full suite.

**Produces:** `create_engine_and_session(settings)` and ORM entities consumed by all later tasks.

### Task 2: Authentication, authorization and project workflow

**Files:** `src/video_workstation/security.py`, `src/video_workstation/services/projects.py`, `src/video_workstation/cli.py`, `tests/test_auth_and_projects.py`

1. Write tests for Argon2 hashing, no default admin, project ownership, storyboard approval and admin-only P0.
2. Watch the tests fail for missing behavior.
3. Implement session-safe authentication helpers, explicit admin bootstrap and project/storyboard services.
4. Run targeted tests, then the full suite.

**Consumes:** Task 1 session factory and entities. **Produces:** role guards and approved-shot workflow.

### Task 3: Persistent single-GPU queue and safe adapters

**Files:** `src/video_workstation/queue.py`, `src/video_workstation/adapters/*`, `src/video_workstation/worker.py`, `tests/test_queue.py`, `tests/test_adapters.py`

1. Write tests for FIFO/aging, atomic claim, lease recovery, one infrastructure retry, no quality retry, cancellation and safe argv rendering.
2. Watch them fail.
3. Implement queue service, worker loop, Demo adapter and configurable local command adapters for Wan/H3/LTX.
4. Run targeted tests, then the full suite.

**Consumes:** Task 1 entities and Task 2 permission decisions. **Produces:** task state machine and adapter contract.

### Task 4: Model profiles, H3 qualification and storage/QC boundaries

**Files:** `src/video_workstation/services/models.py`, `src/video_workstation/storage.py`, `src/video_workstation/qc.py`, `src/video_workstation/benchmark.py`, `tests/test_model_profiles.py`, `tests/test_storage_qc.py`

1. Write tests for H3 FL2VA defaults, duration/aspect qualification, 90% gate, storage threshold, generated paths and ffprobe result classification.
2. Watch them fail.
3. Implement profile seeding, benchmark recording, admission checks, storage guard and QC parser.
4. Run targeted tests, then the full suite.

**Consumes:** Task 1 entities and Task 3 adapter results. **Produces:** `admit_generation()` used by API and UI.

### Task 5: FastAPI API and Operate-mode web UI

**Files:** `src/video_workstation/main.py`, `src/video_workstation/api.py`, `src/video_workstation/templates/*`, `src/video_workstation/static/*`, `tests/test_api.py`

1. Write end-to-end API tests for bootstrap/login, project creation, approval, enqueue, model gating, admin pages and access denial.
2. Watch them fail.
3. Implement app factory, cookie sessions, HTML routes, JSON API and the responsive “片场调度台” interface from `DESIGN.md`.
4. Run targeted tests, full suite, start Uvicorn and perform one desktop/mobile UI inspection pass.

**Consumes:** Tasks 1–4 public services. **Produces:** runnable application and documented API.

### Task 6: Operations, documentation and final verification

**Files:** `README.md`, `.env.example`, `scripts/run_web.ps1`, `scripts/run_worker.ps1`, `scripts/preflight.ps1`, `.feedback/*`, `tests/test_offline_contract.py`

1. Write an offline-contract test that rejects outbound HTTP clients and cloud executor settings in production modules.
2. Watch it fail until the operational surface is complete.
3. Add startup/preflight scripts, environment template, deployment/model installation runbook, license notice guidance and honest acceptance checklist.
4. Run all tests, compile/import checks, migration smoke test, FFmpeg preflight and a secrets scan.
5. Record tool friction and ambiguous requirements in `.feedback`, request one fresh whole-branch review, fix Important/Critical findings with RED→GREEN tests, then re-run the full verification set.

**Consumes:** all prior tasks. **Produces:** handoff-ready MVP repository.
