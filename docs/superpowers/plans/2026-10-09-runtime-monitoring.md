# 运行监控与告警 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为管理员提供可信的 GPU、磁盘、Worker、队列和模型准备度监控，并在探测失败时保持 Web 可用。

**Architecture:** 新建独立的系统状态服务，固定参数调用 `nvidia-smi` 并组合磁盘、Worker、队列和模型数据。Worker 心跳使用独立的 `WorkerStatus` 表，不替换现有任务租约；FastAPI 只负责权限、序列化和服务端页面。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、Jinja2、pytest、原生 `subprocess`/`shutil`。

**Spec:** `docs/superpowers/specs/2026-10-09-workstation-software-completion-design.md`

## Global Constraints

- 保留 FastAPI、Jinja2、SQLite WAL 和单 Worker 架构，不引入前端框架或监控服务依赖。
- 所有本地命令必须使用参数数组和 `shell=False`；探测超时不能拖垮 Web。
- `/health` 保持轻量，不执行 GPU 或磁盘探测。
- 80℃为 GPU 严重告警阈值；显存使用率 92% 或剩余少于 4GB 为严重告警。
- 受管磁盘剩余少于 100GB为严重告警；不自动降低任务参数。
- 普通成员不能访问完整系统状态 API 和页面。
- 每次提交前更新 `.feedback/2026-10-08-agent-experience.md`。

## Review Focus

- `nvidia-smi` 不存在、超时或输出列数错误时返回 `unavailable`，不能抛出 500；Task 2 测试覆盖。
- 2GB 显卡不能因为“少于 4GB 可用”永久误报，只有存在排队/运行重型任务时才评估绝对余量；Task 2 测试覆盖。
- Worker 空闲时也必须有心跳，进程停止后两个周期内转为离线；Task 3 测试覆盖。
- `enabled=True` 但无命令或无准入档位的模型不能显示为可生产；Task 2 与 Task 4 测试覆盖。
- 普通成员请求 `/system` 或 `/api/system/status` 必须得到 403，而不是看到硬件细节；Task 4 测试覆盖。

---

### Task 1: Worker 状态持久化

**Files:**
- Modify: `src/video_workstation/models.py`
- Create: `migrations/versions/20261009_0003_worker_status.py`
- Modify: `tests/test_database.py`

**Interfaces:**
- Produces: `WorkerStatus(worker_id, state, current_task_id, started_at, last_heartbeat_at, details_json)` SQLAlchemy 实体。
- Consumes: 现有 `utcnow()` 和 `Task.id`。

- [ ] **Step 1: Write the failing database tests**

在 `tests/test_database.py` 增加 `worker_statuses` 表存在、同一 `worker_id` 唯一、`current_task_id` 可空且任务删除后置空、Alembic 版本升级到 `20261009_0003` 的断言。

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_database.py -q`

Expected: FAIL，缺少 `WorkerStatus` 或迁移版本仍为 `20261008_0002`。

- [ ] **Step 3: Add the model and idempotent migration**

在 `models.py` 增加实体；迁移先检查表是否存在，再创建表、唯一索引和心跳时间索引。`state` 限定由服务层写入 `starting|idle|running|stopped`。

- [ ] **Step 4: Run focused tests**

Expected: `tests/test_database.py` 全部 PASS。

- [ ] **Step 5: Update feedback and commit**

```powershell
git add src/video_workstation/models.py migrations/versions/20261009_0003_worker_status.py tests/test_database.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: persist worker runtime status"
```

### Task 2: 系统状态采集和告警计算

**Files:**
- Create: `src/video_workstation/services/system_status.py`
- Create: `tests/test_system_status.py`
- Modify: `src/video_workstation/services/models.py`

**Interfaces:**
- Produces: `parse_nvidia_smi(csv_text: str) -> list[GPUStatus]`。
- Produces: `probe_gpu(run: Callable[..., CompletedProcess] = subprocess.run) -> ProbeResult[list[GPUStatus]]`。
- Produces: `collect_system_status(session: Session, settings: Settings, *, now: datetime | None = None) -> SystemStatus`。
- Produces: `model_readiness(profile: ModelProfile) -> Literal['demo','registered','configured','admitted']`。

- [ ] **Step 1: Write parser, failure and threshold tests**

覆盖多 GPU CSV、型号字符串含空格、命令不存在、超时、畸形输出、80℃、92%、A6000 剩余不足 4GB、GT 730 空闲不触发绝对余量告警、磁盘不足、队列无健康 Worker、连续 OOM/基础设施失败以及三层模型准备度。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_system_status.py -q`

Expected: FAIL，模块不存在。

- [ ] **Step 3: Implement immutable status value objects and probes**

使用 dataclass 表达 `GPUStatus`、`DiskStatus`、`Alert`、`ProbeResult` 和 `SystemStatus`。`probe_gpu` 固定使用 `nvidia-smi --query-gpu=name,driver_version,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits`，超时 3 秒，输出最大 64KB。

