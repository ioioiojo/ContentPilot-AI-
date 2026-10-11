import argparse
import getpass
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import set_key
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from .config import Settings
from .database import Database, LoginSession, User
from .security import password_hash


def main():
    parser = argparse.ArgumentParser(description="ContentPilot 账号与数据库管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("check-config", help="校验配置，不调用模型或写入数据库")
    sub.add_parser("configure-tikhub", help="隐藏输入密钥并保存到现有 .env；不调用付费接口")
    backup = sub.add_parser("backup-local", help="通过 SQLite backup API 备份，包括 WAL 数据")
    backup.add_argument("output", help="新的备份文件路径；不会覆盖已有文件")
    for name in ("create-admin", "reset-password"):
        command = sub.add_parser(name)
        command.add_argument("username")
    args = parser.parse_args()
    if args.command == "configure-tikhub":
        path = Path(".env")
        if not path.is_file():
            parser.error("请先准备 .env；不会覆盖或创建其他配置文件")
        key = getpass.getpass("TikHub API key (hidden): ").strip()
        if not key or any(char.isspace() for char in key):
            parser.error("密钥不能为空或包含空白")
        set_key(str(path), "TIKHUB_API_KEY", key, quote_mode="always")
        set_key(str(path), "CONTENTPILOT_SEARCH_XIAOHONGSHU_PROVIDER", "tikhub", quote_mode="never")
        path.chmod(0o600)
        print("小红书搜索密钥已保存；请重启 web/worker。未请求任何付费接口。")
        return
    try:
        settings = Settings.from_env()
    except ValueError:
        parser.error("配置校验失败；检查 APP_ENV、数据库、HTTPS、主机、模型凭据和任务限额")
    if args.command == "check-config":
        print(f"Configuration valid: {settings.app_env}; no external API calls made")
        return
    database_url = settings.database_url
    if args.command == "backup-local":
        url = make_url(database_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            parser.error("该命令仅支持文件 SQLite，服务器 PostgreSQL 请使用 scripts/backup.sh")
        source = Path(url.database).resolve()
        output = Path(args.output).resolve()
        if not source.is_file() or output == source or output.exists():
            parser.error("源数据库必须存在，备份路径必须是不同的新文件")
        output.parent.mkdir(parents=True, exist_ok=True)
        # No initialize before backup: retain the exact pre-migration database.
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
            closing(sqlite3.connect(output)) as dst,
        ):
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查失败")
        from .config_backup import existing_key, verify_key

        key = existing_key()
        backup_db = Database("sqlite:///" + output.as_posix())
        try:
            verify_key(backup_db, key)
        finally:
            backup_db.engine.dispose()
        if key:
            key_output = Path(str(output) + ".key")
            with key_output.open("xb") as stream:
                stream.write(key)
            key_output.chmod(0o600)
        print(f"SQLite backup verified: {output}")
        return
    db = Database(database_url)
    db.initialize()
    if args.command == "init-db":
        print("Database schema v5 ready (additive migration)")
        return
    username = args.username.lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", username):
        parser.error("用户名应为 3 到 64 位英文、数字、点、下划线或短横线")
    password = getpass.getpass("Password (12-128 characters): ")
    if not 12 <= len(password) <= 128 or password != password.strip():
        parser.error("密码长度须为 12 到 128，首尾不能有空白")
    if getpass.getpass("Repeat password: ") != password:
        parser.error("两次密码不一致")
    with db.session() as s:
        user = s.scalar(select(User).where(User.username == username))
        if args.command == "create-admin":
            if user:
                parser.error("用户名已存在；不会覆盖原账号")
            s.add(User(username=username, password=password_hash(password), admin=True))
        else:
            if not user:
                parser.error("用户不存在")
            user.password = password_hash(password)
            s.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    print("Done")


if __name__ == "__main__":
    main()
