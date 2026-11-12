# 质检数据采集系统

质检数据工程师的每日检测明细采集工具：在网页上填写产线、日期后，后台通过
Playwright 实际操作旧仪器门户完成筛选与 CSV 导出，校验通过后发布到质量库，
支持状态查看、取消、失败重试与结果下载。

## 架构

```
┌──────────┐   HTTP    ┌────────────┐   CDP    ┌──────────┐
│  portal  │ ◄──────── │ collector  │ ───────► │ browser  │
│ 模拟门户  │  筛选/导出 │ Flask+队列 │  9222    │ Chromium │
└──────────┘           └────────────┘          └──────────┘
                              │
                        /data (SQLite + 已发布 CSV)
```

- **portal**：本地模拟的旧仪器门户。账号全局仅允许一个会话（新登录踢掉旧会话），
  支持按产线+日期筛选、导出 CSV，并可在「演示控制台」切换两种故障模式：
  延迟导出（约 8 秒）、返回错误产线文件。
- **collector**：采集服务。Flask 网页 + 单线程 worker：
  - 任务排队（FIFO），同一时刻只有一个任务占用门户会话，筛选到下载全程不交叉；
  - 取消执行中任务：先停止浏览器操作、删除未发布文件，再让下一个任务接管会话；
    落盘后才到达的「迟到下载」一律不发布；
  - 发布前校验：CSV 表头必须是 `产线,日期,样品号,测量值`，且**每一条**记录的
    产线、日期都与请求吻合，否则整单拒绝发布；
  - 失败/已取消任务可重试（生成新任务并记录重试链）。
- **browser**：独立 Chromium 容器，通过 CDP（9222）供采集服务连接。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 采集服务 | http://localhost:8080 | 创建任务、查看状态、取消/重试/下载 |
| 模拟门户 | http://localhost:8000 | 账号 `admin` / `instrument123` |
| 门户演示控制台 | http://localhost:8000/admin | 切换延迟导出、错误产线文件 |

### 演示流程

1. **正常采集**：采集服务首页选产线、日期 → 提交采集 → 状态从
   `排队中 → 执行中 → 已发布`，点击「下载 CSV」取回质量库文件。
2. **取消执行中任务**：门户控制台开启「延迟导出」→ 提交任务 → 执行中点「取消」
   → 任务变为「已取消」，未发布文件被删除，下载接口返回 409。
3. **错误数据拦截**：门户控制台开启「返回错误产线文件」→ 提交任务 →
   任务「失败」，消息提示产线不符、拒绝发布 → 关闭开关后点「重试」→ 发布成功。
4. **单会话排队**：连续提交多个任务，观察它们依次执行、互不交叉。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tasks` | 创建任务，表单字段 `line`、`date`（YYYY-MM-DD） |
| GET | `/api/tasks` | 任务列表（JSON） |
| GET | `/api/tasks/<id>` | 单个任务状态 |
| POST | `/api/tasks/<id>/cancel` | 取消（排队中立即取消；执行中先停浏览器再释放会话） |
| POST | `/api/tasks/<id>/retry` | 重试失败/已取消任务，返回新任务 |
| GET | `/tasks/<id>/download` | 下载已发布 CSV（未发布返回 409） |
| GET | `/healthz` | 健康检查 |

任务状态机：`queued → running → published / failed`，
`queued/running → cancelling → cancelled`；`failed`、`cancelled` 可重试。

## 本地开发（不用 Docker）

```bash
pip install -r portal/requirements.txt -r collector/requirements.txt
python -m playwright install chromium

# 终端 1：门户
python portal/app.py                      # http://127.0.0.1:8000

# 终端 2：采集服务（BROWSER_CDP_URL 留空则本地启动浏览器）
PORTAL_URL=http://127.0.0.1:8000 BROWSER_CDP_URL= DATA_DIR=./data \
  python -m app                           # 在 collector/ 目录下，http://127.0.0.1:8080
```

## 测试

```bash
python -m unittest discover -s tests -v
```

- `tests/test_validation.py`：发布校验（表头/产线/日期/空文件等 7 例）
- `tests/test_portal.py`：门户单会话踢出、导出、故障开关（7 例）
- `tests/test_integration.py`：真实浏览器端到端（5 例）——完整发布、
  串行排队、执行中取消（迟到下载不发布）、排队取消后会话交接、
  错误文件拦截后重试成功。需要本机可用的 Playwright Chromium。

## 配置项（环境变量）

采集服务：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORTAL_URL` | `http://portal:8000` | 门户地址 |
| `PORTAL_USERNAME` / `PORTAL_PASSWORD` | `admin` / `instrument123` | 门户账号 |
| `BROWSER_CDP_URL` | `http://browser:9222` | 浏览器 CDP 端点，留空则本地启动 |
| `DATA_DIR` | `/data` | SQLite 与 CSV 存储目录 |
| `OPERATION_TIMEOUT_MS` | `30000` | 浏览器单步/下载等待超时 |
| `HEADLESS` | `1` | 本地启动浏览器时是否无头 |

门户：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORTAL_DELAY_EXPORT` | `0` | 启动时开启延迟导出 |
| `PORTAL_BAD_FILE` | `0` | 启动时开启错误产线文件 |
| `PORTAL_EXPORT_DELAY` | `8` | 延迟导出秒数 |
