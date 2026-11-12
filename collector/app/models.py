"""任务模型与 SQLite 存储。

任务状态机：
    queued -> running -> published（成功发布）
                         -> failed（可重试）
              queued/running -> cancelled（可重试）
重试会新建一条任务（retry_of 指向上一条），保证历史可追溯。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

# 终态之外的状态
ACTIVE_STATES = ("queued", "running", "cancelling")
FINAL_STATES = ("published", "failed", "cancelled")
RETRYABLE_STATES = ("failed", "cancelled")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    line            TEXT NOT NULL,
    date            TEXT NOT NULL,
    status          TEXT NOT NULL,
    message         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    retry_of        TEXT,
    published_file  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TaskStore:
    """所有 DB 访问经同一把锁串行化，配合单 worker 线程足够使用。"""

    def __init__(self, db_path: str | None = None):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            db_path or str(config.DB_PATH), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        # 进程内取消事件：worker 轮询它来中断浏览器操作。
        self._cancel_events: dict[str, threading.Event] = {}

    # ---- 基础工具 -------------------------------------------------------
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def _event(self, task_id: str) -> threading.Event:
        event = self._cancel_events.get(task_id)
        if event is None:
            event = threading.Event()
            self._cancel_events[task_id] = event
        return event

    def is_cancel_set(self, task_id: str) -> bool:
        return self._event(task_id).is_set()

    def create_task(self, line: str, date: str, retry_of: str | None = None,
                    attempts: int = 1) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, line, date, status, created_at, attempts, retry_of)"
                " VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (task_id, line, date, _now(), attempts, retry_of),
            )
            self._conn.commit()
        self._event(task_id)  # 预建事件
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def next_queued(self) -> dict[str, Any] | None:
        """FIFO 取最早的 queued 任务，保证门户会话不被交叉占用。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE status = 'queued'"
                " ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {columns} WHERE id = ?",
                (*fields.values(), task_id),
            )
            self._conn.commit()

    def mark_running(self, task_id: str) -> None:
        self.update(task_id, status="running", started_at=_now(),
                    message="浏览器已接管门户会话，开始筛选")

    def mark_published(self, task_id: str, file_name: str) -> None:
        self.update(task_id, status="published", finished_at=_now(),
                    published_file=file_name, message="校验通过，已发布到质量库")

    def mark_failed(self, task_id: str, message: str) -> None:
        self.update(task_id, status="failed", finished_at=_now(), message=message)

    def mark_cancelled(self, task_id: str, message: str = "任务已取消") -> None:
        self.update(task_id, status="cancelled", finished_at=_now(), message=message)

    def request_cancel(self, task_id: str) -> tuple[bool, str]:
        """请求取消。返回 (是否成功, 说明)。"""
        with self._lock:
            task = self.get_task(task_id)
            if task is None:
                return False, "任务不存在"
            if task["status"] in FINAL_STATES:
                return False, f"任务已处于终态 {task['status']}，无法取消"
            self._event(task_id).set()
            if task["status"] == "queued":
                # 排队中：直接终结，不必等 worker。
                self.update(task_id, status="cancelled", finished_at=_now(),
                            message="排队中取消，未占用门户会话")
            else:
                self.update(task_id, status="cancelling",
                            message="取消请求已下发，正在停止浏览器操作")
            return True, "取消请求已受理"

    def retry(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self.get_task(task_id)
            if task is None or task["status"] not in RETRYABLE_STATES:
                return None
            return self.create_task(
                task["line"], task["date"],
                retry_of=task_id, attempts=task["attempts"] + 1,
            )

    def recover_stale(self) -> int:
        """进程重启后，把未完成任务标记为 failed（可重试）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status = 'failed', finished_at = ?,"
                " message = '采集服务重启，任务中断，可重试'"
                " WHERE status IN ('queued', 'running', 'cancelling')",
                (_now(),),
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
