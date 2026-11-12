"""采集服务 Web 层：任务创建、状态查看、取消、重试、流式下载与文件回收。"""
from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import config
from .downloads import DownloadRegistry
from .models import FINAL_STATES, RETRYABLE_STATES, TaskStore
from .reclaim import ReclaimCleaner
from .worker import Worker

log = logging.getLogger("collector.web")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINE_RE = re.compile(r"^[一-龥A-Za-z0-9_-]{1,20}$")
RECLAIM_ITEM_LABELS = {
    "pending": "待清理",
    "cleaning": "清理中",
    "done": "完成",
    "error": "错误",
}


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = Flask(__name__)
    store = TaskStore()
    worker = Worker(store)
    registry = DownloadRegistry()
    cleaner = ReclaimCleaner(store, registry)

    # 上次进程遗留的未完成采集任务统一转为可重试失败态。
    recovered = store.recover_stale()
    if recovered:
        log.warning("启动恢复：%s 个中断任务标记为 failed 可重试", recovered)
    # 回收进度恢复：清理中断的项回到待清理，回收标记保持，绝不恢复下载。
    reclaim_fixed = store.recover_reclaim_state()
    if reclaim_fixed:
        log.warning("启动恢复：%s 个回收状态被修复并继续清理", reclaim_fixed)

    worker.start()
    cleaner.start()
    app.extensions["store"] = store
    app.extensions["worker"] = worker
    app.extensions["registry"] = registry
    app.extensions["cleaner"] = cleaner

    # ---- 页面 -----------------------------------------------------------
    @app.get("/")
    def index_page():
        tasks = store.list_tasks()
        return render_template(
            "index.html",
            tasks=tasks,
            portal_url=config.PORTAL_URL,
            final_states=FINAL_STATES,
        )

    @app.get("/reclaim")
    def reclaim_page():
        pending = store.get_pending_preview_job()
        # 待确认预览单需要逐项明细，改用带 items 的完整视图。
        preview_job = store.get_reclaim_job(pending["id"]) if pending else None
        jobs = store.list_reclaim_jobs()
        # 已确认回收单带上逐项明细，页面展示每个文件的清理状态与错误信息。
        job_details = {
            j["id"]: store.list_reclaim_items(j["id"])
            for j in jobs
            if j["status"] == "confirmed"
        }
        return render_template(
            "reclaim.html",
            preview_job=preview_job,
            jobs=jobs,
            job_details=job_details,
            item_labels=RECLAIM_ITEM_LABELS,
        )

    # ---- 采集任务 API ----------------------------------------------------
    @app.post("/api/tasks")
    def create_task():
        line = (request.form.get("line") or "").strip()
        day = (request.form.get("date") or "").strip()
        if not LINE_RE.match(line):
            return _bad_request("产线格式无效（1-20 位中文/字母/数字/横线/下划线）")
        valid, message = _validate_date(day)
        if not valid:
            return _bad_request(message)
        task = store.create_task(line, day)
        worker.notify()
        if request.accept_mimetypes.accept_json and not request.form.get("html"):
            return jsonify(task), 201
        return redirect(url_for("index_page"))

    @app.get("/api/tasks")
    def list_tasks():
        return jsonify(store.list_tasks())

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str):
        task = store.get_task(task_id)
        if task is None:
            abort(404)
        return jsonify(task)

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_task(task_id: str):
        ok, message = store.request_cancel(task_id)
        if not ok:
            return _bad_request(message)
        worker.notify()
        if request.form.get("html"):
            return redirect(url_for("index_page"))
        return jsonify({"ok": True, "message": message})

    @app.post("/api/tasks/<task_id>/retry")
    def retry_task(task_id: str):
        new_task = store.retry(task_id)
        if new_task is None:
            return _bad_request("仅失败或已取消的任务可以重试")
        worker.notify()
        if request.form.get("html"):
            return redirect(url_for("index_page"))
        return jsonify(new_task), 201

    @app.get("/tasks/<task_id>/download")
    def download_task(task_id: str):
        """已发布 CSV 的流式下载。

        - 已回收任务一律拒绝新下载（410），未发布任务返回 409；
        - 回收确认前已开始的下载在回收清理线程登记并等待，保证完整返回；
        - 分块流式读取，慢客户端只占用请求线程，不阻塞采集、状态查询与回收受理；
        - 客户端断开时生成器的 finally 释放占用，清理线程随即可以删除文件。
        """
        task = store.get_task(task_id)
        if task is None:
            abort(404)
        # 已回收 / 未发布的拒绝路径无需获取删除准入锁，避免被进行中的清理阻塞，
        # 保证「拒绝新下载」始终即时返回。
        if task.get("reclaimed_at"):
            abort(410, description="文件已超过留存期并回收，仅保留任务记录用于追溯")
        if task["status"] != "published" or not task["published_file"]:
            abort(409, description="任务尚未发布，没有可下载文件")

        # 允许下载后再进入准入锁登记，与清理线程的删除临界区互斥，
        # 保证任何在删除前已开始的下载都被清理线程等待（消除检查/登记竞态）。
        token = registry.begin(task_id)
        try:
            # 拿到锁后复核：等待期间可能刚好完成确认回收。
            fresh = store.get_task(task_id)
            if fresh is None:
                abort(404)
            if fresh.get("reclaimed_at"):
                abort(410, description="文件已超过留存期并回收，仅保留任务记录用于追溯")
            if fresh["status"] != "published" or not fresh["published_file"]:
                abort(409, description="任务尚未发布，没有可下载文件")

            path = ReclaimCleaner._safe_published_path(fresh["published_file"])
            if path is None or not path.is_file():
                abort(404, description="发布文件不存在（可能已被清理）")
        except BaseException:
            # 任何拒绝/异常路径都要释放刚登记的占用。
            registry.end(task_id, token)
            raise

        def stream():
            try:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(config.DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        yield chunk
            finally:
                # 正常读完或客户端断开都会执行，释放该任务的下载占用。
                registry.end(task_id, token)

        ascii_name = (
            fresh["published_file"].encode("ascii", "ignore").decode() or "export.csv"
        )
        headers = {
            "Content-Length": str(path.stat().st_size),
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; "
                f"filename*=UTF-8''{quote(fresh['published_file'])}"
            ),
        }
        return Response(stream(), mimetype="text/csv", headers=headers)

    # ---- 文件回收 API ----------------------------------------------------
    @app.post("/api/reclaim/preview")
    def reclaim_preview():
        line = (request.form.get("line") or "").strip()
        day = (request.form.get("date_upper") or "").strip()
        if line and not LINE_RE.match(line):
            return _bad_request("产线格式无效")
        valid, message = _validate_date(day)
        if not valid:
            return _bad_request(message)
        try:
            job, items = store.preview_reclaim(line, day)
        except ValueError as exc:
            return _bad_request(str(exc))
        if request.form.get("html"):
            return redirect(url_for("reclaim_page"))
        return jsonify({"job": job, "items": items}), 201

    @app.get("/api/reclaim/jobs")
    def reclaim_jobs():
        return jsonify(store.list_reclaim_jobs())

    @app.get("/api/reclaim/jobs/<job_id>")
    def reclaim_job_detail(job_id: str):
        job = store.get_reclaim_job(job_id)
        if job is None:
            abort(404)
        return jsonify(job)

    @app.post("/api/reclaim/jobs/<job_id>/confirm")
    def reclaim_confirm(job_id: str):
        job, message = store.confirm_reclaim_job(job_id)
        if job is None:
            return _bad_request(message)
        cleaner.notify()
        if request.form.get("html"):
            return redirect(url_for("reclaim_page"))
        return jsonify({"ok": True, "message": message, "job": job})

    @app.post("/api/reclaim/jobs/<job_id>/cancel")
    def reclaim_cancel(job_id: str):
        ok, message = store.cancel_reclaim_job(job_id)
        if not ok:
            return _bad_request(message)
        if request.form.get("html"):
            return redirect(url_for("reclaim_page"))
        return jsonify({"ok": True, "message": message})

    @app.post("/api/reclaim/jobs/<job_id>/retry-errors")
    def reclaim_retry_job(job_id: str):
        ok, message = store.retry_reclaim_job_errors(job_id)
        if not ok:
            return _bad_request(message)
        cleaner.notify()
        if request.form.get("html"):
            return redirect(url_for("reclaim_page"))
        return jsonify({"ok": True, "message": message})

    @app.post("/api/reclaim/jobs/<job_id>/items/<task_id>/retry")
    def reclaim_retry_item(job_id: str, task_id: str):
        ok, message = store.retry_reclaim_item(job_id, task_id)
        if not ok:
            return _bad_request(message)
        cleaner.notify()
        if request.form.get("html"):
            return redirect(url_for("reclaim_page"))
        return jsonify({"ok": True, "message": message})

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    return app


def _validate_date(day: str) -> tuple[bool, str]:
    if not DATE_RE.match(day):
        return False, "日期格式无效，应为 YYYY-MM-DD"
    try:
        date.fromisoformat(day)
    except ValueError:
        return False, "日期不是有效日历日期"
    return True, ""


def _bad_request(message: str):
    return (jsonify({"ok": False, "error": message}), 400)
