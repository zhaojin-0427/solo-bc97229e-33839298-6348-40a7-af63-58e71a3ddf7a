"""后台文件回收清理线程。

确认回收只更新数据库（快速、可立即拒绝新下载），实际删除文件由本线程异步执行：
- 删除前等待该任务上确认前已开始的下载全部结束，慢下载不阻塞回收受理；
- 客户端断开会释放下载句柄，等待随即解除；
- 只删除回收项快照中登记的发布文件，路径越界 / 类型异常一律拒绝，绝不误删其他任务；
- 删除失败标记 error 可重试；进程在删除前后中断，重启后由恢复逻辑重新清理。
"""
from __future__ import annotations

import logging
import os
import threading

from . import config
from .downloads import DownloadRegistry
from .models import TaskStore

log = logging.getLogger("collector.reclaim")

# 等待进行中下载时每次唤醒的粒度，兼顾响应速度与 CPU。
IDLE_WAIT_TICK = 0.5


class ReclaimCleaner(threading.Thread):
    def __init__(
        self,
        store: TaskStore,
        registry: DownloadRegistry,
        poll_interval: float | None = None,
    ):
        super().__init__(name="reclaim-cleaner", daemon=True)
        self.store = store
        self.registry = registry
        self._poll = (
            poll_interval if poll_interval is not None else config.RECLAIM_POLL_SECONDS
        )
        self._wake = threading.Event()
        self._stop = threading.Event()

    def notify(self) -> None:
        """有新确认单或错误项重试时唤醒清理线程。"""
        self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        log.info("文件回收清理线程已启动")
        while not self._stop.is_set():
            self._wake.wait(self._poll)
            self._wake.clear()
            try:
                items = self.store.reclaim_pending_items()
            except Exception as exc:
                # 关闭过程中连接已关，且线程即将退出，不打异常堆栈。
                if self._stop.is_set():
                    return
                log.warning("读取待清理回收项失败：%s", exc)
                continue
            for item in items:
                if self._stop.is_set():
                    return
                self._clean_one(item)

    # ------------------------------------------------------------------
    def _clean_one(self, item: dict) -> None:
        job_id = item["job_id"]
        task_id = item["task_id"]
        name = item["published_file"]
        self.store.mark_reclaim_item_cleaning(job_id, task_id)
        log.info("开始清理回收项：job=%s task=%s file=%s", job_id, task_id, name)

        # 快照里没有文件（理论上不会出现）：直接完成，保持状态机一致。
        if not name:
            self.store.mark_reclaim_item_done(job_id, task_id)
            return

        path = self._safe_published_path(name)
        if path is None:
            self.store.mark_reclaim_item_error(
                job_id, task_id, "文件路径越界或类型非法，拒绝删除（安全保护）"
            )
            return

        # 二次核对：当前任务登记的文件名必须与回收快照一致，防止误删其他任务文件。
        task = self.store.get_task(task_id)
        if task is None or task.get("published_file") != name:
            self.store.mark_reclaim_item_error(
                job_id, task_id, "任务文件名与回收快照不一致，拒绝删除"
            )
            return

        # 删除准入临界区：与该任务的下载登记互斥（按任务加锁，不影响其他任务）。
        # - 下载 begin() 需先拿到同一把锁，故临界区内不会出现「新登记的下载」；
        # - 临界区内等待既有下载全部结束后再删，确认前已开始的下载完整返回；
        # - 慢下载只让本清理线程在此等待，不占用任务库锁，不阻塞其他接口。
        with self.registry.gate_for(task_id):
            while True:
                if self._stop.is_set():
                    # 进程要退出：保留 cleaning 状态，交重启续清，绝不跳过删除。
                    return
                if self.registry.wait_idle(task_id, timeout=IDLE_WAIT_TICK):
                    break

            try:
                # 不跟随符号链接删除外部文件；只接受发布目录内的普通文件。
                if path.is_symlink():
                    self.store.mark_reclaim_item_error(
                        job_id, task_id, "目标是符号链接，拒绝删除（安全保护）"
                    )
                    return
                if path.exists():
                    if not path.is_file():
                        self.store.mark_reclaim_item_error(
                            job_id, task_id, "目标不是普通文件，拒绝删除"
                        )
                        return
                    os.remove(path)
                # 文件已不存在（例如上次删除后才中断）：幂等视为完成。
                self.store.mark_reclaim_item_done(job_id, task_id)
                log.info("回收项清理完成：task=%s", task_id)
            except FileNotFoundError:
                self.store.mark_reclaim_item_done(job_id, task_id)
            except OSError as exc:
                log.warning("回收项删除失败：task=%s %s", task_id, exc)
                self.store.mark_reclaim_item_error(
                    job_id, task_id, f"删除失败：{exc.__class__.__name__}: {exc}"
                )
            except Exception as exc:  # 任何意外都落库为可重试错误，线程不中断
                log.exception("回收项清理出现未预期异常：task=%s", task_id)
                self.store.mark_reclaim_item_error(
                    job_id, task_id, f"清理异常：{exc.__class__.__name__}: {exc}"
                )

    @staticmethod
    def _safe_published_path(name: str):
        """把服务端登记的文件名解析为发布目录内的路径，越界返回 None。"""
        if not name or os.path.isabs(name) or "\x00" in name:
            return None
        base = config.PUBLISHED_DIR.resolve()
        candidate = config.PUBLISHED_DIR / name
        try:
            if candidate.parent.resolve() != base:
                return None
        except OSError:
            return None
        return candidate
