# 质检数据采集系统

质检数据工程师的每日检测明细采集工具：在网页上填写产线、日期后，后台通过
Playwright 实际操作旧仪器门户完成筛选与 CSV 导出，校验通过后发布到质量库，
支持状态查看、取消、失败重试与结果下载。

超过留存期的检测 CSV 可在**文件回收页**按产线 + 检测日期上界预览、确认回收：
保留采集任务记录用于追溯，仅后台删除原文件，并保证确认前已开始的下载完整返回。

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
  - **文件回收**：独立回收页 + 后台清理线程，SQLite 持久化回收范围与进度：
    - 按产线 + 检测日期上界（含当天）预览已发布、未回收任务，预览瞬间快照固定，
      确认后只处理快照中的任务，预览之后新发布的文件不纳入；
    - 确认是纯数据库操作：所选任务**一起标记为已回收并立即拒绝新下载（HTTP 410）**，
      其他任务照常运行，任务元数据与原采集入口完整保留；
    - 文件由后台线程异步删除，页面显示每项「待清理 / 清理中 / 完成 / 错误」，
      错误可单项或整单重试；
    - 删除前等待该任务确认前已开始的下载全部结束（按任务加锁，不阻塞其他任务、
      采集调度、状态查询与回收受理）；下载为分块流式返回，客户端断开即释放占用；
    - 删除前后进程中断，重启后把「清理中」项恢复为「待清理」继续清理，
      已回收标记保持、下载绝不恢复；路径越界/快照不一致一律拒绝删除，防止误删。
- **browser**：独立 Chromium 容器，通过 CDP（9222）供采集服务连接。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

| 入口 | 地址 | 说明 |
| --- | --- | --- |
| 采集服务 | http://localhost:8080 | 创建任务、查看状态、取消/重试/下载 |
| 文件回收页 | http://localhost:8080/reclaim | 预览/确认回收，查看清理进度与重试 |
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
5. **文件回收**：先采集若干不同产线/日期任务并发布，进入「文件回收页」：
   - 选产线（或全部）与检测日期上界 → 预览，确认范围只含符合条件的已发布任务；
   - 点「确认回收」→ 任务列表中这些任务变为「已回收」，下载返回 410，
     页面进度从「待清理 → 清理中 → 完成」，发布目录中对应 CSV 被删除；
   - 不在范围内的任务仍显示「已发布」且可正常下载；
   - 重启容器后，未完成的清理项会继续清理，已回收任务不会恢复下载。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/tasks` | 创建任务，表单字段 `line`、`date`（YYYY-MM-DD） |
| GET | `/api/tasks` | 任务列表（JSON） |
| GET | `/api/tasks/<id>` | 单个任务状态 |
| POST | `/api/tasks/<id>/cancel` | 取消（排队中立即取消；执行中先停浏览器再释放会话） |
| POST | `/api/tasks/<id>/retry` | 重试失败/已取消任务，返回新任务 |
| GET | `/tasks/<id>/download` | 流式下载已发布 CSV；未发布 409，已回收 410 |
| GET | `/reclaim` | 文件回收页（预览/确认/进度/重试） |
| POST | `/api/reclaim/preview` | 按 `line`（可空=全部）+ `date_upper` 预览并固定快照 |
| GET | `/api/reclaim/jobs` | 回收单列表（含各状态计数） |
| GET | `/api/reclaim/jobs/<id>` | 回收单明细（含每个回收项状态/错误） |
| POST | `/api/reclaim/jobs/<id>/confirm` | 确认回收：任务一起标记已回收、拒绝新下载 |
| POST | `/api/reclaim/jobs/<id>/cancel` | 取消尚未确认的预览单 |
| POST | `/api/reclaim/jobs/<id>/retry-errors` | 整单重试全部错误项 |
| POST | `/api/reclaim/jobs/<id>/items/<task_id>/retry` | 重试单个错误项 |
| GET | `/healthz` | 健康检查 |

任务状态机：`queued → running → published / failed`，
`queued/running → cancelling → cancelled`；`failed`、`cancelled` 可重试。
已发布任务被回收后状态值仍为 `published`，但带 `reclaimed_at` 标记，
下载接口返回 410，页面显示「已回收」，任务记录永久保留。

回收项状态机：`pending（待清理）→ cleaning（清理中）→ done（完成）`，
任一步删除失败转 `error（错误，可重试回到 pending）`；
进程中断后 `cleaning` 在重启时恢复为 `pending` 继续清理。

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
- `tests/test_reclaim.py`：文件回收子系统（12 例，无需浏览器）——范围预览与快照固定、
  确认后拒绝新下载且不影响其他任务、慢下载不阻塞确认且删除等待既有下载、
  流式下载断开释放、删除错误重试、重启恢复续清、路径越界与快照不一致保护。
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
| `RECLAIM_POLL_SECONDS` | `0.5` | 回收后台清理线程轮询间隔 |
| `DOWNLOAD_CHUNK_BYTES` | `65536` | 流式下载分块大小 |

门户：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORTAL_DELAY_EXPORT` | `0` | 启动时开启延迟导出 |
| `PORTAL_BAD_FILE` | `0` | 启动时开启错误产线文件 |
| `PORTAL_EXPORT_DELAY` | `8` | 延迟导出秒数 |
