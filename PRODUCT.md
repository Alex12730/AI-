# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

用户已指定：Windows 11 + WSL2 Ubuntu，FastAPI、SQLite WAL、SQLAlchemy/Alembic、Jinja2 与少量原生 JavaScript。部署目标是单张 RTX A6000 48GB 的办公局域网工作站。

## Users

- 普通成员：创建项目、编辑分镜、提交审批、生成自己有权限访问的镜头、查看结果并提出返工。
- 管理员：审批分镜、调整任务优先级、验收成片、管理模型准入档位、成员账号与系统健康。

## Product Purpose

把“脚本 → 分镜确认 → 镜头生成 → 配音字幕 → 合成质检 → 成片归档”收敛为一套可追溯、可恢复的纯本地视频生产流程。成功意味着 Web 服务与 GPU Worker 解耦、重启不丢任务、重型任务严格串行、每条成片能够回溯到模型、参数、素材、随机种子和审批记录。

## Positioning

系统不是通用模型聊天界面，而是围绕单 GPU、本地开放权重模型和人工审批门槛设计的生产调度台；产品界面演示必须来自真实录屏或截图动效，不能用生成模型伪造。

## Operating Context

平台在办公局域网内使用。活跃数据位于 WSL2 文件系统和 NVMe；完成验收的项目包归档到机械盘。Qwen、Wan 2.2、MiniMax H3、LTX-2.3、CosyVoice、MuseTalk、Video2X 与 FFmpeg 均通过本地适配器接入，首次下载完成后可离线运行。

## Capabilities and Constraints

- 仅本地执行，不接入或预留云端视频生成器。
- 单张 RTX A6000 48GB；同一时间只允许一个重型 GPU 任务。
- MiniMax H3 MVP 仅启用 FL2VA；时长档必须经连续基准测试后开放。
- H3 固定文案口播不作为默认能力；精确口播使用 CosyVoice 3 + MuseTalk 1.5。
- 普通成员不得修改系统配置或访问无权限项目。
- 低磁盘、连续 OOM、模型健康异常必须显式阻断，不得偷偷降低参数。

## Evidence on Hand

当前没有真实模型权重、硬件基准结果、品牌素材或成片样本。界面不得捏造吞吐、成功率或模型可用性；未验证档位必须明确标记并隐藏生产入口。

## Product Principles

1. 人工确认先于昂贵生成。
2. 真实产品素材优先于生成画面。
3. 可恢复、可追溯先于吞吐量。
4. 参数只按实测开放，不以宣传规格代替本机证据。
5. 安全默认值与明确失败优先于隐式降级。

## Accessibility & Inclusion

Web 界面需支持键盘焦点、清晰状态文本、颜色之外的状态表达，并兼容桌面和窄屏办公设备。
