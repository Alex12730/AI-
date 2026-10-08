# 批量镜头设置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在草稿分镜中一次把全部镜头设置为相同的 5/10/15/20 秒和 16:9/9:16，并继续允许单镜头覆盖。

**Architecture:** 在现有项目服务层增加单事务批量更新函数，统一执行权限、状态和预设校验并只写一条审计记录。FastAPI 服务端表单调用该函数，Jinja2 项目页仅在草稿状态显示批量控件；现有单镜头编辑、审批和模型准入流程保持不变。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、SQLite、Jinja2、原生 CSS、pytest

**Spec:** `docs/superpowers/specs/2026-10-08-bulk-shot-settings-design.md`

## Global Constraints

- 只批量修改现有 `Shot.duration_seconds` 和 `Shot.aspect_ratio`，不增加数据库迁移。
- 时长只允许 `5`、`10`、`15`、`20`；画面比例只允许 `16:9`、`9:16`。
- 仅 `draft` 分镜可批量修改；待审批和已批准状态只读。
- 一次请求更新全部镜头，并只写一条 `storyboard.shots.bulk_update` 审计记录。
- 非法参数、无权限或错误状态必须在修改任何镜头前失败。
- 批量设置后保留现有单镜头设置能力。
- 复用现有 CSRF、项目访问和事务机制，不引入 JavaScript 或新依赖。
- 不修改与本功能无关的项目创建重定向逻辑。

## Review Focus

- 分镜在请求到达时已经进入待审批或已批准状态：拒绝并保持所有镜头原值（Task 1 测试）。
- 非法时长或画幅在多镜头项目中提交：拒绝且不能留下部分更新（Task 1 测试）。
- 项目外用户构造批量请求：返回 403，不能修改镜头或写审计（Task 1 与 Task 2 测试）。
- 空分镜提交合法预设：安全返回更新数 0，且不创建镜头（Task 1 测试）。
- 批量设置后再单独改一个镜头：只覆盖目标镜头，其余镜头维持批量值（Task 2 测试）。

---

### Task 1: 服务层原子批量更新

**Files:**
- Modify: `tests/test_auth_and_projects.py`
- Modify: `src/video_workstation/services/projects.py`
- Modify: `.feedback/2026-10-08-agent-experience.md`

**Interfaces:**
- Consumes: `assert_project_access(actor: User, project: Project)`, `SHOT_DURATION_PRESETS`, `SHOT_ASPECT_RATIOS` 和现有 SQLAlchemy 会话事务。
- Produces: `bulk_update_draft_shots(session: Session, actor: User, storyboard: Storyboard, *, duration_seconds: float, aspect_ratio: str) -> int`；返回实际更新的镜头数。

- [ ] **Step 1: 写服务层失败测试**

在 `tests/test_auth_and_projects.py` 新增：

- `test_bulk_update_draft_shots_updates_all_and_writes_one_audit`：创建 3 个镜头，调用函数后断言全部为 `15` 和 `9:16`、返回 `3`、只存在一条动作名为 `storyboard.shots.bulk_update` 的审计，详情精确包含 `duration_seconds`、`aspect_ratio`、`shot_count`。
- `test_bulk_update_draft_shots_rejects_invalid_or_locked_without_partial_changes`：分别提交时长 `6`、画幅 `1:1` 和待审批分镜，断言抛出对应 `ValueError`，每次全部镜头仍保持调用前值。
- `test_bulk_update_draft_shots_enforces_access_and_accepts_empty_storyboard`：项目外用户触发 `PermissionDenied` 且无审计；清空合法草稿分镜的镜头后提交合法预设，断言返回 `0`、不创建镜头并写入 `shot_count: 0` 的单条审计。

- [ ] **Step 2: 运行测试确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_auth_and_projects.py -k "bulk_update_draft_shots" -q`

Expected: 因 `bulk_update_draft_shots` 尚不存在而失败。

- [ ] **Step 3: 实现最小服务函数**

在 `src/video_workstation/services/projects.py` 新增精确签名：

```python
def bulk_update_draft_shots(
    session: Session,
    actor: User,
    storyboard: Storyboard,
    *,
    duration_seconds: float,
    aspect_ratio: str,
) -> int:
```

按“项目访问 → 草稿状态 → 时长 → 画幅”的顺序完成全部前置校验，再遍历镜头赋值；最后只添加一条 `AuditLog`，`entity_type="storyboard"`、`entity_id=storyboard.id`，详情为目标值和镜头数，并返回镜头数。复用现有中文错误文案，不提交事务。

- [ ] **Step 4: 运行服务层测试确认 GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_auth_and_projects.py -q`

Expected: `tests/test_auth_and_projects.py` 全部通过。

- [ ] **Step 5: 记录反馈并提交 Task 1**

