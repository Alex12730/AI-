# MiniMax H3 本地接入

## 固定边界

- 只装 H3-Base FL2VA；MVP 不装 Ref2VA。
- 本地输出按 768 像素短边、24FPS、原生音频管线配置；这些是模型能力元数据，不是 A6000 性能保证。
- 不接 Context-IR、Regenerate-2K 或其他云端后处理 API。
- 首选 ComfyUI 原生 H3 节点、pruned INT8 Transformer、Ampere 适配量化文本编码器与 CPU offload。
- 模型切换前停止当前任务、卸载模型并清理显存；至少保留 4GB 显存余量。

## 安装记录

把以下信息写入运维记录，不写进源代码：权重 SHA-256、仓库提交、ComfyUI 版本、节点版本、Transformer/文本编码器量化来源、VAE、许可证原文路径、安装总空间和缓存峰值。

本地包预留约 42–45GB 只是容量规划起点；还需要下载缓存、ComfyUI、临时 latent、输出和回滚版本空间。

## 适配

1. 在 ComfyUI 外准备一个稳定的本地启动器，使其能接受命令行参数并等待工作流完成。
2. 复制 `config/examples/h3-command.example.json`，替换实际路径，不改变占位符语义。
3. 使用 `video-workstation configure-model` 登记数组。策略会拒绝云端 endpoint、密钥字段和 curl/wget。
4. 手动跑 2–3 秒冒烟样本，再进入 5/10/15 秒正式准入；冒烟结果不能开放生产档。

## 准入矩阵

对 5、10、15 秒分别执行 16:9 和 9:16，每个组合连续 10 次。每次保存加载时间、生成耗时、显存峰值、系统内存、GPU 温度、输出可读性、音轨、音画同步和失败原因。

通过条件：恰好 10 次、成功率 ≥90%、零 OOM、零损坏文件。15 秒失败只隐藏 15 秒，不影响 5/10 秒。固定文案口播仍走 CosyVoice + MuseTalk。

## 许可证

安装当天重新读取官方 LICENSE，并把版本/哈希写入 ModelProfile 和项目 NOTICE。公开发布保留 AI 标识与要求的 MiniMax H3 归属。年收入和地域触发条件见根目录 `NOTICE.md`，必要时交由法务确认。
