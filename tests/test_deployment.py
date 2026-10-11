import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_preserves_existing_site_ports_and_private_database():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["web"]["ports"] == ["127.0.0.1:18080:8000"]
    assert "ports" not in services["db"]
    assert "ports" not in services["worker"]
    assert services["web"]["environment"]["COOKIE_SECURE"] == "true"
    assert services["worker"]["depends_on"]["web"]["condition"] == "service_healthy"
    assert services["web"]["tmpfs"] == ["/tmp:size=32m,noexec,nosuid"]


def test_release_allowlist_excludes_secrets_and_local_artifacts():
    spec = importlib.util.spec_from_file_location(
        "release", ROOT / "scripts" / "package_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = [str(path.relative_to(ROOT)).replace("\\", "/") for path in module.release_files()]
    assert ".env" not in files
    assert not any(path.endswith((".db", ".docx", ".png")) for path in files)
    assert not any(path.startswith(("data/", "jianli/", ".venv/", "artifacts/")) for path in files)
    assert "contentpilot_agent/templates/index.html" in files
    assert "requirements.lock" in files


def test_linux_scripts_have_lf_endings():
    for path in (ROOT / "scripts").glob("*.sh"):
        assert b"\r\n" not in path.read_bytes(), path.name


def test_mcp_stdio_tool_is_callable():
    from contentpilot_agent.mcp_server import search_hot_topics

    assert search_hot_topics("coffee", "xiaohongshu")


def test_local_backup_includes_wal_without_migrating_or_overwriting(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    source = tmp_path / "original.db"
    output = tmp_path / "backup.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{source}"}
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE schema_version (id INTEGER, version INTEGER)")
        connection.execute("INSERT INTO schema_version VALUES (1,1)")
        connection.commit()
        command = [sys.executable, "-m", "contentpilot_agent.manage", "backup-local", str(output)]
        result = subprocess.run(
            command, env=env, cwd=ROOT, capture_output=True, timeout=20, check=False
        )
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(output) as backup:
            assert backup.execute("SELECT version FROM schema_version").fetchone()[0] == 1
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert (
                backup.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='users'").fetchone()[
                    0
                ]
                == 0
            )
        before = output.read_bytes()
        refused = subprocess.run(
            command, env=env, cwd=ROOT, capture_output=True, timeout=20, check=False
        )
        assert refused.returncode != 0
        assert output.read_bytes() == before
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 1
