# 15 · Batch9 验收单 —— 真 Worker 接平台跑一条端到端真流水线

> 目标：不再用演示数据。一个**真执行方进程**（`worker/agent_worker.py`）接入平台，
> 自己轮询接活 → 自己执行 → 自己报活 → 自己回执，全链路落台账、看板可查。
>
> 验收方式：`python scripts/verify_b9.py`（真起 uvicorn + 真起 Worker 子进程，36 项断言）
> 结论：**全部通过（36 PASS / 0 FAIL）**

---

## 一、这一批暴露并修掉的三处真实缺陷

写这一批的价值不在"加个脚本"，而在于**让一个真执行方接上来，把平台里"演示数据遮着的洞"顶出来**。三处都是文档写了、实现却没兑现：

| # | 缺陷 | 后果（真 Worker 一接就中） | 修法 | 落点 |
| --- | --- | --- | --- | --- |
| 1 | 派发时 `target_worker = envelope.sender.value` | Master 派给 workbuddy 的任务被记成 `master`，Worker 按自己名字轮询**永远查不到活** | 优先取 `payload.target_worker`（缺省回退 `sender`），取值必须属 5 值白名单 | `app/services/dispatch.py::_resolve_target_worker` |
| 2 | 台账只记状态、不记任务内容 | Worker 拿到 task_id 也**不知道要干什么**（TaskView 里没有载荷） | 加列 `task_ledger.dispatch_payload`（JSON 文本），`TaskView` 增 `payload` 字段 | `migrations/versions/v003_task_dispatch_payload.py`、`app/models/__init__.py`、`app/api/schemas_http.py` |
| 3 | `task_reject` 只允许从「待验收」来；「验收驳回」不能重派 | Worker 干不了发 reject 会 **409**；驳回后任务**死在那里**无法重试 | 按 05 状态机放开：reject 允许「执行中／待验收」；dispatch 允许从「验收驳回」重派（次数耗尽则转「执行失败」） | `app/services/dispatch.py::TASK_REJECT_FROM / TASK_DISPATCH_FROM` |

> 三处都属"文档口径已在、代码没跟上"，因此**不升协议版本**（v1.0 不变）：加字段是兼容变更，语义回归文档原意，见 `docs/base/12-版本变更规范.md`。

---

## 二、接口面没动（硬约束）

| 项 | 值 |
| --- | --- |
| 接口数 | **仍为 9**（`/api/health`、`/api/messages`、`/api/pipelines/{trace_id}`、`/api/tasks/{task_id}`、`/api/archive/{trace_id}`、`/api/pipelines`、`/api/tasks`、`/api/export/{trace_id}`、`/api/commands`） |
| 方法 | 只有 GET / POST |
| Worker 用了什么 | `GET /api/tasks?status=执行中&target_worker=<我>` + `POST /api/messages`，**仅此两条** |

`GET /api/tasks` 本来就支持 `target_worker` + `status` 组合筛选，所以"轮询接活"不需要新接口。这也是这一批刻意守住的东西：**能不加接口就不加**。

---

## 三、Worker 执行端（新增 `worker/`）

```
worker/
  agent_worker.py            轮询 / 执行 / 心跳 / 回执 主程序（纯标准库）
  __init__.py
  examples/hello_task.py     最小任务脚本样例（写产物、可 --fail 演示失败）
```

设计三条：

1. **只走 HTTP**：不 `import app.*`、不碰 `sqlalchemy`、不读台账 → 平台改库结构不影响 Worker，Worker 也绕不过平台的状态机与台账（CI 有静态红线拦）。
2. **执行有边界**：`payload.exec` 只接受"白名单目录（`--allow-root`）内的 `.py` + 字符串参数"，拒绝 `..` 越界与任意 shell；单任务硬超时（默认 300s，规格可缩短不可延长）。
3. **只碰自己的活**：轮询就按 `target_worker=自己` 过滤，别人的任务压根不进队列（比"事后跳过"更硬）。

执行规格（`payload.exec`）：

```json
{
  "script": "worker/examples/hello_task.py",
  "args": ["--label", "demo"],
  "timeout_seconds": 30,
  "artifacts": ["artifacts/hello.txt"]
}
```

回执 payload：`output.artifacts`（产物绝对路径 + 是否存在 + 字节数）、`logs`（stdout 尾部）、`metadata.self_check / known_issues / heartbeats`。
做不了就是 `task_reject` + 具体原因（缺规格 / 越界 / 超时 / 非 0 退出码），**不硬扛到平台判「执行失败」**。

---

## 四、端到端实测（`verify_b9.py` 36 项断言，摘关键）

环境：干净库 `sqlite:///./data/verify_b9.db`、干净归档 `./archive/_verify_b9`、真起 uvicorn（随机端口）、真起 Worker 子进程。

