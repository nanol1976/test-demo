# 09 · Worker 契约

Worker 是执行层。本文件定义 Worker 怎么接活、怎么报活、怎么交活、怎么报错。

---

## 一、你的三个动作

1. **接活**：收到属于你的 `task_dispatch`（`target_worker` 是你），任务状态转「执行中」。
2. **报活**：执行期间按周期发 `task_heartbeat`，让平台知道你还活着。
3. **交活**：做完发 `task_return`；做不了发 `task_reject`；被叫停发 `task_abort`。

## 一·补 怎么"接活"：轮询（Batch9 落地方式）

平台不主动推给你——**你自己去平台取**。Sandbox 里只要两个 HTTP 动作就够，不需要任何额外接口：

```http
GET /api/tasks?status=执行中&target_worker=<我>      # 接活：拿派给我的、正在等我干的任务
POST /api/messages                                    # 报活 / 交活 / 报做不了
```

响应里的每个任务带 `payload`（派发报文原样搬运），这就是"要干什么"的来源：

```json
{
  "task_id": "t-demo-1",
  "trace_id": "t-demo",
  "target_worker": "workbuddy",
  "status": "执行中",
  "dispatch_time": "2026-11-19T08:00:00",
  "payload": {
    "task_goal": "跑个样例",
    "target_worker": "workbuddy",
    "exec": { "script": "worker/examples/hello_task.py", "args": ["--label", "demo"] }
  }
}
```

三条要点：

1. **`target_worker` 取 `payload.target_worker`**（缺省回退派发报文 `sender`）。Master 派活儿时把 `payload.target_worker` 写成你的名字，任务才会进你的队列。
2. **轮询只按自己的名字筛**，别去捞全表——别人的活不该出现在你的队列里。
3. **拿到就跑，跑完就回执**；平台侧状态是「执行中」，你不回执它永远停在那儿。

参考实现：`worker/agent_worker.py`（纯标准库，可直接用，也可照着自己写）。

## 一·补二 执行规格 `payload.exec`（可选约定）

平台**不解释**这个字段，只搬运；怎么执行是 Master 与 Worker 之间的约定。参考实现认这一种：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `script` | string | 必填，允许目录（`--allow-root`）内的 `.py`；越界/非 `.py`/不存在 → 直接 `task_reject` |
| `args` | string[] | 可选，原样作为位置参数 |
| `timeout_seconds` | number | 可选，只能比 Worker 全局上限更短 |
| `artifacts` | string[] | 可选，跑完回传这些相对路径（含是否存在、字节数） |

安全边界（参考实现强制）：只跑白名单目录内的 `.py`、拒绝 `..` 越界、拒绝拼 shell、单任务硬超时。

## 二、报文格式（与 Master 同协议）

```json
{
  "trace_id": "<从派发报文原样带回>",
  "task_id": "<你的任务 id，原样带回>",
  "msg_type": "task_heartbeat",
  "sender": "workbuddy",
  "timestamp": "<aware ISO 时间>",
  "version": "v1.0",
  "payload": { "progress": "50%" }
}
```

`sender` 只能是 `master` / `dsh` / `workbuddy` / `codex` / `user` 之一——你是谁就写谁，**不许冒充**。

## 三、四条硬规矩

1. **只碰自己的 task_id（平台已强校验，Batch10 起生效）**。`task_heartbeat` / `task_return` / `task_reject` 的 `sender` 必须等于该任务的 `target_worker`，否则 **409 且不落归档**；`task_abort` 只有两类人可以发：任务归属者本人，或 Master（叫停权）。派发（`task_dispatch`）只有 Master 能发，Worker 派活一律 409。
2. **必须带 trace_id，且要与任务原属 trace 一致**。平台用 `_require_same_trace` 校验，不一致 409。
3. **心跳要持续**。平台默认 `heartbeat_timeout_seconds=120`；超了只写 `[心跳告警]` 留痕，**不会杀你的任务**，但 Master 会看到告警并可能决定中止。
4. **做不了要明说**。发 `task_reject` 并给 `reject_reason`，不要硬扛到超时——超时会被判 `执行失败`，比主动驳回更难看。
5. **别指望改 timestamp 重发能再来一次（Batch10）**。平台按 `sender + payload` 指纹做重放拦截：同内容换时间重发 → 202 幂等忽略，不推进状态、不重复扣重试次数、不追加归档。想让平台当新报文处理，就必须真的改了内容。

