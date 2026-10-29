# 本机视频模型最小链路验证（2026-10-26）

本次验证在本机 ComfyUI `0.40.0`、NVIDIA RTX A6000、G 盘模型目录完成。测试只验证“工作台适配器 → ComfyUI → MP4 输出”的最小链路，不等同于生产档位准入。

| 模型 | 结果 | 预览文件 | 视频流 | 音频流 | 分辨率 | 帧率 |
| --- | --- | --- | --- | --- | --- | --- |
| Wan2.2 | 通过 | `G:\AI_Workstation\data\deployment-verification\wan-2.2-preview.mp4` | H.264 | AAC 静音轨 | 320×192 | 24 fps |
| LTX-2.3 | 通过 | `G:\AI_Workstation\data\deployment-verification\ltx-2.3-preview.mp4` | H.264 | AAC 原生音频 | 256×256 | 24 fps |
| MiniMax H3 FL2VA | 通过 | `G:\AI_Workstation\data\deployment-verification\minimax-h3-fl2va-preview.mp4` | H.264 | AAC 原生音频 | 416×256 | 24 fps |

## 修复记录

- 替换了损坏的 Wan UMT5 文本编码器；旧文件保存在同目录的 `.broken-20261026` 备份中。
- 替换了损坏的 LTX Gemma 文本编码器；旧文件保存在同目录的 `.broken-20261026` 备份中。
- 替换了损坏的 MiniMax H3 视频 VAE 和 Qwen 文本编码器；旧文件保存在同目录的 `.broken-20261026` 备份中。
- 启动脚本 `E:\Workspace\AI视频自动化工作流\start-video-service.ps1` 的编码损坏已修复，ComfyUI 现在从 `G:\AI_Workstation\runtime` 启动。

## 当前准入边界

三套模型均已证明适配器可以实际生成最小预览，但数据库中的生产 `validated_presets` 仍为空。要开放正式的 5 秒/10 秒/15 秒/20 秒档位，必须按仓库规则逐个模型、逐个时长和画幅连续完成十次基准，并记录成功率、OOM、文件损坏、耗时、显存、内存、温度和音画同步。

