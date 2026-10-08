# Shot Duration and Aspect Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在分镜草稿阶段逐镜头选择 5/10/15/20 秒和 16:9/9:16，待审批时可退回修改，批准后只用已保存参数匹配并入队已验证模型。

**Architecture:** 把镜头更新和待审批撤回收敛到 `services/projects.py` 的领域函数，HTML 表单与现有 JSON API共用同一状态和权限校验。项目页由服务端计算每个镜头可用的模型 Profile，入队 API再次核对请求参数与已审批镜头一致；Demo 仅扩展为八种流程测试预设，不代表真实模型能力。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、Jinja2、原生 JavaScript、Pytest。

**Spec:** `docs/superpowers/specs/2026-10-08-shot-duration-aspect-controls-design.md`

## Global Constraints

- 草稿时长只能是 5、10、15、20 秒；画面比例只能是 16:9、9:16。
- `pending_approval` 和 `approved` 镜头不可编辑；已批准分镜不能撤回。
- 普通成员只可撤回自己创建项目的待审批分镜；管理员可撤回其有权访问的待审批分镜。
- 入队参数必须与已审批镜头保存值完全一致，且模型 Profile 必须精确匹配已验证预设。
- Demo 八种组合只生成 JSON 演示清单；真实 H3/LTX 档位仍需 A6000 十次基准。
- 不加入分辨率选择，不下载模型，不修改数据库结构。

## Review Focus

- 浮点或字符串形式的 5/10/15/20 秒必须规范化后验证，其他值拒绝且不改变镜头。
- 项目协作者可编辑草稿但不可撤回非本人创建项目，管理员行为保持可审计。
- 手工篡改入队 JSON 的时长或比例必须返回 409，不能绕过审批锁定值。
- 已存在数据库中的 Demo Profile 必须补齐八个预设，同时保留管理员设置的启停状态。
- 产品 UI 镜头仍只能选择 Demo/真实录屏路径，不能因为 Profile 匹配而开放生成模型。

---

### Task 1: 分镜参数与撤回领域规则

**Files:**
- Modify: `src/video_workstation/services/projects.py:13-170`
- Modify: `src/video_workstation/main.py:24-33,488-519`
- Test: `tests/test_auth_and_projects.py`
- Test: `tests/test_operations.py`

**Interfaces:**
- Produces: `SHOT_DURATION_PRESETS: tuple[int, ...]`、`SHOT_ASPECT_RATIOS: tuple[str, ...]`。
- Produces: `update_draft_shot(session: Session, actor: User, shot: Shot, *, title: str, prompt: str, duration_seconds: float, aspect_ratio: str) -> Shot`。
- Produces: `withdraw_storyboard(session: Session, actor: User, storyboard: Storyboard) -> Storyboard`。

- [x] **Step 1: 写失败测试**

增加测试，断言四个时长和两个比例可保存并写入 `shot.update` 审计；6 秒、非法比例及非草稿状态被拒绝且数据不变。增加撤回测试，断言创建者/管理员成功、协作者失败、批准后失败，并核对项目/分镜/镜头状态、`submitted_at` 和 `storyboard.withdraw` 审计。

- [x] **Step 2: 验证测试按预期失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_auth_and_projects.py tests\test_operations.py -q`

Expected: FAIL，因为领域函数和撤回行为尚不存在，6 秒仍会被现有 API接受。

- [x] **Step 3: 实现领域函数并让现有 JSON 更新 API复用它**

在 `services/projects.py` 实现上述常量和函数；状态或输入错误使用 `ValueError`，项目权限错误使用 `PermissionDenied`。修改 `api_update_shot` 只负责加载实体与调用领域函数，保持原响应结构。

- [x] **Step 4: 运行目标测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_auth_and_projects.py tests\test_operations.py -q`

Expected: PASS。

- [x] **Step 5: 更新 `.feedback` 并提交领域规则**

记录本任务的测试和摩擦点后提交：`feat: add editable shot preset rules`。

### Task 2: 模型匹配与审批参数锁定

**Files:**
- Modify: `src/video_workstation/services/models.py:18-121,198-216`
- Modify: `src/video_workstation/main.py:319-343,424-486`
- Test: `tests/test_model_profiles.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `matching_profiles_for_shot(shot: Shot, profiles: Iterable[ModelProfile]) -> list[ModelProfile]`，按显示名称稳定排序，只返回同时通过场景约束和精确预设准入的 Profile。
- Consumes: Task 1 保存并锁定的 `Shot.duration_seconds` 与 `Shot.aspect_ratio`。

- [ ] **Step 1: 写失败测试**

增加测试，断言新建和既有 Demo Profile 都获得 5/10/15/20 × 16:9/9:16 八个带当前配置指纹的预设，且重复 seed 不恢复被管理员停用的状态。增加匹配测试覆盖禁用、未验证、错误组合及 product_ui；增加 API测试，断言篡改已审批镜头参数返回 409，正确参数进入任务快照。

- [ ] **Step 2: 验证测试按预期失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_model_profiles.py tests\test_api.py -q`

