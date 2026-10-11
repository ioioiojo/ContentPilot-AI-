"""Key backup/restore for maintenance scripts. Export must be redirected to a protected file."""

import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import inspect, select

from .config import Settings
from .database import Database, ServiceConfig


def existing_key():
    value = os.getenv("CONTENTPILOT_CONFIG_KEY", "")
    if value:
        return value.encode()
    path = Path(os.getenv("CONTENTPILOT_CONFIG_KEY_FILE", "data/service-config.key"))
    return path.read_bytes() if path.is_file() else None


def verify_key(db, key):
    if not inspect(db.engine).has_table("service_configs"):
        return
    with db.session() as s:
        rows = s.scalars(select(ServiceConfig)).all()
        if rows and not key:
            raise ValueError("缺少配置加密密钥，服务无法恢复")
        for row in rows:
            Fernet(key).decrypt(row.encrypted.encode())


def restore_key(db, key):
    Fernet(key)
    verify_key(db, key)
    configured = os.getenv("CONTENTPILOT_CONFIG_KEY", "")
    if configured:
        if configured.encode() != key:
            raise ValueError("服务器环境加密密钥与备份不一致，请更新环境密钥再启动")
        return
    path = Path(os.getenv("CONTENTPILOT_CONFIG_KEY_FILE", "data/service-config.key"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"export", "restore", "check"}:
        raise SystemExit("Usage: config_backup export|restore|check (redirect exported key)")
    db = Database(Settings.from_env().database_url)
    try:
        if sys.argv[1] == "restore":
            restore_key(db, sys.stdin.buffer.read(100))
        else:
            key = existing_key()
            verify_key(db, key)
            if sys.argv[1] == "export" and key:
                sys.stdout.buffer.write(key)
    except Exception:  # noqa: BLE001 - no secret/exception payloads in maintenance output
        raise SystemExit(
            "Configuration key verification failed; restore the matching key before startup"
        ) from None
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    main()