在 `.feedback/2026-10-08-agent-experience.md` 记录本任务测试、摩擦点和改进建议，然后提交：

```powershell
git add -- src/video_workstation/services/projects.py tests/test_auth_and_projects.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: add bulk shot settings service"
```

---

### Task 2: 草稿页批量表单与路由

**Files:**
- Modify: `tests/test_operations.py`
- Modify: `src/video_workstation/main.py`
- Modify: `src/video_workstation/templates/project.html`
- Modify: `src/video_workstation/static/app.css`
- Modify: `.feedback/2026-10-08-agent-experience.md`

**Interfaces:**
- Consumes: Task 1 的 `bulk_update_draft_shots(...) -> int`、现有 `validate_csrf`、`html_user`、`duration_presets`、`aspect_ratios` 和单镜头 `/shots/{shot_id}/settings` 路由。
- Produces: `POST /storyboards/{storyboard_id}/shot-settings`；草稿页 `.bulk-shot-settings` 表单；成功后 `303` 跳转至 `/projects/{project_id}`。

- [ ] **Step 1: 写页面和路由失败测试**

在 `tests/test_operations.py` 新增或拆分为以下测试：

- `test_draft_page_bulk_updates_all_shots_then_allows_single_override`：用 3 句脚本创建草稿，断言页面包含“统一设置全部镜头”、表单 action 和提示“应用后仍可单独修改某个镜头”；POST `15`、`9:16` 后断言 `303` 和正确 location，再读取项目 API 确认 3 个镜头已统一；调用现有单镜头表单把第一个镜头改为 `5`、`16:9`，断言仅第一个变化。
- `test_bulk_shot_settings_route_rejects_csrf_invalid_values_and_outsider`：缺少 CSRF 返回 403；合法 CSRF 但时长 `6` 返回 409 且全部镜头不变；项目外成员使用自己的合法 CSRF 返回 403 且全部镜头不变。
- `test_bulk_shot_settings_form_is_hidden_after_submission`：提交审批后页面不再包含批量表单 action，但仍显示只读镜头参数。

- [ ] **Step 2: 运行测试确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_operations.py -k "bulk_shot_settings or bulk_updates_all" -q`

Expected: 因批量路由或页面控件尚不存在而失败。

- [ ] **Step 3: 加载 Impeccable 上下文并读取前端质量规则**

Run: `C:\Users\Administrator\.codex\plugins\cache\openai-curated-remote\impeccable\4.3.1\skills\impeccable\scripts\impeccable.cmd context --target src/video_workstation/templates/project.html`

Expected: 输出当前 Operate 界面的产品和设计约束；若启动器不可用，按 skill 规则读取现有 `PRODUCT.md`、`DESIGN.md`。随后完整读取 `reference/craft-floor.md`，再编辑任何 UI 文件。

- [ ] **Step 4: 实现表单路由**

在 `src/video_workstation/main.py` 导入 `bulk_update_draft_shots`，新增 `POST /storyboards/{storyboard_id}/shot-settings`：复用现有登录、CSRF 和 404 处理，调用服务函数；`ValueError` 转为 409，成功后 `303` 返回项目页。`PermissionDenied` 继续交给全局 403 处理器。

- [ ] **Step 5: 实现草稿页批量控件和响应式样式**

在 `src/video_workstation/templates/project.html` 的“镜头清单”标题与 `.shot-list` 之间，仅对 `draft` 渲染 `.bulk-shot-settings` 表单：两个带可见 label 的下拉框、CSRF 隐藏字段、“应用到全部镜头”按钮和“应用后仍可单独修改某个镜头。”提示。

在 `src/video_workstation/static/app.css` 复用现有表单、边框、间距和 44px 控件规则；桌面端单行排列，`680px` 以下单列，不能造成横向溢出。不要修改现有单镜头表单行为。

- [ ] **Step 6: 运行页面测试确认 GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_operations.py -q`

Expected: `tests/test_operations.py` 全部通过。

- [ ] **Step 7: 运行全量验证**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: 全部测试通过；允许报告现有 Starlette/httpx 或 Alembic 弃用警告，但不得出现新的失败或错误。

Run: `.venv\Scripts\python.exe -m compileall -q src tests`

Expected: 退出码 0。

Run: `git diff --check`

Expected: 无空白错误。

- [ ] **Step 8: 记录反馈并提交 Task 2**

在 `.feedback/2026-10-08-agent-experience.md` 记录本任务测试、UI 检查和任何工具失败，然后提交：

```powershell
git add -- src/video_workstation/main.py src/video_workstation/templates/project.html src/video_workstation/static/app.css tests/test_operations.py .feedback/2026-10-08-agent-experience.md
git commit -m "feat: add bulk shot settings form"
```
