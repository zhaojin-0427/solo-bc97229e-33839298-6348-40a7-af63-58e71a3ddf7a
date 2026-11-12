"""单线程 worker：串行执行任务，保证门户单会话不被交叉占用。"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from . import config
from .models import TaskStore
from .portal_bot import PortalBot, TaskCancelled
from .validation import validate_csv

log = logging.getLogger("collector.worker")


class Worker(threading.Thread):
    def __init__(self, store: TaskStore):
        super().__init__(name="collector-worker", daemon=True)
        self.store = store
        self._wake = threading.Event()
        self._stop = threading.Event()

    def notify(self) -> None:
        """有新任务或状态变化时唤醒 worker。"""
        self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        log.info("采集 worker 已启动，串行调度门户任务")
        while not self._stop.is_set():
            self._wake.wait(config.POLL_INTERVAL)
            self._wake.clear()
            try:
                task = self.store.next_queued()
            except Exception:
                log.exception("读取待执行任务失败")
                continue
            if task is None:
                continue
            self._run_task(task)

    # ------------------------------------------------------------------
    def _run_task(self, task: dict) -> None:
        task_id = task["id"]
        cancel_event = self.store._event(task_id)  # 复用 models 中的事件

        # 排队期间可能已被取消。
        fresh = self.store.get_task(task_id)
        if fresh is None or fresh["status"] != "queued":
            return
        self.store.mark_running(task_id)
        log.info("任务 %s 开始执行：line=%s date=%s", task_id, task["line"], task["date"])

        work_dir = config.WORK_DIR / task_id
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        bot = PortalBot(fresh, work_dir, cancel_event)
        downloaded: Path | None = None
        try:
            downloaded = bot.run()
        except TaskCancelled:
            self._handle_cancel(task_id, work_dir)
            return
        except Exception as exc:  # 浏览器/门户侧任何异常都归为失败，可重试
            log.exception("任务 %s 浏览器操作失败", task_id)
            self._handle_failure(task_id, f"门户操作失败：{exc.__class__.__name__}: {exc}")
            return

        # 下载完成后再次核对：取消信号可能在落盘瞬间到达，
        # 这类「迟到下载」一律不得发布。
        if cancel_event.is_set() or self.store.get_task(task_id)["status"] != "running":
            log.warning("任务 %s 下载已完成但任务已被取消，丢弃迟到下载", task_id)
            self._handle_cancel(task_id, work_dir)
            return

        result = validate_csv(downloaded, fresh["line"], fresh["date"])
        if not result.ok:
            log.warning("任务 %s 校验未通过：%s", task_id, result.message)
            self._handle_failure(task_id, f"数据校验失败：{result.message}")
            return

        published_name = f"{fresh['line']}_{fresh['date']}_{task_id}.csv"
        published_path = config.PUBLISHED_DIR / published_name
        shutil.copy2(downloaded, published_path)

        # 发布前最后一次状态复核，消除竞态窗口。
        if cancel_event.is_set() or self.store.get_task(task_id)["status"] != "running":
            published_path.unlink(missing_ok=True)
            log.warning("任务 %s 发布前被取消，已删除文件", task_id)
            self._handle_cancel(task_id, work_dir)
            return

        self.store.mark_published(task_id, published_name)
        shutil.rmtree(work_dir, ignore_errors=True)
        log.info("任务 %s 已发布：%s（%s）", task_id, published_name, result.message)

    # ------------------------------------------------------------------
    def _handle_cancel(self, task_id: str, work_dir: Path) -> None:
        # 先清掉未发布的工作文件，再让下一个任务使用门户会话。
        shutil.rmtree(work_dir, ignore_errors=True)
        task = self.store.get_task(task_id)
        if task and task["status"] != "cancelled":
            self.store.mark_cancelled(
                task_id, "已停止浏览器操作并删除未发布文件，门户会话已释放"
            )
        log.info("任务 %s 取消完成，允许下一个任务接管会话", task_id)

    def _handle_failure(self, task_id: str, message: str) -> None:
        # 工作目录保留以便排查，但不会对外提供下载。
        self.store.mark_failed(task_id, message)
        log.info("任务 %s 失败（可重试）：%s", task_id, message)
