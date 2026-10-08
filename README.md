# AI 视频自动化工作站 MVP

一套面向单张 RTX A6000 48GB 的纯本地视频生产调度平台。它把项目、分镜审批、模型准入、单 GPU 队列、Worker 租约、结果追溯和基础质检组织在一起；不包含任何云端视频生成 API 客户端。

> 当前交付的是“可运行的软件平台 + 本地模型适配接口 + Demo 执行器 + 准入/运维工具”。Wan 2.2、MiniMax H3、LTX-2.3 等权重体积很大，必须在目标工作站单独下载并完成基准；仓库不会伪造 A6000 性能结果，也不会自动开放未验证档位。

## 已实现

- FastAPI + Jinja2 操作台、JSON API、Argon2 账号、成员/管理员权限。
- 项目 → 分镜草案 → 提交 → 管理员审批 → 逐镜头入队。
- 草稿镜头编辑、成员创建与项目授权、任务取消/重试/优先级、模型启停、管理员验收归档 API。
- SQLite WAL 持久化队列、同级 FIFO、等待老化、单重型任务、Worker 心跳/租约恢复。
- 基础设施错误自动重试一次；画质/OOM 不静默降级。
- Demo、Wan、MiniMax H3 FL2VA、LTX 本地命令适配边界，统一 `shell=False` 参数数组。
- H3 5/10/15 秒、16:9/9:16 独立十次基准门槛；无验证记录时 API 和界面均不开放。
- 100GB 固定余量 + 两倍临时空间检查、系统生成资产路径、ffprobe 结构/音轨/时长检查；验收后生成带 SHA-256 的追溯清单。
- MiniMax H3 许可证/NOTICE 元数据、纯本地 Profile 策略与阻断外部端点测试。
- 桌面与移动端操作台；移动端表格在容器内滚动，不造成整页横向溢出。

## 尚需目标工作站完成

- 安装真实模型权重和推理环境，填写各模型本地命令数组。
- H3、Wan、LTX、CosyVoice、MuseTalk 在 A6000 上逐项基准。
- FFmpeg 黑帧/异常静帧滤镜阈值按真实素材校准。
- 30–60 秒营销片、系列样片、72 小时混合任务和归档盘实测。

## 本地开发启动（Windows PowerShell）

```powershell
Set-Location D:\workspace\AI视频自动化工作流任务
.\scripts\setup.ps1
$env:VIDEO_WORKSTATION_DATA_DIR = "D:\ai-video-data"
$env:VIDEO_WORKSTATION_SESSION_SECRET = "请替换为至少24位随机字符串"
.\.venv\Scripts\video-workstation.exe init-db
.\.venv\Scripts\video-workstation.exe create-admin admin
.\scripts\run_web.ps1
```

另开一个终端：

```powershell
Set-Location D:\workspace\AI视频自动化工作流任务
$env:VIDEO_WORKSTATION_DATA_DIR = "D:\ai-video-data"
.\scripts\run_worker.ps1
```

浏览器访问 `http://127.0.0.1:8000`。需要局域网访问时，把 `run_web.ps1` 的 Host 改为指定办公网卡 IP，并用 Windows 防火墙仅允许批准的办公网段；不要直接监听公网。

平台代码会拒绝云端端点、敏感配置、Shell 和 `python -c/-m` 等动态执行入口，但这不能替代操作系统隔离。生产 Worker 必须由防火墙或 WSL 网络策略禁止公网出站，只放行回环地址和明确的内网依赖；首次下载权重结束后再进入离线生产模式。

## WSL2 部署

