# 分镜编辑与产物预览 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在草稿阶段完整编辑镜头，并安全地登记、预览和下载真实生成视频。

**Architecture:** 扩展现有镜头服务层，所有页面/API 共用同一校验。新增资产服务负责幂等登记、哈希和路径边界；受保护媒体路由负责权限与 Range 响应，项目页仅消费数据库资产记录。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、Jinja2、pytest、HTML5 video。

**Spec:** `docs/superpowers/specs/2026-10-09-workstation-software-completion-design.md`

## Global Constraints

- 仅草稿分镜可编辑；待审批和已批准分镜保持只读。
- 镜头类型只允许 `product_ui|broll|character|series_drama`。
- `product_ui` 不得选择真实生成模型，继续使用录屏或截图动效。
- Demo 任务不登记假视频资产。
- 资产路由必须验证登录、项目权限、数据库记录和规范化路径。
- 不显示磁盘绝对路径，不接受用户提交任意输出路径。
- 每次提交前更新 `.feedback/2026-10-08-agent-experience.md`。

## Review Focus

- 标题或提示词全是空白时拒绝保存，且不部分更新其他字段；Task 1 测试覆盖。
- 同一任务重试或 Worker 重放不能创建重复视频资产；Task 2 测试覆盖。
- 上传的真实录屏必须分块写入受管目录、限制大小并通过视频结构质检；Task 3 测试覆盖。
- 数据库记录指向资产根目录外、符号链接逃逸或文件丢失时拒绝访问；Task 4 测试覆盖。
- Range 的空值、后缀范围、越界和多范围输入必须返回正确 206/416；Task 4 测试覆盖。
- 无权限成员即使知道 asset ID 也不能播放或下载；Task 4 与 Task 5 测试覆盖。

---

### Task 1: 完整草稿镜头编辑

**Files:**
- Modify: `src/video_workstation/services/projects.py`
- Modify: `src/video_workstation/main.py`
- Modify: `src/video_workstation/templates/project.html`
- Modify: `src/video_workstation/static/app.css`
- Modify: `tests/test_auth_and_projects.py`
- Modify: `tests/test_operations.py`

**Interfaces:**
- Produces: `SHOT_SCENE_TYPES = ('product_ui','broll','character','series_drama')`。
- Changes: `update_draft_shot(..., negative_prompt: str, scene_type: str, ...) -> Shot`。
- Changes: `ShotUpdate` 增加 `negative_prompt` 和严格 scene type 字段。

- [ ] **Step 1: Write failing service and page tests**

测试合法更新五类字段、空白标题/提示词、非法镜头类型、非草稿、无权限、审计详情，以及草稿页面显示标题/正向/负面/类型/时长/画幅控件，已审批页面不显示表单。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_auth_and_projects.py tests\test_operations.py -q`

Expected: FAIL，现有函数不接收新增字段或页面缺控件。

- [ ] **Step 3: Extend domain validation and both write routes**

服务函数在修改对象前完成全部校验；JSON PATCH 与服务端 `/shots/{shot_id}/settings` 表单都调用同一函数并返回/重定向现有格式。

- [ ] **Step 4: Expand the draft form without changing approval UX**

每个镜头使用可折叠编辑区域；移动端控件单列且最小 44px。批量时长/画幅栏保持原样。

- [ ] **Step 5: Run focused and full tests**

Expected: 目标测试和全量测试 PASS。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/services/projects.py src/video_workstation/main.py src/video_workstation/templates/project.html src/video_workstation/static/app.css tests/test_auth_and_projects.py tests/test_operations.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: edit complete shot metadata"
```

### Task 2: 视频资产幂等登记

**Files:**
- Modify: `src/video_workstation/models.py`
- Create: `migrations/versions/20261009_0004_asset_idempotency.py`
- Create: `src/video_workstation/services/assets.py`
- Modify: `src/video_workstation/worker.py`
- Modify: `tests/test_database.py`
- Modify: `tests/test_review_fixes.py`
- Create: `tests/test_assets.py`

**Interfaces:**
- Produces: `register_video_asset(session: Session, task: Task, path: Path, qc: QCResult) -> Asset`。
- Produces: `sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str`。
- Produces: 唯一约束 `uq_assets_task_kind(task_id, kind)`；空 `task_id` 的归档资产不受影响。

- [ ] **Step 1: Write failing asset and migration tests**

验证真实成功任务创建 `kind=video`、包含哈希和完整 QC 元数据；同一任务二次登记返回原记录；Demo 不登记；不同任务可各有视频；迁移升级成功。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_assets.py tests\test_database.py tests\test_review_fixes.py -q`

Expected: FAIL，服务或唯一约束不存在。

- [ ] **Step 3: Implement streaming hash and idempotent registration**

`register_video_asset` 要求任务已有 project/shot、文件存在、QC passed；先查询相同 task/kind，存在则返回，不覆盖历史元数据。

- [ ] **Step 4: Register after QC and before task success**

Worker 只在非 Demo 输出通过质检后调用登记服务，再写 `Task.result_json`。登记或哈希失败按基础设施错误处理，不把未登记文件报告为成功。

- [ ] **Step 5: Run focused and full tests**

Expected: 全部 PASS。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/models.py migrations/versions/20261009_0004_asset_idempotency.py src/video_workstation/services/assets.py src/video_workstation/worker.py tests/test_database.py tests/test_review_fixes.py tests/test_assets.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: register generated video assets"
```

