"""模拟门户功能测试（Flask 测试客户端，无需浏览器）。"""
import importlib.util
import sys
import unittest
from pathlib import Path

# 门户与采集服务的包名都叫 app，这里按文件路径显式加载避免 sys.modules 撞名。
_portal_path = Path(__file__).resolve().parents[1] / "portal" / "app.py"
_spec = importlib.util.spec_from_file_location("mock_portal_app", _portal_path)
portal_module = importlib.util.module_from_spec(_spec)
sys.modules["mock_portal_app"] = portal_module
_spec.loader.exec_module(portal_module)


class PortalTest(unittest.TestCase):
    def setUp(self):
        portal_module.app.config["TESTING"] = True
        self.client = portal_module.app.test_client()
        # 重置门户全局状态
        with portal_module._state_lock:
            portal_module._state["session_token"] = None
            portal_module._state["delay_export"] = False
            portal_module._state["bad_file"] = False

    def _login(self):
        return self.client.post(
            "/login",
            data={"username": "admin", "password": "instrument123"},
            follow_redirects=True,
        )

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_login_required(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_bad_credentials(self):
        resp = self.client.post(
            "/login", data={"username": "admin", "password": "wrong"}
        )
        self.assertIn("错误", resp.get_data(as_text=True))

    def test_single_session_kicks_old_one(self):
        first = portal_module.app.test_client()
        first.post("/login", data={"username": "admin", "password": "instrument123"})
        self.assertEqual(first.get("/").status_code, 200)

        second = portal_module.app.test_client()
        second.post("/login", data={"username": "admin", "password": "instrument123"})
        # 旧会话再访问应被踢回登录页
        resp = first.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        # 新会话正常
        self.assertEqual(second.get("/").status_code, 200)

    def test_export_csv_normal(self):
        self._login()
        resp = self.client.post(
            "/export", data={"line": "A线", "date": "2026-11-10"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["Content-Type"])
        text = resp.data.decode("utf-8-sig")
        self.assertIn("产线,日期,样品号,测量值", text)
        self.assertIn("A线,2026-11-10", text)
        for line in text.splitlines()[1:]:
            if line.strip():
                self.assertTrue(line.startswith("A线,2026-11-10,"))

    def test_bad_file_mode_returns_wrong_line(self):
        with portal_module._state_lock:
            portal_module._state["bad_file"] = True
        self._login()
        resp = self.client.post(
            "/export", data={"line": "A线", "date": "2026-11-10"}
        )
        text = resp.data.decode("utf-8-sig")
        data_lines = [ln for ln in text.splitlines()[1:] if ln.strip()]
        self.assertTrue(any(not ln.startswith("A线,") for ln in data_lines))

    def test_admin_toggles(self):
        resp = self.client.post(
            "/admin", data={"delay_export": "on"}, follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        with portal_module._state_lock:
            self.assertTrue(portal_module._state["delay_export"])
            self.assertFalse(portal_module._state["bad_file"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
