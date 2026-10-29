# 本地工作台温度保护与启动入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有本地工作台中持续显示 GPU 温度，并在监测失联或持续高温时阻止新重型任务；统一启动入口负责启动监测、网页和 Worker。

**Architecture:** 独立监测进程每五秒读取一次 GPU，原子写入 G 盘数据目录的状态文件。Web 只读取状态并提供恢复操作；Worker 认领前检查同一文件。温度保护与作品暂停保持分离。由于现有 ComfyUI 适配器没有可证实的定向取消能力，本次紧急高温只锁住新任务并发出明确警报，不伪称已经停止当前镜头。

**Tech Stack:** Python, FastAPI, PowerShell, nvidia-smi, Jinja2, pytest。

**Spec:** `docs/本地视频工作台产品设计方案20261028.md` 第 2.2、2.3、15 章。

## Global Constraints

- 仅使用本机服务和文件；不请求云端，不改变模型档位和已确认文案。
- 预警连续 30 秒达到 80℃；降温保护连续 30 秒达到 85℃；紧急连续 10 秒达到 90℃或单次达到 93℃；失联 15 秒；恢复低于 75℃连续 120 秒后由使用者手动继续。
- 监测与 Worker 分进程；没有温度读数不得显示 0℃或沿用过期数值冒充实时值。
- 未实现定向停止时明确显示“当前任务需人工处理”，不杀所有 Python 进程。
- 保留已存在的团队权限和旧 API；本轮不迁移账号与数据库。

## Review Focus

1. 状态文件不存在、损坏或超过 15 秒：阻止新重型任务并显示失联。
2. 单个高温尖峰：除 93℃紧急阈值外不触发持续预警。
3. 低温短暂回落：不得解除已锁定状态；需低于 75℃持续 120 秒且手动确认。
4. 正在运行的任务：保护仅阻止新认领，不能报称已停止当前任务。
5. 重复打开启动入口：不重复启动监测、Web、Worker。

---

### Task 1: 独立温度状态机与监测进程

**Files:** Create `src/video_workstation/thermal.py`; modify `src/video_workstation/cli.py`, `src/video_workstation/config.py`; test `tests/test_thermal.py`.

**Interfaces:** `ThermalState.sample(result, now)` 更新等级；`read_thermal_status(path, now)` 判定新鲜度；`run_thermal_monitor(settings, interval=5)` 持续采样并原子落盘。

- [x] 先写 80/85/90/93、失联、恢复边界、损坏文件测试并运行确认失败。
- [x] 实现纯状态机、原子状态持久化和 `cli monitor-temperature`。
- [x] 运行 `tests/test_thermal.py`，确认全部通过。

### Task 2: Worker 派发门禁与网页提示

**Files:** Modify `src/video_workstation/worker.py`, `src/video_workstation/main.py`, `src/video_workstation/templates/base.html`, `src/video_workstation/templates/system.html`, `src/video_workstation/static/app.js`, `src/video_workstation/static/app.css`; test `tests/test_thermal.py`, `tests/test_system_routes.py`.

**Interfaces:** `thermal_dispatch_allowed(settings, now)`；`GET /api/thermal/status`；`POST /api/thermal/resume`，均需登录，后者需 CSRF。

- [x] 写 Worker 锁定不认领、普通编辑可用、网页状态/恢复权限测试并运行确认失败。
- [x] Worker 在 `advance_creations` 和认领之间读取锁；全局顶栏定时读取状态；运行状态页显示历史和人工恢复入口。
- [x] 运行对应测试并检查无 JS/CSS 破坏。

### Task 3: 启动与真实集成检查

**Files:** Modify `scripts/start_local_workstation.ps1`, root `启动视频自动化工作站.ps1`; test `tests/test_operations.py`; document `docs/operations/thermal-monitor.md`.

- [x] PowerShell语法和重复启动实测，确认监测由统一入口启动并避免重复进程。
- [x] 更新启动脚本，核验服务能力、避免重复进程；维护 Hidden 窗口和现有日志位置。
- [x] 跑完整 pytest、PowerShell 语法检查、真实本机温度读取；只在队列空闲时重启现有服务。

## 执行记录

2026-10-29：基线164项通过；接入温度监测后曾达到170项通过，审查修复后增加失联宽限、损坏状态拒绝、监测进程锁、启动互斥与旧Worker核验，并再跑针对性测试。真实本机采样63–64℃，启动后`/health`表明温度门禁启用，温度状态正常且Worker标记为启用。定向停止、桌面通知、长期分层记录仍延期，不能把此计划当作完整产品验收。

独立复查进一步修复预警状态映射、损坏持久化状态安全重建、旧 Worker 切换时队列写锁与目标进程归属核验。随后营销调度模块一并完成，最终完整测试达到187项通过；网页、Worker 和监测进程在本机均报告运行版本5。

## 后续独立模块

完整设计另分为模型正式准入与专用环境、声音字幕及局部返工、角色与列表剪辑、作品级营销插队和归档导出。各模块应有独立计划与验收。本计划交付温度保护的第一可用版本，不宣称完成全部 15 章。
