"""本地模拟的旧仪器门户。

特性：
- 账号全局只允许一个会话，新登录会把旧会话踢下线（对应真实门户的单会话限制）。
- 按产线 + 日期筛选检测明细，导出 CSV（中文表头：产线,日期,样品号,测量值）。
- 可通过环境变量或 /admin 页面切换「延迟导出」和「返回错误产线文件」两种故障模式。
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import threading
import time
from datetime import date
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = os.environ.get("PORTAL_SECRET_KEY", "portal-demo-secret")

USERNAME = os.environ.get("PORTAL_USERNAME", "admin")
PASSWORD = os.environ.get("PORTAL_PASSWORD", "instrument123")
# 导出动作的人工延迟（秒），由「延迟导出」开关启用。
EXPORT_DELAY = float(os.environ.get("PORTAL_EXPORT_DELAY", "8"))

LINES = ["A线", "B线", "C线"]

# 全局门户状态：单会话 + 故障开关。
_state_lock = threading.Lock()
_state = {
    "session_token": None,   # 当前唯一有效会话
    "delay_export": os.environ.get("PORTAL_DELAY_EXPORT", "0") == "1",
    "bad_file": os.environ.get("PORTAL_BAD_FILE", "0") == "1",
}


def _generate_records(line: str, day: str) -> list[dict[str, str]]:
    """以可重复方式生成某天某产线的检测明细（门户端原始数据）。"""
    count = 6 + int(hashlib.sha256(f"{line}|{day}".encode()).hexdigest(), 16) % 5
    records = []
    for i in range(1, count + 1):
        digest = hashlib.sha256(f"{line}|{day}|{i}".encode()).hexdigest()
        value = 10 + int(digest[:8], 16) % 9000 / 100.0
        records.append(
            {
                "产线": line,
                "日期": day,
                "样品号": f"{line.replace('线', '')}-{day.replace('-', '')}-{i:03d}",
                "测量值": f"{value:.2f}",
            }
        )
    return records


def _is_logged_in() -> bool:
    token = session.get("token")
    return bool(token) and _state["session_token"] == token


def _require_login():
    if not _is_logged_in():
        session.clear()
        return False
    return True


@app.route("/", methods=["GET"])
def index():
    if not _require_login():
        return redirect(url_for("login"))
    return render_template("index.html", lines=LINES)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == USERNAME and password == PASSWORD:
            token = hashlib.sha256(os.urandom(32)).hexdigest()
            with _state_lock:
                old = _state["session_token"]
                _state["session_token"] = token
            session.clear()
            session["token"] = token
            session["user"] = username
            if old is not None:
                flash("提示：该账号在别处登录，原会话已被下线（门户仅允许一个会话）。", "warn")
            else:
                flash("登录成功。", "ok")
            return redirect(url_for("index"))
        flash("用户名或密码错误。", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    with _state_lock:
        if session.get("token") == _state["session_token"]:
            _state["session_token"] = None
    session.clear()
    return redirect(url_for("login"))


def _read_filter() -> tuple[str | None, str | None, str | None]:
    line = request.form.get("line", "").strip()
    day = request.form.get("date", "").strip()
    if line not in LINES:
        return None, None, "请选择有效产线。"
    try:
        date.fromisoformat(day)
    except ValueError:
        return None, None, "日期格式无效，应为 YYYY-MM-DD。"
    return line, day, None


def _csv_response(line: str, day: str, *, bad_line: bool) -> Response:
    # 错误产线文件：表头正确，但数据里混入其他产线，用于触发采集端校验拦截。
    actual_line = "X线" if bad_line else line
    rows = _generate_records(actual_line, day)
    if bad_line:
        rows[0]["产线"] = "Z线"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["产线", "日期", "样品号", "测量值"])
    writer.writeheader()
    writer.writerows(rows)
    payload = "﻿" + buf.getvalue()  # 带 BOM，模拟仪器门户常见导出格式
    # 头部必须是 latin-1 安全的 ASCII：文件名走 RFC 5987 的 filename* 承载中文。
    ascii_name = f"inspection_{hashlib.sha1(f'{line}|{day}'.encode()).hexdigest()[:10]}.csv"
    utf8_name = f"inspection_{line}_{day}.csv"
    disposition = (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quote(utf8_name)}"
    )
    return Response(
        payload.encode("utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@app.route("/query", methods=["POST"])
def query():
    if not _require_login():
        return redirect(url_for("login"))
    line, day, err = _read_filter()
    if err:
        flash(err, "error")
        return redirect(url_for("index"))
    records = _generate_records(line, day)
    return render_template(
        "index.html", lines=LINES, result_line=line, result_date=day, records=records
    )


@app.route("/export", methods=["POST"])
def export():
    if not _require_login():
        return redirect(url_for("login"))
    line, day, err = _read_filter()
    if err:
        flash(err, "error")
        return redirect(url_for("index"))
    with _state_lock:
        delay = _state["delay_export"]
        bad = _state["bad_file"]
        delay_seconds = EXPORT_DELAY if delay else 0.0
    if delay_seconds:
        # 分段睡眠，门户被挤下线时可以更快返回错误，而不是干等。
        deadline = time.monotonic() + delay_seconds
        while time.monotonic() < deadline:
            if not _is_logged_in():
                flash("会话已失效（账号在别处登录），导出中断。", "error")
                return redirect(url_for("login"))
            time.sleep(min(0.5, deadline - time.monotonic()))
    if not _is_logged_in():
        return redirect(url_for("login"))
    return _csv_response(line, day, bad_line=bad)


@app.route("/admin", methods=["GET", "POST"])
def admin():
    """演示控制台：切换故障模式，无需登录便于演示。"""
    if request.method == "POST":
        with _state_lock:
            _state["delay_export"] = request.form.get("delay_export") == "on"
            _state["bad_file"] = request.form.get("bad_file") == "on"
        flash("门户故障模式已更新。", "ok")
        return redirect(url_for("admin"))
    with _state_lock:
        snapshot = dict(_state)
    snapshot["active_session"] = snapshot["session_token"] is not None
    return render_template("admin.html", state=snapshot)


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
