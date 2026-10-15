# 本地三模型部署接入

本仓库是项目、分镜、任务队列和视频资产的管理工作站；ComfyUI 是执行生成的本地引擎。浏览器中的 `127.0.0.1:8188` 是 ComfyUI，工作站运行在 `127.0.0.1:8000`。

已增加固定脚本 `scripts/run_comfy_model.py` 和三个 API 工作流，复用 `LocalCommandAdapter`，不通过 Shell 拼接提示词。运行期只允许连接本机 ComfyUI，不使用代理，不跟随重定向。

## 当前接入范围

| 模型配置 | 权重 | 声音 | 当前模式 |
| --- | --- | --- | --- |
| wan-2.2 | Wan2.2 TI2V 5B FP16、UMT5 FP8、Wan2.2 VAE | 补静音轨，供后续配音合成 | 文生视频 |
| ltx-2.3 | LTX-2.3 22B distilled FP8、Gemma 12B FP4 | 原生音频 | 文生音视频 |
| minimax-h3-fl2va | H3 Base FL2VA INT8 convrot、Qwen NVFP4 AWQ、视频/音频 VAE | 原生音频 | 文生音视频 |

首帧/尾帧输入尚未接入工作站。H3 当前使用 Base 模式，关闭 turbo LoRA；该工作流没有负面提示词输入，传入非空负面提示词会明确失败。LTX 属于 Lightricks；从阿里 ModelScope 下载是下载渠道。

工作流会将请求时长映射到模型支持的帧数：Wan 为 `4k+1`，LTX 为 `8k+1`，H3 为 `17k+5`，均为 24fps。实际时长可能略有差异，仍须通过工作站媒体质检。

## 准备与注册

1. 安装本仓库 Python 环境和依赖（Python 3.11+），确保 `.venv/Scripts/python.exe` 可用。Windows Store 的 Python 别名不能替代真实 Python。
2. 安装 FFmpeg/FFprobe。可使用 [FFmpeg 官方下载页](https://ffmpeg.org/download.html)推荐的 Windows 构建，核对下载方 SHA256。
3. 保持本机 ComfyUI 运行，推荐本次验证使用的 0.40.0 或包含这些节点的版本。让 `extra_model_paths.yaml` 指向已有权重目录；无需复制百 GB 权重进仓库。权重和运行环境不提交 Git。此次已下载文件的来源、固定版本、大小及 SHA256 见 [权重清单](../../config/local-model-manifest.json)，迁移工作站时据此下载与校验。
4. 在仓库根目录执行以下命令，把路径替换为本机实际位置：

```powershell
$env:VIDEO_WORKSTATION_DATA_DIR = "$PWD/data"
$env:PATH = "E:/your-ffmpeg/bin;$env:PATH"
& ./.venv/Scripts/python.exe scripts/configure_comfy_models.py --ffmpeg E:/your-ffmpeg/bin/ffmpeg.exe
& ./.venv/Scripts/python.exe scripts/verify_comfy_models.py --ffprobe E:/your-ffmpeg/bin/ffprobe.exe
& ./scripts/start_local_workstation.ps1 -FFmpegDirectory E:/your-ffmpeg/bin
```

注册会检查引擎节点和权重名称，为三个配置写入固定命令数组，启用模型；注册后的状态为“已配置，待基准”。机器专用命令写入忽略的 `data/model-commands/`，小样验证视频及报告写入 `data/deployment-verification/`。首次在 `/setup` 设置管理员账户密码。启动器生成的会话密钥保存在忽略的 `data/session-secret.txt`，不会打印或提交。

重复注册相同配置会保留既有基准记录；工作流或调用代码变化会改变配置指纹，要求重新验收。执行时还会检查已登记的工作流与调用代码校验值，拒绝未经重新配置的修改。

## 生产验收

`--preview` 仅用于小样调用链验证，分辨率和采样步数较低，不能作为生产质量、耗时或稳定性结论。验证脚本直接测试适配器边界，不伪造队列准入、不写入生产基准。

正式命令不包含 `--preview`。候选配置为 Wan 1280×704/20 步、LTX 1024×576/8 步 distilled、H3 约 1344×768/50 步 Base；竖屏交换宽高。这些参数尚待本机生产实测，可能需要降低分辨率或调整采样参数。

继续沿用仓库既有规则：每个模型、时长、画幅组合连续十次实测，记录成功、OOM、损坏、耗时、显存、内存、温度和音画同步。使用 `record-benchmark` 登记真实数据后才能开放对应档位；H3 上限 15 秒。变更权重、引擎、工作流或量化方案后，应重新配置版本并重做基准。

超时会删除本任务的排队项，并使用 `prompt_id` 定向取消本任务，避免中断其他生成。依赖 ComfyUI 的定向中断接口。生成结果先写临时文件，成功后才替换工作站目标文件；失败不会发布部分文件。
