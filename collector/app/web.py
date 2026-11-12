"""采集服务 Web 层：任务创建、状态查看、取消、重试、下载。"""
from __future__ import annotations

import logging
import re
from datetime import date

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from . import config
from .models import FINAL_STATES, RETRYABLE_STATES, TaskStore
from .worker import Worker

log = logging.getLogger("collector.web")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINE_RE = re.compile(r"^[一-龥A-Za-z0-9_-]{1,20}$")


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = Flask(__name__)
    store = TaskStore()
    worker = Worker(store)

    # 上次进程遗留的未完成任务统一转为可重试失败态。
    recovered = store.recover_stale()
    if recovered:
        log.warning("启动恢复：%s 个中断任务标记为 failed 可重试", recovered)
    worker.start()
    app.extensions["store"] = store
    app.extensions["worker"] = worker

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

    # ---- API ------------------------------------------------------------
    @app.post("/api/tasks")
    def create_task():
        line = (request.form.get("line") or "").strip()
        day = (request.form.get("date") or "").strip()
        if not LINE_RE.match(line):
            return _bad_request("产线格式无效（1-20 位中文/字母/数字/横线/下划线）")
        if not DATE_RE.match(day):
            return _bad_request("日期格式无效，应为 YYYY-MM-DD")
        try:
            date.fromisoformat(day)
        except ValueError:
            return _bad_request("日期不是有效日历日期")
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
        task = store.get_task(task_id)
        if task is None:
            abort(404)
        if task["status"] != "published" or not task["published_file"]:
            abort(409, description="任务尚未发布，没有可下载文件")
        # send_from_directory 会拦截路径穿越，文件名来自服务端生成也更安全。
        return send_from_directory(
            config.PUBLISHED_DIR, task["published_file"], as_attachment=True
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}, 200

    return app


def _bad_request(message: str):
    return (jsonify({"ok": False, "error": message}), 400)