| 段落 | 断言 | 结果 |
| --- | --- | --- |
| A 治理面 | 接口数仍 9 / 方法只有 GET/POST | PASS |
| B 派发归属 | `target_worker` = payload 指定的 `workbuddy`（不是 sender=master） | PASS（`workbuddy`） |
| B 载荷回读 | `GET /api/tasks/{id}` 能读到 `payload.exec.script` | PASS |
| C 真执行 | Worker 进程 rc=0，用时 6.6s，日志有 `pick_task` | PASS |
| C 收敛 | 任务 → **待验收**，`return_time` 已落库 | PASS |
| C 产物 | `artifacts/slow.txt` 真实存在，内容是脚本最后一次写入（step=5） | PASS |
| C 心跳 | 归档计数 `{task_dispatch:1, task_heartbeat:2, task_return:1}`（心跳是执行期真发的） | PASS |
| D 越权 | 派给 `codex` 的任务，workbuddy 一轮后仍是「执行中」，日志里从未出现该 task_id | PASS |
| D 越界 | 白名单外脚本 → **验收驳回**，原因写明"脚本越界" | PASS |
| E 幂等 | 同一 dispatch 重投 → 同 `archive_id`、`status_changed=false`、状态仍「待验收」 | PASS |
| F 失败链路 | 脚本 exit 3 → Worker 发 `task_reject` → **验收驳回**，原因含"退出码 3" | PASS |
| G 台账/看板 | `?target_worker=workbuddy&status=待验收` 能筛到；流水线详情、导出 json/csv、首页看板均 200 | PASS |
| H 静态红线 | Worker 不 import app、无 sqlalchemy、无删除调用 | PASS |

> 心跳为什么能证明"真发"：慢任务脚本跑 6 秒，心跳间隔 2 秒，归档里落了 2 条 `task_heartbeat` 且 `timestamp` 在执行窗口内；这不是事后补的。

---

## 五、迁移与门禁

| 项 | 内容 |
| --- | --- |
| 新迁移 | `v003_task_dispatch_payload`（加列，可空，幂等 `up`）→ `schema_version` 3 |
| CI 新增 | 编译范围加 `worker/`；业务词扫描覆盖 `worker/`；删除模式扫描覆盖 `worker/`；新增"Worker 只走 HTTP"红线；新增"派发载荷落库"检查 |
| run_all | 加入 Batch9 一项 |
| 老脚本适配 | `verify_b7` / `verify_b8` 里写死的"版本=2"改为**跟着迁移目录走**（`runner.latest_available()`），以后加 v004 不用再改验收 |

全量回归（本地）：`ci.py` 全部通过；`run_all.py` 8 项全绿（Batch2/3/4/5/7/8/9 + 冒烟）。

---

## 六、怎么自己跑一遍

```bash
# 只跑这一批（约 30 秒，含起服务与两次 Worker 子进程）
python scripts/verify_b9.py

# 全量
python scripts/run_all.py
python scripts/ci.py
```

手工体验真链路（两个终端）：

```bash
# 终端 1：起平台
python -m uvicorn app.main:app --port 8000

# 终端 2：起 Worker（常驻，2 秒一轮）
python worker/agent_worker.py --base-url http://127.0.0.1:8000 --worker-id workbuddy

# 终端 3：以 Master 身份派活
curl -X POST http://127.0.0.1:8000/api/messages -H 'Content-Type: application/json' -d '{
  "trace_id":"t-demo","task_id":"t-demo-1","msg_type":"task_dispatch","sender":"master",
  "timestamp":"2026-11-19T16:00:00+08:00","version":"v1.0",
  "payload":{"target_worker":"workbuddy","task_goal":"跑个样例",
    "exec":{"script":"worker/examples/hello_task.py","args":["--label","demo"],
            "artifacts":["artifacts/hello.txt"]}}}'
```

看板 <http://127.0.0.1:8000/> 上任务会依次走 执行中 → 待验收，`artifacts/hello.txt` 是真写出来的文件。

---

## 七、本批没做（如实交接）

| # | 未做 | 为什么 |
| --- | --- | --- |
| 1 | 越权强校验（heartbeat/return 的 `sender` 必须等于 `task.target_worker`） | 09 契约写了"只碰自己的 task_id"，但当前平台只校验 trace 同源。加上会影响 b2/b4/b5 的报文构造，属独立小项，建议单独一批做（Batch10） |
| 2 | Worker 常驻守护（systemd / launchd / Windows 服务） | 本批只做"能真跑通"，进程托管属部署层 |
| 3 | 多任务并发 / 优先级 | 当前 Worker 串行处理轮询到的任务；并发度与优先级需要先定调度策略 |
| 4 | `payload.exec` 之外的执行形态（HTTP 调用、容器任务） | 先把最小可用的"跑脚本"跑通，其它形态按需加规格 |
| 5 | 验收判定（产出质量） | 属 Master 职责，平台与本批都不碰 |