- [ ] **Step 4: Implement readiness and alert aggregation**

`configured` 必须同时满足非 Demo、本地命令数组有效、版本与量化不为 `unconfigured`；`admitted` 还必须至少有一个当前配置指纹匹配的准入档。

- [ ] **Step 5: Run focused tests**

Expected: 全部 PASS。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/services/system_status.py src/video_workstation/services/models.py tests/test_system_status.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: collect workstation health signals"
```

### Task 3: Worker 空闲与运行心跳

**Files:**
- Create: `src/video_workstation/services/workers.py`
- Modify: `src/video_workstation/worker.py`
- Modify: `src/video_workstation/cli.py`
- Modify: `tests/test_queue.py`
- Modify: `tests/test_review_fixes.py`

**Interfaces:**
- Consumes: Task 1 的 `WorkerStatus`。
- Produces: `touch_worker(session: Session, worker_id: str, state: str, *, current_task_id: str | None = None, now: datetime | None = None) -> WorkerStatus`。
- Produces: `worker_is_healthy(status: WorkerStatus, *, now: datetime, heartbeat_seconds: float) -> bool`。

- [ ] **Step 1: Write failing heartbeat tests**

验证首次调用创建记录、重复调用更新同一记录、无任务时写 `idle`、认领后写 `running/current_task_id`、成功或失败后恢复 `idle`，陈旧心跳判定离线但不改任务租约。

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_queue.py tests\test_review_fixes.py -q`

Expected: FAIL，缺少 Worker 状态记录。

- [ ] **Step 3: Implement worker status service and integrate lifecycle**

`Worker.run_once()` 在认领前触摸 `idle`，认领后提交 `running`，所有成功/失败返回路径都恢复 `idle`。CLI 捕获 `KeyboardInterrupt` 时用独立短事务写 `stopped`；心跳失败记录受控错误但不得覆盖任务结果。

- [ ] **Step 4: Run focused and queue tests**

Expected: 全部 PASS，既有租约并发测试不回退。

- [ ] **Step 5: Update feedback and commit**

```powershell
git add src/video_workstation/services/workers.py src/video_workstation/worker.py src/video_workstation/cli.py tests/test_queue.py tests/test_review_fixes.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: report worker heartbeat state"
```

### Task 4: 管理员运行状态页面和模型状态文案

**Files:**
- Modify: `src/video_workstation/main.py`
- Create: `src/video_workstation/templates/system.html`
- Modify: `src/video_workstation/templates/base.html`
- Modify: `src/video_workstation/templates/models.html`
- Modify: `src/video_workstation/static/app.css`
- Create: `tests/test_system_routes.py`
- Modify: `tests/test_model_profiles.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 2 的 `collect_system_status()`、`model_readiness()`。
- Produces: `GET /api/system/status` 和 `GET /system`，均仅管理员访问。
- Produces: `status_context(request: Request) -> dict`，只读取 `app.state.system_status_summary` 的缓存摘要。

- [ ] **Step 1: Write failing route and rendering tests**

测试未登录重定向/401、普通成员 403、管理员 JSON 字段、探测 unavailable 仍返回 200、模型页区分 Demo/已登记/已配置/已准入、导航只对管理员显示“运行状态”。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_system_routes.py tests\test_model_profiles.py -q`

Expected: FAIL，路由或模板不存在。

- [ ] **Step 3: Add API/page routes and compact server-rendered UI**

系统页分 GPU、磁盘、Worker、队列、模型准备度和当前告警六块；保持移动端单列。`/system` 和 `/api/system/status` 刷新 `app.state.system_status_summary`；Jinja 上下文处理器只读取该摘要，其他页面不会调用 `nvidia-smi`。尚未采集时侧边栏显示“状态未检查”。

- [ ] **Step 4: Update model readiness rendering and README**

把现有“启用/停用”与“生产准备度”分列；README 增加状态页面说明和“监控不等于 A6000 验收”边界。

- [ ] **Step 5: Run tests and full verification**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest -q`

Run: `.\.venv\Scripts\python.exe -m compileall -q src`

Run: `git diff --check`

Expected: 全部 PASS、compileall 退出 0、无空白错误。

- [ ] **Step 6: Browser acceptance**

启动 Web 与 Worker，登录管理员，验证当前 GT 730 型号、显存/温度、Worker 空闲状态、模型准备度和移动端布局；不得改动真实项目数据。

- [ ] **Step 7: Update feedback and commit**

```powershell
git add src/video_workstation/main.py src/video_workstation/templates/system.html src/video_workstation/templates/base.html src/video_workstation/templates/models.html src/video_workstation/static/app.css tests/test_system_routes.py tests/test_model_profiles.py README.md .feedback/2026-10-08-agent-experience.md
git commit -m "feat: add workstation status dashboard"
```
