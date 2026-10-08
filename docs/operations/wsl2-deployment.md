# Windows 11 + WSL2 部署

## 1. 主机基线

1. Windows、NVIDIA 驱动、BIOS/ME 使用硬件厂商稳定版本；先完成 [硬件验收](hardware-acceptance.md)。
2. WSL2 安装 Ubuntu LTS，并在 WSL 内确认 `nvidia-smi` 能看到 RTX A6000 48GB。
3. 代码、SQLite、活跃项目放 WSL2 ext4（例如 `/srv/ai-video-workstation`），不要把 SQLite 放到 `/mnt/c`。
4. 模型、缓存、活跃资产放 NVMe。机械盘挂载到固定归档目录，只保存已验收项目包。

## 2. 应用

```bash
cd /srv/ai-video-workstation
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
export VIDEO_WORKSTATION_DATA_DIR=/srv/ai-video-workstation/data
export VIDEO_WORKSTATION_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
video-workstation init-db
video-workstation create-admin admin
```

密钥通过只读环境文件或 systemd credential 注入，不写进仓库。Web 与 Worker 分成两个服务；Worker 崩溃不应带走 Web。

## 3. 局域网

- Uvicorn 默认监听 `127.0.0.1`。反向代理或端口转发只绑定批准的办公网卡。
- Windows 防火墙入站规则只允许指定办公网段。
- 不为模型进程配置公网反向代理；ComfyUI 如需本地 HTTP，仅监听 loopback。
- 权重首次下载结束后，执行离线演练并检查进程连接日志。

## 4. 自动启动

为 Web 和 Worker 各建 systemd unit，`WorkingDirectory` 指向项目，`EnvironmentFile` 指向仓库外只读配置。Web 使用 `scripts/run_web.sh`，Worker 使用 `scripts/run_worker.sh`。服务账号只需数据目录、模型目录和归档目录的最小权限。

## 5. 备份与恢复

- 停止写入或使用 SQLite 在线备份 API 后备份 `.db`，同时保存对应迁移版本。
- 活跃资产按项目目录备份；已验收归档包含追溯清单和 SHA-256。
- 每月实际做一次恢复演练；只看到备份文件不算通过。