## 四、回传内容要求

`task_return` 的 payload 里应包含：
- **产出位置**（文件路径 / URL / 关键结论）
- **自检结论**（你验过什么、结果如何）
- **已知问题**（没做完的、有风险的，如实写）

平台不判内容质量，但你写清楚，Master 才能判。

## 五、你不需要做什么

- 不用自己算超时（平台算）
- 不用自己写台账（平台写）
- 不用自己判并行/依赖（平台做闸门）
- 不用自己维护心跳表（平台记）

## 六、失败时的标准姿势

| 情况 | 发什么 | payload 关键字段 |
| --- | --- | --- |
| 干完了 | `task_return` | 产出位置、自检结论、已知问题 |
| 干不了 | `task_reject` | `reject_reason`（具体到"缺什么/为什么做不了"） |
| 被叫停 | `task_abort` | `abort_reason` |
| 还活着 | `task_heartbeat` | `progress`（可粗略） |

## 六·补 回执里该写什么（Batch9 参考实现的字段）

`task_return` 的 `payload` 现成模板：

```json
{
  "output": {
    "artifacts": [{"path": "/abs/path/artifacts/hello.txt", "exists": true, "size_bytes": 210}],
    "exit_code": 0,
    "duration_ms": 82,
    "script": "worker/examples/hello_task.py"
  },
  "logs": "written: /abs/path/artifacts/hello.txt",
  "metadata": {
    "self_check": "脚本正常退出（退出码 0）；产出路径已核验存在",
    "known_issues": "",
    "heartbeats": 2,
    "worker_id": "workbuddy",
    "finished_at": "2026-11-19T08:00:00+00:00"
  }
}
```

`task_reject` 的 `payload.reason` 要写成"下次能据此改"的话，例如
`exec 规格不合法：脚本越界（不在允许目录内）：/tmp/x/escaping_task.py`、
`执行失败（退出码 3）：boom: 故意失败`。

## 六·补二 并发、优先级与停止（Batch11）

| 项 | 口径 |
| --- | --- |
| 并发 | `--concurrency N` 同时执行 N 个任务；槽位满了就停止拉新活，不堆积 |
| 优先级 | `payload.priority`（数字，越大越先）；同优先级按 `dispatch_time` 升序、再按 `task_id` 兜底。**平台只搬运不排序**，排序是实现方的事 |
| 停止 | 收到 `SIGTERM`/`SIGINT` → 不再拉新活 → 等在跑任务收尾（`--grace-seconds`，默认 30）→ 退出 0。**不要在主流程里杀掉子进程**，否则任务会留在「执行中」等平台判超时 |
| 被强杀 | `SIGKILL` 时任务留在「执行中」，由平台 `task_timeout_seconds` / `heartbeat_timeout_seconds` 处置；不会被伪造成成功或失败 |
| 状态可观测 | `--status-file` 每轮落一个小 JSON（`worker_id`/`updated_at`/`concurrency`/`in_flight`/`handled`/`poll`），守护脚本与运维直接读它 |

## 七、重试

被驳回后 Master 可以重派同一 task_id（Batch9 起「验收驳回」可直接重派），`remaining_retry` 在驳回当时已减 1，重派不再重复扣；扣到 0 后任务转「执行失败」，不能再派。你不需要自己管次数，但要保证**重派时能接着做**（幂等：别把上一次的半成品重复叠加）。

`remaining_retry` 扣到 0 后不会变负数，也不会再允许派发。
