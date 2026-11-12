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

# 回收项状态机：
#   pending（待清理）-> cleaning（清理中）-> done（完成）
#                                   \-> error（错误，可重试回到 pending）
RECLAIM_ITEM_STATES = ("pending", "cleaning", "done", "error")

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

-- 回收单：一次「预览 -> 确认」的固定范围（产线 + 检测日期上界）。
CREATE TABLE IF NOT EXISTS reclaim_jobs (
    id           TEXT PRIMARY KEY,
    line         TEXT NOT NULL,             -- 预览时选定的产线，空串表示全部产线
    date_upper   TEXT NOT NULL,             -- 检测日期上界（含）
    status       TEXT NOT NULL,             -- preview / confirmed
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reclaim_jobs_status ON reclaim_jobs(status);

-- 回收项：预览瞬间快照固定的任务，确认后只处理这些行，之后新发布的任务不纳入。
CREATE TABLE IF NOT EXISTS reclaim_items (
    job_id         TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    line           TEXT NOT NULL,
    date           TEXT NOT NULL,
    published_file TEXT,
    status         TEXT NOT NULL,
    error          TEXT NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT,
    PRIMARY KEY (job_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_reclaim_items_status ON reclaim_items(status);
"""

# 旧库增量迁移：为 tasks 补充回收标记列。
_TASK_MIGRATIONS = (
    ("reclaimed_at", "ALTER TABLE tasks ADD COLUMN reclaimed_at TEXT"),
    ("reclaim_job_id", "ALTER TABLE tasks ADD COLUMN reclaim_job_id TEXT"),
)


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
        self._migrate_tasks()
        self._conn.commit()
        # 进程内取消事件：worker 轮询它来中断浏览器操作。
        self._cancel_events: dict[str, threading.Event] = {}

    def _migrate_tasks(self) -> None:
        """对旧版数据库增量加列，保留既有任务与回收元数据。"""
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for column, ddl in _TASK_MIGRATIONS:
            if column not in existing:
                self._conn.execute(ddl)

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

    # ---- 文件回收 -------------------------------------------------------
    def preview_reclaim(self, line: str, date_upper: str) -> tuple[dict, list[dict]]:
        """按产线 + 检测日期上界（含）快照符合条件的已发布任务，生成预览单。

        范围在预览瞬间固定：回收项写入后不再变化，确认后只处理这些任务，
        预览之后新发布的文件不会被纳入。同一时刻只允许一个待确认预览单。
        """
        existing = self.get_pending_preview_job()
        if existing is not None:
            raise ValueError("已有待确认的回收预览，请先确认或取消后再试")
        job_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO reclaim_jobs (id, line, date_upper, status, created_at)"
                " VALUES (?, ?, ?, 'preview', ?)",
                (job_id, line, date_upper, _now()),
            )
            if line:
                sql = (
                    "SELECT * FROM tasks WHERE status = 'published'"
                    " AND reclaimed_at IS NULL AND line = ? AND date <= ?"
                    " AND NOT EXISTS ("
                    "  SELECT 1 FROM reclaim_items ri WHERE ri.task_id = tasks.id)"
                    " ORDER BY date ASC, line ASC, created_at ASC"
                )
                params: tuple = (line, date_upper)
            else:
                sql = (
                    "SELECT * FROM tasks WHERE status = 'published'"
                    " AND reclaimed_at IS NULL AND date <= ?"
                    " AND NOT EXISTS ("
                    "  SELECT 1 FROM reclaim_items ri WHERE ri.task_id = tasks.id)"
                    " ORDER BY date ASC, line ASC, created_at ASC"
                )
                params = (date_upper,)
            rows = self._conn.execute(sql, params).fetchall()
            items = [dict(r) for r in rows]
            for t in items:
                self._conn.execute(
                    "INSERT INTO reclaim_items"
                    " (job_id, task_id, line, date, published_file, status, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                    (job_id, t["id"], t["line"], t["date"],
                     t["published_file"], _now()),
                )
            self._conn.commit()
        # 返回回收项视图（键名与回收项表一致），保持预览/明细字段统一。
        return self.get_reclaim_job(job_id), self.list_reclaim_items(job_id)

    def get_pending_preview_job(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reclaim_jobs WHERE status = 'preview'"
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_reclaim_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reclaim_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            items = self._conn.execute(
                "SELECT * FROM reclaim_items WHERE job_id = ?"
                " ORDER BY date ASC, line ASC, task_id ASC",
                (job_id,),
            ).fetchall()
            job["items"] = [dict(r) for r in items]
            counts: dict[str, int] = {s: 0 for s in RECLAIM_ITEM_STATES}
            for it in job["items"]:
                counts[it["status"]] += 1
            job["counts"] = counts
            return job

    def list_reclaim_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reclaim_jobs ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            jobs = []
            for row in rows:
                job = dict(row)
                counts = {
                    r["status"]: r["n"]
                    for r in self._conn.execute(
                        "SELECT status, COUNT(*) AS n FROM reclaim_items"
                        " WHERE job_id = ? GROUP BY status",
                        (job["id"],),
                    ).fetchall()
                }
                job["counts"] = {s: counts.get(s, 0) for s in RECLAIM_ITEM_STATES}
                jobs.append(job)
            return jobs

    def list_reclaim_items(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reclaim_items WHERE job_id = ?"
                " ORDER BY date ASC, line ASC, task_id ASC",
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def cancel_reclaim_job(self, job_id: str) -> tuple[bool, str]:
        """取消尚未确认的预览单：删除快照项，任务恢复可回收状态。"""
        with self._lock:
            job = self.get_reclaim_job(job_id)
            if job is None:
                return False, "回收单不存在"
            if job["status"] != "preview":
                return False, "回收单已确认，无法取消"
            self._conn.execute(
                "DELETE FROM reclaim_items WHERE job_id = ?", (job_id,)
            )
            self._conn.execute(
                "DELETE FROM reclaim_jobs WHERE id = ?", (job_id,)
            )
            self._conn.commit()
            return True, "预览已取消，未改动任何任务"

    def confirm_reclaim_job(self, job_id: str) -> tuple[dict | None, str]:
        """确认回收：原子地把预览项固定的任务一起标记为已回收。

        确认后这些任务立即拒绝新下载；后台清理另行异步执行，确认不被慢删除阻塞。
        """
        with self._lock:
            job = self.get_reclaim_job(job_id)
            if job is None:
                return None, "回收单不存在"
            if job["status"] != "preview":
                return None, "回收单已确认，不能重复确认"
            if not job["items"]:
                return None, "预览范围为空，没有可回收任务"
            stamp = _now()
            self._conn.execute(
                "UPDATE reclaim_jobs SET status = 'confirmed', confirmed_at = ?"
                " WHERE id = ?",
                (stamp, job_id),
            )
            # 只标记预览快照里的任务；之后新发布的任务不受影响。
            self._conn.executemany(
                "UPDATE tasks SET reclaimed_at = ?, reclaim_job_id = ? WHERE id = ?",
                [(stamp, job_id, it["task_id"]) for it in job["items"]],
            )
            self._conn.commit()
            return self.get_reclaim_job(job_id), "回收已受理，所选任务已标记为已回收"

    def reclaim_pending_items(self, limit: int = 5) -> list[dict[str, Any]]:
        """取已确认回收单中待清理的项，供后台清理线程认领。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ri.* FROM reclaim_items ri"
                " JOIN reclaim_jobs rj ON rj.id = ri.job_id"
                " WHERE rj.status = 'confirmed' AND ri.status = 'pending'"
                " ORDER BY rj.created_at ASC, ri.date ASC, ri.task_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_reclaim_item_cleaning(self, job_id: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reclaim_items SET status = 'cleaning',"
                " attempts = attempts + 1, updated_at = ?"
                " WHERE job_id = ? AND task_id = ?",
                (_now(), job_id, task_id),
            )
            self._conn.commit()

    def mark_reclaim_item_done(self, job_id: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reclaim_items SET status = 'done', error = '', updated_at = ?"
                " WHERE job_id = ? AND task_id = ?",
                (_now(), job_id, task_id),
            )
            self._conn.commit()

    def mark_reclaim_item_error(self, job_id: str, task_id: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reclaim_items SET status = 'error', error = ?, updated_at = ?"
                " WHERE job_id = ? AND task_id = ?",
                (message[:500], _now(), job_id, task_id),
            )
            self._conn.commit()

    def retry_reclaim_item(self, job_id: str, task_id: str) -> tuple[bool, str]:
        """单项重试：错误态回到待清理。任务保持已回收，下载不会恢复。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM reclaim_items WHERE job_id = ? AND task_id = ?",
                (job_id, task_id),
            ).fetchone()
            if row is None:
                return False, "回收项不存在"
            if row["status"] != "error":
                return False, "仅错误状态的回收项可以重试"
            self._conn.execute(
                "UPDATE reclaim_items SET status = 'pending', error = '', updated_at = ?"
                " WHERE job_id = ? AND task_id = ?",
                (_now(), job_id, task_id),
            )
            self._conn.commit()
            return True, "已重新加入清理队列"

    def retry_reclaim_job_errors(self, job_id: str) -> tuple[bool, str]:
        with self._lock:
            job = self.get_reclaim_job(job_id)
            if job is None:
                return False, "回收单不存在"
            if job["counts"]["error"] == 0:
                return False, "该回收单没有错误项"
            self._conn.execute(
                "UPDATE reclaim_items SET status = 'pending', error = '', updated_at = ?"
                " WHERE job_id = ? AND status = 'error'",
                (_now(), job_id),
            )
            self._conn.commit()
            return True, "全部错误项已重新加入清理队列"

    def recover_reclaim_state(self) -> int:
        """重启恢复：清理中断的项回到待清理，并修复回收标记，绝不恢复下载。"""
        fixed = 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reclaim_items SET status = 'pending', updated_at = ?"
                " WHERE status = 'cleaning'",
                (_now(),),
            )
            fixed += cur.rowcount
            # 已确认回收单中的任务必须保持已回收标记（防止确认事务半提交导致漏标）。
            cur = self._conn.execute(
                "UPDATE tasks SET reclaimed_at = COALESCE(reclaimed_at, ?),"
                " reclaim_job_id = ri.job_id"
                " FROM reclaim_items ri"
                " JOIN reclaim_jobs rj ON rj.id = ri.job_id"
                " WHERE tasks.id = ri.task_id AND rj.status = 'confirmed'"
                " AND tasks.reclaimed_at IS NULL",
                (_now(),),
            )
            fixed += cur.rowcount
            self._conn.commit()
        return fixed

    def close(self) -> None:
        with self._lock:
            self._conn.close()
