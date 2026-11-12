"""采集服务配置（全部可通过环境变量覆盖）。"""
from __future__ import annotations

import os
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


# 门户与浏览器
PORTAL_URL = _env("PORTAL_URL", "http://portal:8000")
PORTAL_USERNAME = _env("PORTAL_USERNAME", "admin")
PORTAL_PASSWORD = _env("PORTAL_PASSWORD", "instrument123")
# 留空则本地启动浏览器；容器内指向独立 Chromium 的 CDP 端点。
BROWSER_CDP_URL = _env("BROWSER_CDP_URL", "http://browser:9222")
HEADLESS = _env("HEADLESS", "1") == "1"

# 存储
DATA_DIR = Path(_env("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "collector.db"
PUBLISHED_DIR = DATA_DIR / "published"
WORK_DIR = DATA_DIR / "work"

# 调度
POLL_INTERVAL = float(_env("POLL_INTERVAL", "1.0"))
OPERATION_TIMEOUT_MS = int(_env("OPERATION_TIMEOUT_MS", "30000"))
# 导出轮询间隔，取消信号按此粒度生效。
DOWNLOAD_TICK_SECONDS = float(_env("DOWNLOAD_TICK_SECONDS", "0.25"))
NAV_TIMEOUT_MS = int(_env("NAV_TIMEOUT_MS", "15000"))

# 质量库 CSV 表头
CSV_HEADER = ["产线", "日期", "样品号", "测量值"]