Expected: FAIL，因为 Demo 目前只有两个 5 秒预设，且入队 API未比对镜头保存值。

- [ ] **Step 3: 实现 Demo 预设同步、Profile 匹配和入队一致性校验**

更新 `PROFILE_DEFINITIONS` 与 `seed_model_profiles`，只同步 Demo 的能力预设和指纹，不覆盖 `enabled`。实现 `matching_profiles_for_shot`，复用 `validate_model_for_shot` 和 `admit_generation`。在 `api_enqueue` 校验请求时长/比例与镜头值一致，并让项目页传入按镜头 ID组织的可用 Profile 映射。

- [ ] **Step 4: 运行目标测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_model_profiles.py tests\test_api.py -q`

Expected: PASS。

- [ ] **Step 5: 更新 `.feedback` 并提交模型匹配**

记录本任务的测试和摩擦点后提交：`feat: match models to approved shot presets`。

### Task 3: 服务端页面表单与待审批撤回

**Files:**
- Modify: `src/video_workstation/main.py:319-367`
- Modify: `src/video_workstation/templates/project.html`
- Modify: `src/video_workstation/static/app.css`
- Modify: `src/video_workstation/static/app.js`
- Test: `tests/test_operations.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 1 的 `update_draft_shot`、`withdraw_storyboard` 和预设常量。
- Consumes: Task 2 的 `available_profiles_by_shot: dict[str, list[ModelProfile]]`。
- Produces: `POST /shots/{shot_id}/settings` 与 `POST /storyboards/{storyboard_id}/withdraw` 两个带 CSRF 的 HTML 表单端点。

- [ ] **Step 1: 写失败的页面与表单测试**

断言草稿页包含每镜头时长/比例下拉框和保存按钮；设置表单保存后 303 返回项目页。断言待审批页显示“退回修改”，撤回后恢复草稿控件。断言批准页只列出匹配 Profile，表单使用镜头保存值；无匹配模型显示“暂无已验证模型”。

- [ ] **Step 2: 验证测试按预期失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_operations.py tests\test_api.py -q`

Expected: FAIL，因为页面仍只显示文本并硬编码 Demo 参数。

- [ ] **Step 3: 实现路由、模板和最小样式**

新增两个 HTML POST 路由并复用领域函数。草稿镜头使用原生 `<select>` 和提交按钮；待审批页按权限显示撤回按钮；批准页显示模型下拉框并把保存值放进入队表单。保留现有 JS入队反馈，仅适配模型 `<select>`；CSS只增加紧凑镜头设置布局和移动端单列规则。

- [ ] **Step 4: 运行目标测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_operations.py tests\test_api.py -q`

Expected: PASS。

- [ ] **Step 5: 更新 `.feedback` 并提交页面功能**

记录本任务的测试和摩擦点后提交：`feat: add shot preset controls`。

### Task 4: 完整验证、反馈与运行服务更新

**Files:**
- Modify: `.feedback/2026-10-08-agent-experience.md`
- Modify: `README.md`（仅在使用说明需要补充分镜参数时）
- Test: `tests/`

**Interfaces:**
- Consumes: Tasks 1–3 的完整功能。
- Produces: 可在当前本地数据库与浏览器中使用的新版本服务。

- [ ] **Step 1: 运行完整自动验证**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: 全部通过，0 failures。

Run: `.\.venv\Scripts\python.exe -m compileall -q src`

Expected: exit 0。

Run: `git diff --check`

Expected: 无错误。

- [ ] **Step 2: 更新反馈并提交实现**

在 `.feedback` 记录测试、工具摩擦和改进建议；审查暂存内容不含数据库、密码、Cookie、会话密钥或模型权重，再提交功能代码和计划。

- [ ] **Step 3: 精确重启 Web服务并验收当前项目**

只停止监听 `127.0.0.1:8000` 的已核实 Python Web进程，使用项目虚拟环境重新启动。读取 `/health`、当前项目页和数据库，确认待审批项目可退回、草稿可保存 20 秒/9:16，且批准后的入队参数与保存值一致。
