"""基于 Playwright 的门户自动化操作。

每个任务独立 BrowserContext，所有步骤之间检查取消事件；
worker 保证同一时刻只有一个任务在操作门户（单会话、不交叉）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from . import config


class TaskCancelled(Exception):
    """任务被取消，浏览器操作立即中止。"""


class PortalBot:
    def __init__(self, task: dict, work_dir: Path, cancel_event: threading.Event):
        self.task = task
        self.work_dir = work_dir
        self.cancel_event = cancel_event
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---- 取消与超时辅助 -------------------------------------------------
    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled("收到取消信号，停止浏览器操作")

    def _sleep_cancellable(self, seconds: float) -> None:
        if self.cancel_event.wait(seconds):
            raise TaskCancelled("收到取消信号，停止浏览器操作")

    # ---- 生命周期 -------------------------------------------------------
    def _start(self) -> None:
        self._pw = sync_playwright().start()
        if config.BROWSER_CDP_URL:
            # 容器内：连接独立 Chromium 容器的 CDP 端点。
            self._browser = self._pw.chromium.connect_over_cdp(
                config.BROWSER_CDP_URL, timeout=config.NAV_TIMEOUT_MS
            )
            self._context = self._browser.new_context(accept_downloads=True)
        else:
            # 本地调试：自行启动浏览器。
            self._browser = self._pw.chromium.launch(
                headless=config.HEADLESS, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            self._context = self._browser.new_context(accept_downloads=True)
        self._context.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        self._context.set_default_timeout(config.OPERATION_TIMEOUT_MS)
        self._page = self._context.new_page()

    def close(self) -> None:
        # 任何情况下都关掉上下文，未完成的下载请求会被浏览器中断。
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        if self._pw is not None:
            self._pw.stop()

    # ---- 门户步骤 -------------------------------------------------------
    def _is_login_page(self) -> bool:
        self._check_cancel()
        return self._page.locator("input[name='username']").count() > 0

    def _login(self) -> None:
        page = self._page
        page.goto(config.PORTAL_URL + "/login")
        page.fill("input[name='username']", config.PORTAL_USERNAME)
        page.fill("input[name='password']", config.PORTAL_PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        if self._is_login_page():
            raise RuntimeError("门户登录失败：用户名或密码被拒绝")

    def _ensure_session(self) -> None:
        self._check_cancel()
        page = self._page
        page.goto(config.PORTAL_URL + "/")
        page.wait_for_load_state("networkidle")
        if self._is_login_page():
            self._login()

    def _apply_filter(self) -> None:
        self._check_cancel()
        page = self._page
        page.select_option("select[name='line']", self.task["line"])
        page.fill("input[name='date']", self.task["date"])
        # 查询表单的提交按钮
        page.click("form[action='/query'] button[type='submit']")
        page.wait_for_load_state("networkidle")
        if self._is_login_page():
            raise RuntimeError("筛选过程中会话失效（可能被其他登录挤下线）")
        rows = page.locator("table tbody tr")
        rows.first.wait_for(state="visible", timeout=config.OPERATION_TIMEOUT_MS)
        if rows.count() == 0:
            raise RuntimeError("门户筛选结果为空，无法导出")

    def _export_and_save(self) -> Path:
        """点击导出并取消感知地等待下载，落盘到任务工作目录。"""
        self._check_cancel()
        page = self._page
        collected: list = []
        page.on("download", lambda d: collected.append(d))

        # 下载响应不产生导航，禁止点击后等待导航，否则会无谓超时。
        page.click(
            "form[action='/export'] button[type='submit']", no_wait_after=True
        )

        # 不使用阻塞式 expect_download：按 tick 轮询，使取消信号即时生效。
        deadline = time.monotonic() + config.OPERATION_TIMEOUT_MS / 1000
        while not collected:
            if self.cancel_event.is_set():
                raise TaskCancelled("导出等待期间收到取消信号")
            if time.monotonic() > deadline:
                raise PlaywrightTimeoutError("等待门户 CSV 下载超时")
            # 导出过程中若被挤下线，门户会跳回登录页。
            if self._is_login_page():
                raise RuntimeError("导出过程中会话失效（可能被其他登录挤下线）")
            self._sleep_cancellable(config.DOWNLOAD_TICK_SECONDS)

        download = collected[0]
        suggested = download.suggested_filename or "export.csv"
        if not suggested.lower().endswith(".csv"):
            suggested += ".csv"
        target = self.work_dir / suggested
        download.save_as(str(target))  # 事件已触发后落盘很快
        return target

    def _logout(self) -> None:
        """正常结束时主动释放门户会话。"""
        try:
            self._check_cancel()
            self._page.click("form[action='/logout'] button[type='submit']")
            self._page.wait_for_load_state("networkidle")
        except Exception:
            pass

    # ---- 入口 -----------------------------------------------------------
    def run(self) -> Path:
        """执行完整流程，返回下载文件路径。任何一步取消都会抛 TaskCancelled。"""
        self._start()
        try:
            self._ensure_session()
            self._apply_filter()
            path = self._export_and_save()
            self._logout()
            return path
        finally:
            self.close()
