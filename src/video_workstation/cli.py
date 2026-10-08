from __future__ import annotations

import argparse
import getpass
import json
import time
from pathlib import Path

from sqlalchemy import select

from .config import Settings
from .db import Database
from .models import ModelProfile
from .services.models import configure_local_profile, record_benchmark, seed_model_profiles
from .services.projects import bootstrap_admin
from .worker import Worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-workstation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化数据库")
    admin = subparsers.add_parser("create-admin", help="创建首个管理员")
    admin.add_argument("username")
    worker = subparsers.add_parser("worker", help="运行单 GPU Worker")
    worker.add_argument("--once", action="store_true", help="只认领一个任务后退出")
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
    database = Database(Settings())
    database.create_schema()
    with database.session() as session:
        seed_model_profiles(session)
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
        worker = Worker(database.settings.worker_id)
        try:
            while True:
                with database.session() as session:
                    worked = worker.run_once(session)
                if args.once:
                    print("已处理一个任务" if worked else "当前没有可认领任务")
                    return
                time.sleep(1 if worked else 3)
        except KeyboardInterrupt:
            print("Worker 已停止")
            return
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
