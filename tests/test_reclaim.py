"""文件回收子系统测试（不依赖浏览器）。

直接构造「已发布」任务与发布文件，覆盖：
- 按产线 + 检测日期上界预览、范围快照固定（预览后新发布不纳入）；
- 确认后任务一起标记已回收并拒绝新下载，其他任务照常；
- 后台异步删除、清理状态机、错误重试；
- 慢下载不阻塞确认且清理等待既有下载完成，断开后释放并继续删除；
- 重启恢复（清理中 -> 待清理、回收标记保持、下载不恢复）；
- 路径越界安全保护，避免误删其他任务文件。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

# 必须在导入 app 之前指向独立临时库
TMP = Path(tempfile.mkdtemp(prefix="collector-reclaim-"))
os.environ["DATA_DIR"] = str(TMP / "data")
os.environ["BROWSER_CDP_URL"] = ""
os.environ["RECLAIM_POLL_SECONDS"] = "0.05"

from app import config  # noqa: E402
from app.downloads import DownloadRegistry  # noqa: E402
from app.reclaim import ReclaimCleaner  # noqa: E402
from app.web import create_app  # noqa: E402

CSV_HEADER = "﻿产线,日期,样品号,测量值\n"


class ReclaimTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 无浏览器环境下不希望 worker 抢任务：让 worker 主循环立即退出。
        from app import worker as worker_mod

        worker_mod.Worker.run = lambda self: None
        cls.app = create_app()
        cls.client = cls.app.test_client()
        cls.store = cls.app.extensions["store"]
        cls.registry = cls.app.extensions["registry"]
        # 停掉应用自带的后台清理线程，改用一个不会收到 shutdown 的清理器，
        # 由测试手动驱动（_stop 始终未置位，等待逻辑正常工作）。
        cls.app.extensions["cleaner"].shutdown()
        cls.cleaner = ReclaimCleaner(cls.store, cls.registry, poll_interval=60)
        cls.cleaner._poll = 0.05

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["worker"].shutdown()
        cls.cleaner.shutdown()
        cls.store.close()
        shutil.rmtree(TMP, ignore_errors=True)

    # ---- 工具 -----------------------------------------------------------
    def setUp(self):
        # 用例隔离：清理上一用例可能遗留的待确认预览单。
        leftover = self.store.get_pending_preview_job()
        if leftover is not None:
            self.store.cancel_reclaim_job(leftover["id"])

    def _publish(self, line, day, content=None):
        """直接落库一个已发布任务并写出发布文件，绕开浏览器流程。"""
        task = self.store.create_task(line, day)
        name = f"{line}_{day}_{task['id']}.csv"
        path = config.PUBLISHED_DIR / name
        path.write_text(content or f"{CSV_HEADER}{line},{day},S-1,1.23\n",
                        encoding="utf-8")
        self.store.mark_published(task["id"], name)
        return self.store.get_task(task["id"])

    def _post(self, path, data=None):
        return self.client.post(
            path,
            data={**(data or {}), "html": ""},  # 触发 JSON 分支
            headers={"Accept": "application/json"},
        )

    def _preview(self, line, upper):
        resp = self._post("/api/reclaim/preview",
                          {"line": line, "date_upper": upper})
        self.assertEqual(resp.status_code, 201, resp.data)
        body = resp.get_json()
        return body["job"], body["items"]

    def _wait_item(self, job_id, task_id, target, timeout=5):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            items = {it["task_id"]: it
                     for it in self.store.list_reclaim_items(job_id)}
            last = items[task_id]["status"]
            if last == target:
                return items[task_id]
            time.sleep(0.03)
        raise AssertionError(f"等待 {target} 超时，当前 {last}")

    def _wait_job_idle(self, job_id, timeout=5):
        """等待一个已确认回收单没有 pending/cleaning 项。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.store.get_reclaim_job(job_id)
            if job["counts"]["pending"] + job["counts"]["cleaning"] == 0:
                return job
            time.sleep(0.03)
        raise AssertionError("回收单清理未在超时内结束")

    def _drive_clean(self):
        """手动跑一轮清理，保证断言前删除动作已执行。"""
        for item in self.store.reclaim_pending_items():
            self.cleaner._clean_one(item)

    # ---- 预览 -----------------------------------------------------------
    def test_01_preview_filters_by_line_and_date_upper(self):
        a_old = self._publish("A线", "2026-10-01")
        a_new = self._publish("A线", "2026-10-20")
        b_old = self._publish("B线", "2026-10-05")

        # 上界含当天；只取 A线且日期 <= 2026-10-10
        job, items = self._preview("A线", "2026-10-10")
        ids = {it["task_id"] for it in items}
        self.assertEqual(ids, {a_old["id"]})
        # 预览单仍是 preview，任务尚未标记回收、文件仍在、可下载。
        self.assertEqual(job["status"], "preview")
        fresh = self.store.get_task(a_old["id"])
        self.assertIsNone(fresh["reclaimed_at"])
        self.assertTrue((config.PUBLISHED_DIR / fresh["published_file"]).exists())

        # 全部产线预览：上界之内的 A_old/B_old 都在，A_new 被排除
        ok, _ = self.store.cancel_reclaim_job(job["id"])
        self.assertTrue(ok)
        job2, items2 = self._preview("", "2026-10-10")
        ids2 = {it["task_id"] for it in items2}
        self.assertEqual(ids2, {a_old["id"], b_old["id"]})
        self.assertNotIn(a_new["id"], ids2)
        ok, _ = self.store.cancel_reclaim_job(job2["id"])
        self.assertTrue(ok)

    def test_02_preview_excludes_reclaimed_and_fixes_snapshot(self):
        t1 = self._publish("C线", "2026-09-01")
        job, items = self._preview("C线", "2026-09-30")
        self.assertEqual({it["task_id"] for it in items}, {t1["id"]})

        # 预览之后才发布的文件，确认时不得纳入同一张单。
        late = self._publish("C线", "2026-09-15")
        confirmed, msg = self.store.confirm_reclaim_job(job["id"])
        self.assertIsNotNone(confirmed, msg)
        snapshot_ids = {it["task_id"] for it in confirmed["items"]}
        self.assertEqual(snapshot_ids, {t1["id"]})
        self.assertNotIn(late["id"], snapshot_ids)
        self._drive_clean()
        self._wait_job_idle(job["id"])

        # 新预览不再包含已回收任务，但包含后发布的任务。
        job2, items2 = self._preview("C线", "2026-09-30")
        self.assertEqual({it["task_id"] for it in items2}, {late["id"]})
        ok, _ = self.store.cancel_reclaim_job(job2["id"])
        self.assertTrue(ok)

    def test_03_only_one_pending_preview_allowed(self):
        self._publish("A线", "2026-08-01")
        job, _ = self._preview("A线", "2026-08-31")
        resp = self._post("/api/reclaim/preview",
                          {"line": "A线", "date_upper": "2026-08-31"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("待确认", resp.get_json()["error"])
        ok, _ = self.store.cancel_reclaim_job(job["id"])
        self.assertTrue(ok)

    def test_04_preview_validates_input(self):
        bad = self._post("/api/reclaim/preview",
                         {"line": "A线", "date_upper": "2026-13-40"})
        self.assertEqual(bad.status_code, 400)
        bad2 = self._post("/api/reclaim/preview",
                          {"line": "bad/line", "date_upper": "2026-08-31"})
        self.assertEqual(bad2.status_code, 400)

    # ---- 确认与下载拒绝 --------------------------------------------------
    def test_05_confirm_marks_all_and_blocks_download_others_keep_running(self):
        keep = self._publish("B线", "2026-07-03")
        gone1 = self._publish("B线", "2026-07-01")
        gone2 = self._publish("B线", "2026-07-02")
        job, items = self._preview("B线", "2026-07-02")
        self.assertEqual({it["task_id"] for it in items},
                         {gone1["id"], gone2["id"]})

        # 确认前两个文件都能正常下载
        for t in (gone1, gone2):
            r = self.client.get(f"/tasks/{t['id']}/download")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.headers["Content-Length"].isdigit())

        resp = self._post(f"/api/reclaim/jobs/{job['id']}/confirm")
        self.assertEqual(resp.status_code, 200)

        # 所选任务一起标记已回收、立即拒绝新下载（文件可能尚未删除）
        for t in (gone1, gone2):
            fresh = self.store.get_task(t["id"])
            self.assertTrue(fresh["reclaimed_at"])
            self.assertEqual(fresh["reclaim_job_id"], job["id"])
            r = self.client.get(f"/tasks/{t['id']}/download")
            self.assertEqual(r.status_code, 410)

        # 未选任务照常：未被标记、仍可下载。
        self.assertIsNone(self.store.get_task(keep["id"])["reclaimed_at"])
        self.assertEqual(
            self.client.get(f"/tasks/{keep['id']}/download").status_code, 200
        )

        self._drive_clean()
        job = self._wait_job_idle(job["id"])
        self.assertEqual(job["counts"]["done"], 2)
        for t in (gone1, gone2):
            self.assertFalse(
                (config.PUBLISHED_DIR / t["published_file"]).exists()
            )
        # 不在范围的文件保留
        self.assertTrue(
            (config.PUBLISHED_DIR / keep["published_file"]).exists()
        )

    def test_06_cancel_preview_changes_nothing(self):
        t = self._publish("A线", "2026-06-01")
        job, items = self._preview("A线", "2026-06-30")
        self.assertEqual(len(items), 1)
        ok, _ = self.store.cancel_reclaim_job(job["id"])
        self.assertTrue(ok)
        self.assertIsNone(self.store.get_reclaim_job(job["id"]))
        self.assertIsNone(self.store.get_task(t["id"])["reclaimed_at"])
        self.assertTrue(
            (config.PUBLISHED_DIR / t["published_file"]).exists()
        )

    # ---- 慢下载不阻塞、等待后删除 ----------------------------------------
    def test_07_slow_download_blocks_delete_but_not_confirm(self):
        t = self._publish("A线", "2026-05-01")
        path = config.PUBLISHED_DIR / t["published_file"]
        job, _ = self._preview("A线", "2026-05-31")

        # 模拟一个确认前已开始、尚未结束的慢下载。
        token = self.registry.begin(t["id"])
        try:
            resp = self._post(f"/api/reclaim/jobs/{job['id']}/confirm")
            self.assertEqual(resp.status_code, 200)  # 确认不被慢下载阻塞
            # 确认是纯 DB 操作，立即拒绝新下载，与文件是否删除无关。
            self.assertEqual(
                self.client.get(f"/tasks/{t['id']}/download").status_code, 410
            )

            # 子线程驱动一轮清理：下载未结束时它应阻塞在等待，文件保留。
            item0 = self.store.reclaim_pending_items()[0]
            entered = threading.Event()
            orig_wait = self.registry.wait_idle

            def tracked_wait(task_id, timeout=None):
                entered.set()
                return orig_wait(task_id, timeout)

            self.registry.wait_idle = tracked_wait
            thread = threading.Thread(
                target=self.cleaner._clean_one, args=(item0,), daemon=True
            )
            thread.start()
            self.assertTrue(entered.wait(timeout=3.0))  # 已进入下载等待
            time.sleep(0.1)
            self.assertTrue(thread.is_alive())  # 仍在等待慢下载
            self.registry.wait_idle = orig_wait
            self.assertEqual(
                self.store.list_reclaim_items(job["id"])[0]["status"], "cleaning"
            )
            self.assertTrue(path.exists())
        finally:
            # 模拟客户端断开/下载完成，释放占用；清理线程随即解除阻塞。
            self.registry.wait_idle = orig_wait
            self.registry.end(t["id"], token)

        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self._wait_item(job["id"], t["id"], "done")
        self.assertFalse(path.exists())
        self.assertEqual(
            self.client.get(f"/tasks/{t['id']}/download").status_code, 410
        )

    def test_08_streaming_download_and_disconnect_release(self):
        t = self._publish("A线", "2026-04-01",
                          CSV_HEADER + "A线,2026-04-01,S-1,9.9\n" * 100)
        # 完整流式返回：分块读回，内容与长度正确。
        resp = self.client.get(f"/tasks/{t['id']}/download")
        self.assertEqual(resp.status_code, 200)
        payload = b"".join(resp.iter_encoded())
        self.assertIn("产线,日期,样品号,测量值".encode(), payload)
        self.assertEqual(len(payload),
                         (config.PUBLISHED_DIR / t["published_file"]).stat().st_size)
        # 响应迭代结束后占用必须已释放（生成器 finally 执行）。
        self.assertEqual(self.registry.active_count(t["id"]), 0)

        # 客户端中途断开：直接模拟生成器关闭，验证 finally 释放占用。
        token = self.registry.begin(t["id"])
        self.assertEqual(self.registry.active_count(t["id"]), 1)
        # 下载响应对象的生成器在客户端断开时由 WSGI 服务器 close()。
        gen_resp = self.client.get(f"/tasks/{t['id']}/download")
        gen = gen_resp.response
        next(gen)  # 读出第一个分块
        gen.close()  # 模拟客户端断开
        self.assertEqual(self.registry.active_count(t["id"]), 1)  # 仅剩手动 token
        self.registry.end(t["id"], token)
        self.assertEqual(self.registry.active_count(t["id"]), 0)

    # ---- 错误重试 --------------------------------------------------------
    def test_09_delete_error_retriable_and_succeeds(self):
        t = self._publish("A线", "2026-03-01")
        path = config.PUBLISHED_DIR / t["published_file"]
        job, _ = self._preview("A线", "2026-03-31")
        self._post(f"/api/reclaim/jobs/{job['id']}/confirm")

        # 注入一次删除故障（不依赖文件系统权限，root/容器下行为稳定）。
        import app.reclaim as reclaim_mod
        real_remove = reclaim_mod.os.remove
        calls = {"n": 0}

        def boom(p, *a, **k):
            calls["n"] += 1
            raise OSError("模拟磁盘故障")

        reclaim_mod.os.remove = boom
        try:
            self._drive_clean()
        finally:
            reclaim_mod.os.remove = real_remove
        self.assertEqual(calls["n"], 1)
        item = self._wait_item(job["id"], t["id"], "error")
        self.assertIn("模拟磁盘故障", item["error"])
        self.assertTrue(path.exists())

        # 单项重试后成功。
        ok, _ = self.store.retry_reclaim_item(job["id"], t["id"])
        self.assertTrue(ok)
        self._drive_clean()
        self._wait_item(job["id"], t["id"], "done")
        self.assertFalse(path.exists())

        # 整单重试接口对无错误单返回失败。
        ok2, msg = self.store.retry_reclaim_job_errors(job["id"])
        self.assertFalse(ok2)
        self.assertIn("没有错误项", msg)

    # ---- 重启恢复 --------------------------------------------------------
    def test_10_restart_resumes_cleaning_and_never_restores_download(self):
        t = self._publish("C线", "2026-02-01")
        path = config.PUBLISHED_DIR / t["published_file"]
        job, _ = self._preview("C线", "2026-02-28")
        self._post(f"/api/reclaim/jobs/{job['id']}/confirm")
        # 模拟删除进行到一半进程中断：项停在 cleaning，文件仍在。
        self.store.mark_reclaim_item_cleaning(job["id"], t["id"])
        self.assertEqual(
            self.store.list_reclaim_items(job["id"])[0]["status"], "cleaning"
        )
        self.assertTrue(path.exists())

        # 用同一数据库新开一个「进程」：只建 store + cleaner 做恢复。
        restarted_store = type(self.store)(str(config.DB_PATH))
        fixed = restarted_store.recover_reclaim_state()
        self.assertGreaterEqual(fixed, 1)
        item = restarted_store.list_reclaim_items(job["id"])[0]
        self.assertEqual(item["status"], "pending")  # 中断项回到待清理
        # 回收标记保持，下载绝不恢复。
        self.assertTrue(restarted_store.get_task(t["id"])["reclaimed_at"])
        registry2 = DownloadRegistry()
        cleaner2 = ReclaimCleaner(restarted_store, registry2, poll_interval=0.02)
        cleaner2.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and path.exists():
                time.sleep(0.03)
            self.assertFalse(path.exists())
        finally:
            cleaner2.shutdown()
            restarted_store.close()
        self.assertEqual(
            self.client.get(f"/tasks/{t['id']}/download").status_code, 410
        )

    # ---- 安全：不误删其他文件 --------------------------------------------
    def test_11_path_guard_rejects_traversal_and_foreign_files(self):
        guard = ReclaimCleaner._safe_published_path
        self.assertIsNone(guard("../etc/passwd"))
        self.assertIsNone(guard("/etc/passwd"))
        self.assertIsNone(guard(""))
        self.assertIsNone(guard("a/../../b.csv"))
        ok_path = guard("A线_2026-01-01_abc.csv")
        self.assertIsNotNone(ok_path)
        self.assertEqual(ok_path.parent.resolve(),
                         config.PUBLISHED_DIR.resolve())

    def test_12_snapshot_mismatch_blocks_delete(self):
        # target 进入回收单；foreign 不在任何回收单中，是绝不能被误删的他人文件。
        target = self._publish("A线", "2026-01-01")
        foreign = self._publish("A线", "2026-01-02")
        job, _ = self._preview("A线", "2026-01-01")
        self._post(f"/api/reclaim/jobs/{job['id']}/confirm")
        # 篡改快照文件名，使其与任务当前登记不一致（模拟异常数据）。
        with self.store.lock:
            self.store._conn.execute(
                "UPDATE reclaim_items SET published_file = ?"
                " WHERE job_id = ? AND task_id = ?",
                (foreign["published_file"], job["id"], target["id"]),
            )
            self.store._conn.commit()
        self._drive_clean()
        item = self._wait_item(job["id"], target["id"], "error")
        self.assertIn("不一致", item["error"])
        # 目标文件与无关的他人文件都必须还在，绝不误删。
        self.assertTrue(
            (config.PUBLISHED_DIR / target["published_file"]).exists()
        )
        self.assertTrue(
            (config.PUBLISHED_DIR / foreign["published_file"]).exists()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
