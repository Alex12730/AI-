# 简易制作与整片编排 Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: superpowers:executing-plans. 本轮由当前会话直接执行；用户已确认设计并要求开始修改代码。

Goal: 交付第一批可用代码：六种确定性模板、按镜头自动匹配、个人确认、串行生成与自动合成、可读的进度与预览。

Architecture: 复用 FastAPI/Jinja/SQLite/现有 Task 和 Worker。新增持久化 CreationPlan 保存制作选项、冻结的输入和明确的任务/素材 ID；Worker 在领取任务前推进计划，不增加第二套执行器。

Tech Stack: Python, SQLAlchemy, Alembic, Jinja2, FFmpeg。

Spec: docs/superpowers/specs/2026-10-22-personal-offline-video-workstation-design.md

## Global Constraints

- 保留旧项目、团队审批、鉴权和 CSRF。个人确认只限由向导创建且本人拥有的项目。
- 只选择三个真实视频模型，必须满足当前配置的准入档位；不以 Demo、云端或改变时长兜底。
- 本批支持已有视频绑定；图片、录音、离线配音、参考帧及高级版本编辑仍须后续任务，界面明确说明。
- 不自动运行 60 次正式基准，不覆盖已有权重，不改账号、不推送 GitHub。
- 专用 Python 迁移、安装闭包、运行中定向取消、备份恢复及真实断网验收属于后续独立里程碑，本批不得宣称全套设计完成。

## Review Focus

1. Demo 或未准入配置被当成真实生成：任务 1 的路由测试。
2. 个人确认导致成员越权：任务 2、4 的所有者与 CSRF 测试。
3. 重复点击或 Worker 重启重复生成/合成：任务 2、3 的幂等性与重开会话测试。
4. 真实软件画面误交模型：任务 1、4 的素材门禁测试。
5. 暂停/失败仍偷偷进入后续镜头：任务 3 状态测试。

## Task 1: 模板与解释性路由

Files: 新增 services/creation.py、tests/test_creation.py。
Interfaces: `RECIPES`, `create_guided_project(session, actor, *, name, script, recipe, target_seconds, aspect_ratio)`；`recommend_shot(shot, profiles)` 返回来源、模型、理由、阻塞原因。

- [x] 先写六流程、目标时长、输入验证、真实界面、声音候选与未准入排除测试。
- [x] 运行 pytest tests/test_creation.py，确认缺少入口失败。
- [x] 实现规则模板与路由；五秒为镜头单位，整片目标必须为五秒倍数，不静默改变。
- [x] 重跑该文件，预期全部通过。

## Task 2: 持久化制作计划与个人确认

Files: models.py、migration 0005、services/creation.py、tests/test_creation.py。
Interfaces: `CreationPlan`；`start_creation(session, actor, plan, settings)`；冻结 shots/model fingerprints/assets，生成任务使用现有 payload 格式。

- [x] 写未满足条件不审批、本人确认、成员越权和重复提交测试，运行确认失败。
- [x] 增加表与迁移，事务中检查所有输入后再确认/批准并创建任务。
- [x] 运行服务测试和迁移测试，预期通过。

## Task 3: 复用 Worker 自动推进和合成

Files: services/creation.py、worker.py、tests/test_creation.py。
Interfaces: `advance_creations(session, settings)`；每次只派发一个确定的节点，已成功生成按 task_id 取得资产；合成显式使用被冻结的资产 ID。

- [x] 写顺序执行、失败停止、暂停边界、重复推进/重开会话测试，确认失败。
- [x] 在 Worker 取任务前推进计划，补仅作用于个人计划的合成授权，不放开旧合成接口权限。
- [x] 跑服务测试和完整 pytest，预期通过。

## Task 4: 简易界面与端到端测试

Files: main.py、templates/create.html、templates/creation.html、base.html、app.css、tests/test_creation_api.py。
Interfaces: `/create`, `/creations/{id}` 及该页面的 CSRF 表单；保持 `/` 原管理页兼容。

- [x] 写新建→确认、未准入门禁、非本人/跨项目访问、CSRF 的 HTTP 测试，确认失败。
- [x] 实现页面、真实视频上传与绑定、状态、暂停后续、恢复及成片播放器/下载。
- [x] 运行完整 pytest，真实浏览器检查宽屏/窄屏、键盘和错误状态。
- [x] 记录实现范围、实测与未验收项，不把软件测试代替模型/离线验收。

## 后续独立里程碑

专用运行环境及桌面入口；正式档位准备向导与 AV 检查适用性；图片/声音/SRT 的简易素材管理；定向停止及旧 Comfy 任务恢复；镜头版本锁定与局部返工；一致性备份和断网验收。按完整设计继续实施。

## 执行记录

本批执行完成。初始基线 104 passed；最终完整命令（FFmpeg/FFprobe 已加入 PATH）`python -m pytest -q --tb=short`：121 passed，40 条已有依赖弃用提示，无跳过。真实媒体测试完成两个五秒视频→十秒 1920×1080 H.264/AAC 成片。数据迁移同时覆盖空库、实际旧库升级；升级前使用 SQLite backup 保存本地备份。

浏览器：独立 QA 库和临时账号，1280×720 开始页、390×844 确认页已截图；390 视口的文档宽度为 390；该页面 externalResources=[]，控制台 0 错误。此检查不能替代完整断网验收。

审查：独立 reviewer 发现草稿修改可能并发覆盖确认快照。上传/绑定/编辑统一获取写锁并刷新后重新检查状态；两会话回归测试与损坏快照隔离测试均先失败后通过。复核无 Critical/Important 发现。队列认领另外增加原子全局运行状态复查，并通过过期查询回归测试。

Ruling: 沿用当前已隔离于 main 的开发分支和现有安装路径，不额外迁移工作目录，以便继续复用模型及启动脚本。用户明确要求开始编码，按已确认设计直接执行当前会话计划，不再次要求审批相同设计。此批不是完整设计的全部实现。

Ruling: 个人自动合成由已经冻结并审批的 CreationPlan 内部生成 Task，不放开原 admin-only 合成 API；端到端测试确认普通成员原接口仍返回 403。

Ruling: 暂停采用当前已入队段落结束后暂停，与现有运行器能力保持一致；定向取消和旧 Comfy 任务恢复不在本批承诺范围。

变更说明与后续项：docs/operations/guided-production-first-release.md。真实生成的生产档位未自动验收，未推送 GitHub。
