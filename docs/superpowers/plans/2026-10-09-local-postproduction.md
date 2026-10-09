# 本地后期任务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用受控 FFmpeg 参数数组完成项目视频拼接、字幕烧录和 1080p H.264/AAC 交付，并为已准入的本地 Video2X 提供默认关闭的入口。

**Architecture:** 字幕和视频都先登记为项目资产；后期服务只消费通过权限和质检的资产 ID。合成、交付编码和超分继续使用现有持久化队列，Worker 按受控 `task_type` 分派，结果统一登记为资产。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Jinja2、pytest、本地 FFmpeg/ffprobe、可选本地 Video2X。

**Spec:** `docs/superpowers/specs/2026-10-09-workstation-software-completion-design.md`

## Global Constraints

- 依赖 `2026-10-09-authoring-and-asset-preview.md` 已完成的资产登记和安全路径服务。
- FFmpeg、ffprobe、Video2X 都使用参数数组和 `shell=False`。
- 输入仅限同项目且已经通过质检的视频资产；缺少镜头时不启动进程。
- 输出固定为 1080p H.264/AAC，支持 16:9 和 9:16；不静默拉伸，使用等比缩放和补边。
- 字幕仅接受管理员确认的 UTF-8 SRT，最大 2MB，不引用外部 URL。
- Video2X 默认关闭，未配置或未准入时禁止入队，不回退云端。
- 每次提交前更新 `.feedback/2026-10-08-agent-experience.md`。

## Review Focus

- SRT 的 BOM、CRLF、重叠时间轴、负时长和超长行必须被确定性处理或拒绝；Task 1 测试覆盖。
- 文件名包含空格、中文或 FFmpeg 特殊字符时不能变成命令注入；Task 2 测试覆盖 argv 元素。
- 输入视频无音轨时合成仍应产生 AAC 静音轨；Task 2 集成测试覆盖。
- Worker 重启后同一后期任务不能覆盖已登记成功资产；Task 3 测试覆盖幂等路径与租约。
- Video2X Profile 仅 `enabled=True` 但没有命令/准入档时必须保持禁用；Task 4 测试覆盖。

---

### Task 1: SRT 字幕资产上传与校验

**Files:**
- Create: `src/video_workstation/subtitles.py`
- Modify: `src/video_workstation/main.py`
- Modify: `src/video_workstation/templates/project.html`
- Create: `tests/test_subtitles.py`
- Modify: `src/video_workstation/static/app.css`

**Interfaces:**
- Produces: `parse_srt(text: str) -> list[SubtitleCue]`。
- Produces: `store_subtitle_asset(session: Session, project: Project, actor: User, filename: str, content: bytes, settings: Settings) -> Asset`。
- Produces: `POST /projects/{project_id}/subtitles`，仅管理员，multipart 字段名 `subtitle`。

- [ ] **Step 1: Write failing parser/upload tests**

覆盖 UTF-8/UTF-8 BOM、CRLF、合法多段、非法编号、非法时间戳、结束早于开始、重叠时间轴、空文本、超过 2MB、错误扩展名、普通成员 403、跨项目访问和审计日志。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_subtitles.py -q`

Expected: FAIL，字幕模块或路由不存在。

- [ ] **Step 3: Implement strict SRT parsing and managed storage**

保存路径使用 `generated_asset_path(settings.asset_dir, project.id, new_id(), '.srt')`；先完整校验，再原子写临时文件并替换，登记 `Asset(kind='subtitle')`、哈希、cue 数量和总时长。

- [ ] **Step 4: Add admin upload form and asset list**

项目页只对管理员显示字幕上传；页面显示文件名、cue 数量和已确认状态，不显示绝对路径。

- [ ] **Step 5: Run focused tests**

Expected: 全部 PASS。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/subtitles.py src/video_workstation/main.py src/video_workstation/templates/project.html src/video_workstation/static/app.css tests/test_subtitles.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: upload validated subtitle assets"
```

### Task 2: FFmpeg 参数构造与本地合成执行器

**Files:**
- Create: `src/video_workstation/postproduction.py`
- Create: `tests/test_postproduction.py`
- Modify: `src/video_workstation/qc.py`

**Interfaces:**
- Produces: `PostproductionRequest(task_id, input_paths, output_path, aspect_ratio, subtitle_path=None)`。
- Produces: `build_compose_argv(request: PostproductionRequest) -> list[str]`。
- Produces: `execute_compose(request: PostproductionRequest, *, run=subprocess.run) -> AdapterResult`。
- Produces: `build_encode_argv(input_path: Path, output_path: Path, aspect_ratio: str) -> list[str]`。

- [ ] **Step 1: Write failing argv and validation tests**

断言 16:9 输出 `1920x1080`、9:16 输出 `1080x1920`、scale+pad 不拉伸、H.264/AAC、24fps、音轨缺失时补静音、字幕路径作为单独 argv 元素、中文/空格/特殊字符不拆词、空输入拒绝。

- [ ] **Step 2: Write a local FFmpeg integration test**

测试用 FFmpeg lavfi 生成两个 1 秒彩色视频（一个有音频、一个无音频），合成后用现有 `probe_media()` 验证分辨率、H.264、AAC、音轨和约 2 秒时长；若测试环境明确没有 FFmpeg，使用 pytest skip 并在人工验收补测。

- [ ] **Step 3: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_postproduction.py -q`

Expected: FAIL，后期模块不存在。

- [ ] **Step 4: Implement filter graph and safe execution**

采用单次 `filter_complex` 统一每段画幅、像素格式、帧率和音轨后 concat；字幕存在时在最终视频链路使用本地 `subtitles` 滤镜。捕获超时/OSError/非零退出，只返回受控错误，不写入 stderr 原文。

- [ ] **Step 5: Run unit and integration tests**

Expected: 全部 PASS 或仅在 FFmpeg 明确缺失时集成测试 SKIP。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/postproduction.py src/video_workstation/qc.py tests/test_postproduction.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: compose local delivery videos"
```

