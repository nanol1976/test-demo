# 02 · 六步 SOP（一条需求的标准生命周期）

从"人提需求"到"收尾归档"，固定六步。每一步有唯一负责人，越步即越权。

## 步骤总览

| 步 | 名称 | 负责人 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| 1 | 受理与拆解 | Master | 原始需求 | 任务清单（含依赖、验收标准） |
| 2 | 落流水线 | Master → 平台 | 任务清单 | `pipeline_ledger` 一条 + `task_ledger` N 条（状态=待拆解→待调度下发） |
| 3 | 派发 | Master → 平台 | task_dispatch 报文 | 任务状态 → 执行中 |
| 4 | 执行与自保 | Worker | 任务内容 | task_heartbeat / task_return / task_reject / task_abort |
| 5 | 验收 | Master | task_return 内容 | 验收通过 / 验收驳回（驳回可重试，受 remaining_retry 限制） |
| 6 | 收尾归档 | 平台 | 全流程报文 | JSONL 归档 + 索引行 + pipeline 终态 |

## 每步的硬规则

**第 1 步 拆解**
- 每个任务必须带：`task_goal`（干什么）、`acceptance_criteria`（怎样算合格）、`target_worker`（谁干）。
- 有先后依赖的，写进 `depends_on`；平台只校验"依赖是否已验收通过"，不做拓扑排序。

**第 2 步 落流水线**
- 一条需求 = 一个 `trace_id`。`trace_id` 一旦定，全程不改。
- 初始状态：所有任务 `待拆解`，随后派发前需为 `待调度下发`。

**第 3 步 派发**
- 派发前平台过三道闸门：**暂停 → 熔断 → 依赖**。任一不过直接 409，**不落任何状态**。
- 幂等键 `(trace_id, task_id, msg_type, timestamp)`：同一条报文重投，返回同一 `archive_id`，不重复推进状态。

**第 4 步 执行**
- Worker 必须周期心跳；心跳只用于告警，**平台不会因心跳超时杀任务**（杀不杀是 Master 的决定）。
- 执行超时（`dispatch_time + task_timeout_seconds`）平台判 `执行失败` 并写原因，同一任务只判一次。

**第 5 步 验收**
- 验收判定属 Master；平台只记录结果状态。
- 驳回后重新派发时，`remaining_retry` 减 1，扣到 0 不再允许（不出现负数）。

**第 6 步 收尾**
- 报文全部落 JSONL（先写正文再写索引，撞唯一约束则回退查已有）。
- pipeline 终态三种：`success` / `aborted` / `failed`。

## 异常路径（不进主流程的两种情况）

| 情况 | 平台动作 | 后续 |
| --- | --- | --- |
| 同一 trace 连续失败任务数 ≥ `failure_threshold` | 熔断该 trace，pipeline 置 `failed` | 后续派发 409，需 `master:clear-breaker` 或 `master:resume` |
| 人主动暂停 | `pause_flag=True` | 派发 409，`master:resume` 恢复 |

## 一句话记法

**拆解 → 落库 → 过闸 → 干 → 判 → 归档**。每一步只由一个人负责。
