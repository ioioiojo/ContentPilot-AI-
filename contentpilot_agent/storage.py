from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from .models import RunResult


class RunStore:
    """Small SQLite persistence layer for reproducible run history."""

    def __init__(self, path: str | Path = "contentpilot.db") -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload TEXT NOT NULL
                )
                """
            )

    def save(self, result: RunResult) -> str:
        run_id = result.run_id or uuid.uuid4().hex
        result.run_id = run_id
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs (run_id, payload) VALUES (?, ?)",
                (run_id, json.dumps(result.model_dump(), ensure_ascii=False)),
            )
        return run_id

    def get(self, run_id: str) -> RunResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return RunResult.model_validate(json.loads(row["payload"]))