### Task 3: 真实录屏安全导入

**Files:**
- Modify: `src/video_workstation/config.py`
- Modify: `src/video_workstation/services/assets.py`
- Modify: `src/video_workstation/main.py`
- Modify: `src/video_workstation/templates/project.html`
- Modify: `tests/test_assets.py`

**Interfaces:**
- Produces: `Settings.maximum_video_upload_bytes`，默认 `4 * 1024**3`。
- Produces: `store_uploaded_video(session: Session, actor: User, shot: Shot, filename: str, stream: BinaryIO, settings: Settings) -> Asset`。
- Produces: `POST /shots/{shot_id}/assets/video`，multipart 字段名 `video`。

- [ ] **Step 1: Write failing upload tests**

覆盖项目成员可上传、无权限 403、非 mp4/mov/webm 拒绝、超 4GB 模拟流在越界时停止、空文件、无视频轨、时长偏差、无音轨允许、临时文件清理、资产元数据标记 `source=upload`，以及 shot/project 关联正确。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_assets.py -q`

Expected: FAIL，上传服务或路由不存在。

- [ ] **Step 3: Implement chunked managed upload**

每次读取最多 1MB，同时计算 SHA-256 和累计字节；写入同目录临时文件，完成后执行 `probe_media(..., require_audio=False)`，通过才原子改名并登记 `kind=video`。任何失败都删除临时文件，不记录客户端原始路径。

- [ ] **Step 4: Add per-shot upload form**

已批准镜头和 `product_ui` 镜头显示“上传真实录屏”；表单文案说明不会调用生成模型。上传成功回到项目页。

- [ ] **Step 5: Run focused tests**

Expected: 全部 PASS。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/config.py src/video_workstation/services/assets.py src/video_workstation/main.py src/video_workstation/templates/project.html tests/test_assets.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: import real screen recordings"
```

### Task 4: 安全媒体 Range 响应

**Files:**
- Create: `src/video_workstation/media.py`
- Modify: `src/video_workstation/main.py`
- Modify: `tests/test_assets.py`

**Interfaces:**
- Produces: `resolve_asset_path(asset: Asset, settings: Settings) -> Path`，只允许 `asset_dir` 或 `archive_dir` 下的常规文件。
- Produces: `parse_range_header(value: str | None, file_size: int) -> ByteRange | None`。
- Produces: `GET /assets/{asset_id}/content?download=0|1`。

- [ ] **Step 1: Write failing path, authorization and Range tests**

覆盖完整响应、`bytes=0-99`、`bytes=100-`、`bytes=-100`、越界、多范围、零字节文件、丢失文件、外部路径、符号链接逃逸、无权限和下载响应头。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_assets.py -q`

Expected: FAIL，媒体模块或路由不存在。

- [ ] **Step 3: Implement bounded streaming**

`resolve_asset_path` 使用 `Path.resolve(strict=True)` 和 `is_relative_to()`；Range 仅接受单范围。206 返回 `Accept-Ranges`、`Content-Range`、准确 `Content-Length`；无效范围返回 416 和 `Content-Range: bytes */size`。

- [ ] **Step 4: Add project authorization before path resolution**

路由先加载 Asset 和 Project，调用 `assert_project_access()`，再解析文件。错误响应不包含真实路径。

- [ ] **Step 5: Run focused tests**

Expected: 全部 PASS。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/media.py src/video_workstation/main.py tests/test_assets.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: stream authorized media assets"
```

### Task 5: 项目资产预览界面

**Files:**
- Modify: `src/video_workstation/main.py`
- Modify: `src/video_workstation/templates/project.html`
- Modify: `src/video_workstation/static/app.css`
- Modify: `tests/test_operations.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 2 的 `Asset` 元数据和 Task 4 的媒体路由。
- Produces: 项目页 `assets_by_shot` 和 `composite_assets` 模板上下文。

- [ ] **Step 1: Write failing project-page tests**

验证最新视频默认展示播放器、历史资产折叠、模型/版本/量化/种子/时长/画幅/QC 可见、Demo 明示无媒体、文件丢失显示状态、下载链接使用受保护路由且不出现绝对路径。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_operations.py -q`

Expected: FAIL，页面没有资产区域。

- [ ] **Step 3: Load authorized assets and render lazy video elements**

只查询当前项目资产；播放器使用 `preload="metadata"`、不自动播放，历史资产放进 `<details>`。缺失文件在服务端计算状态，不触发页面渲染异常。

- [ ] **Step 4: Add responsive styles and README usage**

播放器宽度不超过容器，移动端元数据改为单列；README 说明预览权限和 Demo 边界。

- [ ] **Step 5: Full verification and browser acceptance**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest -q`

Run: `.\.venv\Scripts\python.exe -m compileall -q src`

Run: `git diff --check`

使用系统生成的本地短视频测试播放、拖动和下载；用无权限成员验证隔离；不修改用户已有项目。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/main.py src/video_workstation/templates/project.html src/video_workstation/static/app.css tests/test_operations.py README.md .feedback/2026-10-08-agent-experience.md
git commit -m "feat: preview project video assets"
```