推荐将代码、SQLite 和活跃项目放在 WSL2 的 ext4 文件系统，例如 `/srv/ai-video-workstation`，不要把 SQLite 放在 `/mnt/c`。NVMe 存模型、缓存和活跃项目；机械盘只接收已经验收的归档包。完整步骤见 [WSL2 部署](docs/operations/wsl2-deployment.md)。

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
export VIDEO_WORKSTATION_DATA_DIR=/srv/ai-video-workstation/data
export VIDEO_WORKSTATION_SESSION_SECRET='至少24位随机字符串'
video-workstation init-db
video-workstation create-admin admin
uvicorn video_workstation.main:application --factory --host 127.0.0.1 --port 8000
```

## 模型接入

模型命令保存在数据库里，不写死在业务代码。命令 JSON 顶层必须是字符串数组；提示词、输出路径等通过白名单占位符替换后作为单独 argv 传递，不经过 Shell。

```powershell
.\.venv\Scripts\video-workstation.exe configure-model minimax-h3-fl2va `
  --command-json config\examples\h3-command.example.json `
  --version H3-Base `
  --quantization pruned-int8-ampere
```

可用占位符：`{task_id}`、`{prompt}`、`{negative_prompt}`、`{output}`、`{duration}`、`{aspect_ratio}`、`{seed}`、`{first_frame}`、`{last_frame}`。示例只是接口合同，必须按实际 ComfyUI 工作流/本地启动器修改路径。

### MiniMax H3

- MVP 仅使用 FL2VA；不安装 Ref2VA。
- 本地只承诺 H3-Base 768p 管线；不接入 Context-IR 或 Regenerate-2K API。
- 目标配置：ComfyUI 原生 H3 节点、pruned INT8 Transformer、Ampere 适配文本编码器、CPU offload，保留至少 4GB 显存余量。
- 对 5/10/15 秒与 16:9/9:16 分别生成 10 次，把结果保存为 JSON 后录入：

```powershell
.\.venv\Scripts\video-workstation.exe record-benchmark minimax-h3-fl2va 5 16:9 benchmark-runs.json
.\.venv\Scripts\video-workstation.exe list-models
```

只有“10 次、成功率至少 90%、无 OOM、无损坏文件”的组合会出现为已验证档。样例结构见 [benchmark-runs.example.json](config/examples/benchmark-runs.example.json)。安装与许可检查见 [MiniMax H3 本地接入](docs/operations/minimax-h3-local.md)。

## 目录与数据

```text
src/video_workstation/   应用、队列、Worker、模型适配器
migrations/              Alembic 初始迁移
config/examples/         本地命令与基准结果样例
docs/operations/         部署、硬件、模型和验收手册
scripts/                 安装、Web、Worker、预检脚本
data/                    默认运行数据（Git 忽略）
```

数据库、输出、模型权重、`.env` 和虚拟环境均在 `.gitignore` 中。不要把真实素材、密码、Cookie、许可证密钥或权重提交到仓库。

## 管理 API（MVP）

- `PATCH /api/shots/{id}`：仅草稿阶段编辑镜头。
- `POST /api/admin/users`、`POST /api/projects/{id}/members`：管理员创建成员并授权项目。
- `POST /api/tasks/{id}/pause|resume|cancel|retry`、`PATCH /api/tasks/{id}/priority`：任务控制；P0 和人工重试仅管理员。
- `PATCH /api/models/{slug}`：管理员启停本地模型 Profile。
- `POST /api/projects/{id}/accept`：全部任务成功后验收，写入 `Review`、`Asset` 和归档 `manifest.json`。

所有写接口都需要登录会话和 CSRF 请求头。Swagger 页面可查看字段，但实际调用应从登录后的同源管理界面或受控客户端发起。

## 验证

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src
.\scripts\preflight.ps1
```

硬件与成片验收不能由单元测试替代，按 [验收清单](docs/operations/acceptance-checklist.md) 逐项签字。系统设计规格在 [设计规格](docs/superpowers/specs/2026-10-08-ai-video-workstation-design.md)，实施记录在 [实施计划](docs/superpowers/plans/2026-10-08-ai-video-workstation-mvp.md)。

## 许可证提醒

应用代码的项目许可证尚未由项目所有者指定。第三方模型各自遵循其许可证。MiniMax H3 是开放权重但不是 Apache/MIT；部署时必须重新读取权重目录中的 LICENSE，并遵循界面归属、AI 生成标识、收入阈值与地域限制。仓库中的 [NOTICE](NOTICE.md) 仅是运维提醒，不是法律意见。