### Task 3: 后期任务入队、Worker 分派和资产登记

**Files:**
- Create: `src/video_workstation/services/postproduction.py`
- Modify: `src/video_workstation/worker.py`
- Modify: `src/video_workstation/main.py`
- Modify: `tests/test_operations.py`
- Modify: `tests/test_review_fixes.py`

**Interfaces:**
- Produces: `create_compose_task(session: Session, actor: User, project: Project, *, asset_ids: list[str], subtitle_asset_id: str | None, aspect_ratio: str, priority: int, settings: Settings) -> Task`。
- Produces: `create_encode_task(session: Session, actor: User, project: Project, *, source_asset_id: str, aspect_ratio: str, priority: int, settings: Settings) -> Task`。
- Consumes: Task 2 的 `execute_compose()` 和资产计划的 `register_video_asset()`。
- Produces: `POST /api/projects/{project_id}/compose`，仅管理员。

- [ ] **Step 1: Write failing service and worker tests**

验证输入资产必须同项目、kind=video、QC passed、每个已批准镜头都有选择且按 sequence 排序；创建 `compose_project` 任务；Worker 不要求 model_profile；成功登记 `kind=composite`；失败遵循现有重试；旧租约不能覆盖结果。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_operations.py tests\test_review_fixes.py -q`

Expected: FAIL，创建服务或 Worker 分派不存在。

- [ ] **Step 3: Implement preflight and task payload snapshots**

payload 只保存资产 ID、目标画幅、字幕资产 ID、系统输出路径和受控交付 Profile；创建时计算预计临时空间并调用 `StorageGuard`。不把客户端路径写入任务。

- [ ] **Step 4: Dispatch by task_type before model-profile validation**

`generate` 保持现有适配器流程；`compose_project|encode_delivery` 调用后期执行器；未知 task_type 以基础设施错误终止。所有分支复用租约、心跳、重试、QC 和资产登记规则。

- [ ] **Step 5: Run focused and full tests**

Expected: 全部 PASS，既有生成任务测试不回退。

- [ ] **Step 6: Update feedback and commit**

```powershell
git add src/video_workstation/services/postproduction.py src/video_workstation/worker.py src/video_workstation/main.py tests/test_operations.py tests/test_review_fixes.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: queue local postproduction tasks"
```

### Task 4: 合成界面和 Video2X 准入入口

**Files:**
- Modify: `src/video_workstation/services/models.py`
- Modify: `src/video_workstation/postproduction.py`
- Modify: `src/video_workstation/worker.py`
- Modify: `src/video_workstation/main.py`
- Modify: `src/video_workstation/templates/project.html`
- Modify: `src/video_workstation/templates/models.html`
- Modify: `src/video_workstation/static/app.js`
- Modify: `src/video_workstation/static/app.css`
- Modify: `tests/test_operations.py`
- Modify: `tests/test_model_profiles.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `video2x_is_admitted(profile: ModelProfile, *, scale: int = 2) -> bool`。
- Produces: `execute_video2x(profile: ModelProfile, input_path: Path, output_path: Path, *, scale: int = 2) -> AdapterResult`，只允许 `{input}`、`{output}`、`{scale}` 三个占位符。
- Produces: 管理员项目页“合成成片”表单及 `upscale` 默认关闭控件。

- [ ] **Step 1: Write failing UI and admission tests**

验证全部镜头有合格资产才启用合成；缺失镜头列出名称；字幕下拉只显示同项目字幕；默认目标画幅来自分镜；Video2X 未配置/无准入/停用时禁用且有原因；普通成员不可提交。

- [ ] **Step 2: Run tests and confirm failure**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests\test_operations.py tests\test_model_profiles.py -q`

Expected: FAIL，界面和准入函数不存在。

- [ ] **Step 3: Add server-rendered composition controls**

首版每个镜头自动选最新合格视频，管理员可改为历史资产；提交前页面显示输入数量、目标画幅、字幕、预计输出和缺失项。

- [ ] **Step 4: Add gated upscale follow-up task**

只有 Video2X Profile 同时已配置、启用且含 `scale=2` 的当前配置准入记录时，合成任务成功后才创建 `upscale` 后续任务；否则不创建，不能静默回退。Worker 的 `upscale` 分支通过 `execute_video2x()` 运行固定命令、执行媒体质检并登记 `kind=upscaled`，未知占位符或外部 URL 配置必须拒绝。

- [ ] **Step 5: Full verification and browser acceptance**

Run: `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest -q`

Run: `.\.venv\Scripts\python.exe -m compileall -q src`

Run: `git diff --check`

人工使用两个本地短样片和一个 SRT 验证 16:9、9:16、静音轨、字幕、播放器、下载、归档追溯；确认 Video2X 在当前机器保持禁用。

- [ ] **Step 6: Update operations docs and feedback**

README 写清 FFmpeg 安装检查、合成使用方式、Video2X 准入和当前非 A6000 边界；`.feedback` 记录所有工具失败与模糊点。

- [ ] **Step 7: Commit**

```powershell
git add src/video_workstation/services/models.py src/video_workstation/postproduction.py src/video_workstation/worker.py src/video_workstation/main.py src/video_workstation/templates/project.html src/video_workstation/templates/models.html src/video_workstation/static/app.js src/video_workstation/static/app.css tests/test_operations.py tests/test_model_profiles.py README.md .feedback/2026-10-08-agent-experience.md
git commit -m "feat: add local delivery workflow"
```
