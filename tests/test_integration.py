"""端到端集成测试：真实门户 + 采集服务 + 本地 Chromium。

运行前需要本机具备 Playwright Chromium（容器内由 browser 服务提供）。
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from threading import Thread
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parents[1]
PORTAL_PORT = 8123
COLLECTOR_PORT = 8124
BASE = f"http://127.0.0.1:{COLLECTOR_PORT}"

# 采集服务配置必须在 import 之前注入
os.environ["PORTAL_URL"] = f"http://127.0.0.1:{PORTAL_PORT}"
os.environ["BROWSER_CDP_URL"] = ""          # 本地自行 launch
os.environ["HEADLESS"] = "1"
os.environ["OPERATION_TIMEOUT_MS"] = "60000"

# 门户与采集服务的包名都叫 app，门户按文件路径显式加载避免撞名。
_spec = importlib.util.spec_from_file_location(
    "mock_portal_app", ROOT / "portal" / "app.py"
)
portal_module = importlib.util.module_from_spec(_spec)
sys.modules["mock_portal_app"] = portal_module
_spec.loader.exec_module(portal_module)

sys.path.insert(0, str(ROOT / "collector"))
from app import config as collector_config  # noqa: E402
from app.web import create_app  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


def _serve(app, port):
    server = make_server("127.0.0.1", port, app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _wait_http(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"服务未就绪：{url}")


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE, "本机未安装 Playwright")
class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="collector-it-"))
        os.environ["DATA_DIR"] = str(cls.tmpdir / "data")
        # 让 config 的默认路径重新解析到临时目录
        collector_config.DATA_DIR = Path(os.environ["DATA_DIR"])
        collector_config.DB_PATH = collector_config.DATA_DIR / "collector.db"
        collector_config.PUBLISHED_DIR = collector_config.DATA_DIR / "published"
        collector_config.WORK_DIR = collector_config.DATA_DIR / "work"

        # 确认浏览器真能启动，缺系统库时跳过而不是误报失败
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                browser.close()
        except Exception as exc:
            raise unittest.SkipTest(f"本地 Chromium 不可用：{exc}")

        with portal_module._state_lock:
            portal_module._state["session_token"] = None
            portal_module._state["delay_export"] = False
            portal_module._state["bad_file"] = False
        portal_module.EXPORT_DELAY = 4

        cls.portal_server = _serve(portal_module.app, PORTAL_PORT)
        cls.flask_app = create_app()
        cls.collector_server = _serve(cls.flask_app, COLLECTOR_PORT)
        cls.store = cls.flask_app.extensions["store"]
        cls.worker = cls.flask_app.extensions["worker"]
        _wait_http(f"http://127.0.0.1:{PORTAL_PORT}/healthz")
        _wait_http(f"{BASE}/healthz")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.worker.shutdown()
            cls.collector_server.shutdown()
            cls.portal_server.shutdown()
            cls.store.close()
        finally:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    def setUp(self):
        # 用例间隔离：等待上一用例的任务全部进入终态，避免状态互相干扰。
        deadline = time.time() + 60
        while time.time() < deadline:
            active = [
                t for t in self.store.list_tasks()
                if t["status"] in ("queued", "running", "cancelling")
            ]
            if not active:
                return
            time.sleep(0.5)
        raise AssertionError("上一用例的任务未在 60 秒内结束")

    def _post(self, path, data=None):
        import urllib.parse
        body = urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(
            BASE + path, data=body, method="POST",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = resp.read()
            try:
                import json
                return resp.status, json.loads(payload)
            except ValueError:
                return resp.status, payload

    def _create_task(self, line, day):
        """创建任务并返回服务端生成的任务记录（含准确 id）。"""
        status, task = self._post("/api/tasks", {"line": line, "date": day})
        assert status == 201, task
        return task

    def _get_json(self, path):
        import json
        with urllib.request.urlopen(BASE + path, timeout=10) as resp:
            return json.loads(resp.read())

    def _wait_status(self, task_id, target, timeout=45):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            task = self._get_json(f"/api/tasks/{task_id}")
            last = task["status"]
            if task["status"] == target:
                return task
            if task["status"] in ("failed", "cancelled") and target not in (
                "failed",
                "cancelled",
            ):
                raise AssertionError(f"任务进入 {task['status']}：{task['message']}")
            time.sleep(0.4)
        raise AssertionError(f"等待 {target} 超时，当前状态 {last}")

    # ------------------------------------------------------------------
    def test_01_full_flow_publishes_and_downloads(self):
        created = self._create_task("A线", "2026-11-10")
        task = self._wait_status(created["id"], "published", timeout=60)
        self.assertTrue(task["published_file"])

        with urllib.request.urlopen(
            f"{BASE}/tasks/{task['id']}/download", timeout=10
        ) as resp:
            payload = resp.read().decode("utf-8-sig")
        self.assertIn("产线,日期,样品号,测量值", payload)
        self.assertTrue(
            all(
                ln.startswith("A线,2026-11-10,")
                for ln in payload.splitlines()[1:]
                if ln.strip()
            )
        )
        # 工作目录应已清理，只保留发布文件
        self.assertFalse((collector_config.WORK_DIR / task["id"]).exists())

    def test_02_queue_is_serial_single_session(self):
        # 连续提交两个任务，二者不应出现并发执行
        t1 = self._create_task("B线", "2026-11-10")
        t2 = self._create_task("C线", "2026-11-10")
        a = self._wait_status(t1["id"], "published", timeout=60)
        b = self._wait_status(t2["id"], "published", timeout=60)
        # 串行证据：两个任务的执行区间 [started_at, finished_at] 不得重叠
        non_overlapping = (
            a["finished_at"] <= b["started_at"]
            or b["finished_at"] <= a["started_at"]
        )
        self.assertTrue(
            non_overlapping,
            f"任务执行区间重叠：{a['started_at']}~{a['finished_at']} 与 "
            f"{b['started_at']}~{b['finished_at']}",
        )

    def test_03_cancel_running_task_discards_late_download(self):
        with portal_module._state_lock:
            portal_module._state["delay_export"] = True
        try:
            task_id = self._create_task("A线", "2026-11-11")["id"]
            self._wait_status(task_id, "running", timeout=30)
            ok, _ = self.store.request_cancel(task_id)
            self.assertTrue(ok)
            task = self._wait_status(task_id, "cancelled", timeout=30)
            self.assertIn("删除未发布文件", task["message"])
            # 未发布文件与工作目录均不得残留
            self.assertFalse((collector_config.WORK_DIR / task_id).exists())
            published = list(collector_config.PUBLISHED_DIR.glob(f"*{task_id}*"))
            self.assertEqual(published, [])
            # 下载接口必须拒绝
            import urllib.error
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{BASE}/tasks/{task_id}/download", timeout=10)
            self.assertEqual(ctx.exception.code, 409)
        finally:
            with portal_module._state_lock:
                portal_module._state["delay_export"] = False

    def test_04_cancel_queued_then_next_task_takes_session(self):
        with portal_module._state_lock:
            portal_module._state["delay_export"] = True
        try:
            running_id = self._create_task("A线", "2026-11-12")["id"]
            self._wait_status(running_id, "running", timeout=30)

            queued_id = self._create_task("B线", "2026-11-12")["id"]
            # 排队任务直接取消（若已被 worker 抢先取走，则走执行中取消路径）
            ok, _ = self.store.request_cancel(queued_id)
            self.assertTrue(ok)
            queued = self._wait_status(queued_id, "cancelled", timeout=10)
            self.assertTrue(
                "排队中取消" in queued["message"]
                or "已停止浏览器操作" in queued["message"],
                queued["message"],
            )

            # 执行中的任务仍可正常走完，证明会话交接正常
            done = self._wait_status(running_id, "published", timeout=60)
            self.assertEqual(done["status"], "published")
        finally:
            with portal_module._state_lock:
                portal_module._state["delay_export"] = False

    def test_05_bad_file_blocked_then_retry_succeeds(self):
        with portal_module._state_lock:
            portal_module._state["bad_file"] = True
        try:
            bad_id = self._create_task("C线", "2026-11-09")["id"]
            failed = self._wait_status(bad_id, "failed", timeout=60)
            self.assertIn("校验失败", failed["message"])
            self.assertFalse(failed["published_file"])
        finally:
            with portal_module._state_lock:
                portal_module._state["bad_file"] = False

        # 门户恢复正常后重试 -> 新任务发布成功
        status, new_task = self._post(f"/api/tasks/{bad_id}/retry", {})
        self.assertEqual(status, 201)
        new_id = new_task["id"]
        self.assertNotEqual(new_id, bad_id)
        retried = self._wait_status(new_id, "published", timeout=60)
        self.assertEqual(retried["retry_of"], bad_id)
        self.assertEqual(retried["attempts"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
