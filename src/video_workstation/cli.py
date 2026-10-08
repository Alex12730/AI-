from __future__ import annotations

import argparse
import getpass

from .config import Settings
from .db import Database
from .services.projects import bootstrap_admin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-workstation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化数据库")
    admin = subparsers.add_parser("create-admin", help="创建首个管理员")
    admin.add_argument("username")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    database = Database(Settings())
    database.create_schema()
    if args.command == "init-db":
        print("数据库已初始化")
        return
    password = getpass.getpass("管理员密码（至少 12 位）: ")
    confirmation = getpass.getpass("再次输入密码: ")
    if password != confirmation:
        raise SystemExit("两次密码不一致")
    with database.session() as session:
        admin = bootstrap_admin(session, args.username, password)
        print(f"管理员已创建: {admin.username}")


if __name__ == "__main__":
    main()
