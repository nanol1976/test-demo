# 09 · Worker 契约

Worker 是执行层。本文件定义 Worker 怎么接活、怎么报活、怎么交活、怎么报错。

---

## 一、你的三个动作

1. **接活**：收到属于你的 `task_dispatch`（`target_worker` 是你），任务状态转「执行中」。
2. **报活**：执行期间按周期发 `task_heartbeat`，让平台知道你还活着。
3. **交活**：做完发 `task_return`；做不了发 `task_reject`；被叫停发 `task_abort`。

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

1. **只碰自己的 task_id**。带别人的 task_id 发报文，平台会 409/404；试图改别的任务状态等于越权。
2. **必须带 trace_id，且要与任务原属 trace 一致**。平台用 `_require_same_trace` 校验，不一致 409。
3. **心跳要持续**。平台默认 `heartbeat_timeout_seconds=120`；超了只写 `[心跳告警]` 留痕，**不会杀你的任务**，但 Master 会看到告警并可能决定中止。
4. **做不了要明说**。发 `task_reject` 并给 `reject_reason`，不要硬扛到超时——超时会被判 `执行失败`，比主动驳回更难看。

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

## 七、重试

被驳回后 Master 可能重派同一 task_id，`remaining_retry` 会减 1。你不需要自己管次数，但要保证**重派时能接着做**（幂等：别把上一次的半成品重复叠加）。

`remaining_retry` 扣到 0 后不会变负数，也不会再允许派发。
