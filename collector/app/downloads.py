"""进行中的下载注册表。

回收清理在删除文件前必须等待该任务上的既有下载全部结束：
- 回收「确认」之前已经选定原文件的下载要完整、流式地返回；
- 客户端断开后下载句柄立即释放，清理随即可以继续；
- 下载只占用请求线程与文件句柄，不持有任务库锁，因此慢下载不会阻塞
  采集调度、状态查询或回收受理。
"""
from __future__ import annotations

import itertools
import threading


class DownloadRegistry:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._active: dict[str, set[int]] = {}
        self._ids = itertools.count(1)
        # 每个任务一把删除准入锁：清理删除与该任务的下载登记互斥；
        # 不同任务用不同锁，回收一个任务不会阻塞其他任务的下载。
        self._gates: dict[str, threading.Lock] = {}
        self._gates_guard = threading.Lock()

    def _gate_for(self, task_id: str) -> threading.Lock:
        with self._gates_guard:
            lock = self._gates.get(task_id)
            if lock is None:
                lock = threading.Lock()
                self._gates[task_id] = lock
            return lock

    def gate_for(self, task_id: str) -> threading.Lock:
        """返回某任务的删除准入锁，供清理线程在删除前后持有。"""
        return self._gate_for(task_id)

    def begin(self, task_id: str) -> int:
        """登记一个开始的下载，返回句柄标识。

        与该任务的删除准入锁互斥：若文件正在删除，登记阻塞到删除结束，
        随后由回收状态检查拒绝下载，绝不会在删除中途打开文件。
        """
        token = next(self._ids)
        lock = self._gate_for(task_id)
        with lock:
            with self._cond:
                self._active.setdefault(task_id, set()).add(token)
        return token

    def end(self, task_id: str, token: int) -> None:
        """下载结束（正常完成或客户端断开）时释放占用。"""
        with self._cond:
            handles = self._active.get(task_id)
            if handles is not None:
                handles.discard(token)
                if not handles:
                    del self._active[task_id]
                    self._cond.notify_all()

    def wait_idle(self, task_id: str, timeout: float | None = None) -> bool:
        """等待某任务没有进行中的下载，返回 True 表示已空闲。"""
        with self._cond:
            return self._cond.wait_for(
                lambda: task_id not in self._active, timeout=timeout
            )

    def active_count(self, task_id: str) -> int:
        with self._cond:
            return len(self._active.get(task_id, ()))
