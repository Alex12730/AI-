from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select

from .config import RUNTIME_VERSION, Settings
from .db import Database
from .models import ModelProfile
from .services.models import configure_local_profile, record_benchmark, seed_model_profiles
from .services.projects import bootstrap_admin
from .services.workers import touch_worker
from .worker import Worker


def stop_idle_worker(database: Database, pid: int) -> None:
    if os.name != "nt" or not database.settings.database_url.startswith("sqlite") or pid <= 0:
        raise RuntimeError("此命令仅支持本机 Windows SQLite 工作站")
    marker = database.settings.data_dir / "service-logs" / "worker-runtime.json"
    try:
        marker_info = json.loads(marker.read_text(encoding="utf-8"))
        if type(marker_info.get("pid")) is not int or marker_info["pid"] != pid:
            raise ValueError("Worker PID 与当前数据目录不匹配")
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("不能确认旧 Worker 属于当前工作站，已拒绝停止") from exc
    executable = str(Path(sys.executable)).replace("'", "''")
    identity_script = (
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
        "$parent=if ($p) { Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\" }; "
        f"$ownedPath=($p -and $p.CommandLine.IndexOf('{executable}',[StringComparison]::OrdinalIgnoreCase) -ge 0) "
        f"-or ($parent -and $parent.CommandLine.IndexOf('{executable}',[StringComparison]::OrdinalIgnoreCase) -ge 0); "
        "if ($ownedPath -and $p.CommandLine -match 'video_workstation\\.cli\\s+worker') { 'OWNED' }"
    )
    identity = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", identity_script],
                              capture_output=True, text=True, check=False)
    if identity.returncode != 0 or identity.stdout.strip() != "OWNED":
        raise RuntimeError("目标进程不是当前工作站的 Worker，已拒绝停止")
    # BEGIN IMMEDIATE serializes with QueueService.claim_next. The old Worker
    # cannot claim a new task between the idle check and process termination.
    with database.engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            from .models import Task
            running = connection.execute(select(Task.id).where(Task.status == "running")).first()
            if running is not None:
                raise RuntimeError("还有正在运行的镜头，旧 Worker 保持运行；请稍后重试。")
            result = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                    capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise RuntimeError(f"无法停止旧 Worker PID {pid}: {result.stderr.strip()}")
        finally:
            connection.rollback()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-workstation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化数据库")
    admin = subparsers.add_parser("create-admin", help="创建首个管理员")
    admin.add_argument("username")
    worker = subparsers.add_parser("worker", help="运行单 GPU Worker")
    worker.add_argument("--once", action="store_true", help="只认领一个任务后退出")
    subparsers.add_parser("monitor-temperature", help="独立采集 GPU 温度并维护保护状态")
    subparsers.add_parser("active-task-count", help="供启动器检查是否有正在运行的任务")
    stop_worker = subparsers.add_parser("stop-idle-worker", help="持有队列写锁时安全停止旧 Worker")
    stop_worker.add_argument("pid", type=int)
    configure = subparsers.add_parser("configure-model", help="登记本地模型命令数组")
    configure.add_argument("slug")
    configure.add_argument("--command-json", required=True, type=Path)
    configure.add_argument("--version", required=True)
    configure.add_argument("--quantization", required=True)
    benchmark = subparsers.add_parser("record-benchmark", help="录入十次本机准入结果")
    benchmark.add_argument("slug")
    benchmark.add_argument("duration", type=int, choices=[5, 10, 15, 20])
    benchmark.add_argument("aspect_ratio", choices=["16:9", "9:16"])
    benchmark.add_argument("runs_json", type=Path)
    subparsers.add_parser("list-models", help="列出模型和已验证档位")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "monitor-temperature":
        from .thermal import run_thermal_monitor
        run_thermal_monitor(Settings())
        return
    database = Database(Settings())
    database.create_schema()
    with database.session() as session:
        seed_model_profiles(session)
    if args.command == "active-task-count":
        from .models import Task
        with database.session() as session:
            print(len(list(session.scalars(select(Task.id).where(Task.status == "running")))))
        return
    if args.command == "stop-idle-worker":
        try:
            stop_idle_worker(database, args.pid)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"已安全停止空闲 Worker PID {args.pid}")
        return
    if args.command == "init-db":
        print("数据库已初始化")
        return
    if args.command == "create-admin":
        password = getpass.getpass("管理员密码（至少 12 位）: ")
        confirmation = getpass.getpass("再次输入密码: ")
        if password != confirmation:
            raise SystemExit("两次密码不一致")
        with database.session() as session:
            admin = bootstrap_admin(session, args.username, password)
            print(f"管理员已创建: {admin.username}")
        return
    if args.command == "worker":
        worker = Worker(database.settings.worker_id, settings=database.settings)
        marker = database.settings.data_dir / "service-logs" / "worker-runtime.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"pid": os.getpid(),
            "thermal_monitor_enabled": database.settings.thermal_monitor_enabled,
            "runtime_version": RUNTIME_VERSION}), encoding="utf-8")
        try:
            while True:
                with database.session() as session:
                    worked = worker.run_once(session)
                if args.once:
                    print("已处理一个任务" if worked else "当前没有可认领任务")
                    return
                time.sleep(1 if worked else 3)
        except KeyboardInterrupt:
            with database.session() as session:
                touch_worker(session, database.settings.worker_id, "stopped")
            print("Worker 已停止")
            return
        finally:
            try:
                if json.loads(marker.read_text(encoding="utf-8")).get("pid") == os.getpid():
                    marker.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    if args.command == "configure-model":
        command = json.loads(args.command_json.read_text(encoding="utf-8"))
        if not isinstance(command, list):
            raise SystemExit("command JSON 顶层必须是字符串数组")
        with database.session() as session:
            profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == args.slug))
            if profile is None:
                raise SystemExit(f"找不到模型 Profile: {args.slug}")
            configure_local_profile(
                profile,
                command=command,
                version=args.version,
                quantization=args.quantization,
            )
            print(f"已配置本地模型: {profile.display_name}")
        return
    if args.command == "record-benchmark":
        runs = json.loads(args.runs_json.read_text(encoding="utf-8"))
        if not isinstance(runs, list):
            raise SystemExit("基准结果 JSON 顶层必须是数组")
        with database.session() as session:
            profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == args.slug))
            if profile is None:
                raise SystemExit(f"找不到模型 Profile: {args.slug}")
            qualified = record_benchmark(profile, args.duration, args.aspect_ratio, runs)
            print("准入通过" if qualified else "准入未通过，档位保持隐藏")
        return
    if args.command == "list-models":
        with database.session() as session:
            for profile in session.scalars(select(ModelProfile).order_by(ModelProfile.display_name)):
                presets = ", ".join(
                    f"{item['duration_seconds']}s {item['aspect_ratio']}" for item in profile.validated_presets_json
                ) or "未验证"
                print(f"{profile.slug}\t{'启用' if profile.enabled else '停用'}\t{presets}")
        return


if __name__ == "__main__":
    main()
