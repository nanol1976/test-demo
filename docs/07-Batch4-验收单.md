# Batch4 验收单：超时 / 心跳告警 / 熔断 / 暂停恢复 / DAG 依赖

- 执行方：主审代 Codex 落地，Codex 保留复核权
- 执行机：Windows DESKTOP-C1V00J3，`.venv311`（Python 3.11）
- 执行日期：2026-11-19
- 验收脚本：`scripts/verify_b4.py`（独立库与归档目录；时间用 `FakeClock` 推进，不靠 sleep）

## 一、交付物

| # | 文件 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | `app/core/clock.py` | 新增 | `Clock` / `SystemClock` / `FakeClock` + `ensure_utc`（SQLite 取回的 naive 时间统一按 UTC 解释） |
| 2 | `app/services/breaker.py` | 新增 | 熔断登记表（进程内，线程安全）：trip / is_open / reason / reset |
| 3 | `app/services/scheduler.py` | 新增 | `run_once()` 一轮判定（纯函数式，可被测试直接调用）+ `Scheduler` 后台线程宿主 |
| 4 | `app/api/routes_commands.py` | 新增 | `POST /api/commands`：`master:pause` / `master:resume` / `master:clear-breaker` |
| 5 | `app/services/dispatch.py` | 修改 | 派发前三道闸门：暂停 → 熔断 → 依赖未满足 |
| 6 | `app/repositories/ledger.py` | 修改 | 新增 `list_tasks_by_status` / `last_heartbeat_at` / `count_tasks_by_status_in_trace` / `set_pause` / `list_pipelines_paused` |
| 7 | `app/core/config.py` | 修改 | task_timeout / heartbeat_timeout / failure_threshold / scan_interval |
| 8 | `app/main.py` | 修改 | lifespan 启动并停止 Scheduler，挂 `routes_commands` |
| 9 | `scripts/verify_b4.py` | 新增 | 6 组 44 条断言 |
| 10 | `docs/07-Batch4-验收单.md` | 新增 | 本文件 |

## 二、判定语义（先定死，避免含糊）

| 机制 | 触发条件 | 动作 | 是否改状态 |
| --- | --- | --- | --- |
| 派发超时 | `执行中` 且 `now - dispatch_time > task_timeout_seconds`（默认 3600s） | 写 `[超时]` 原因 + 置 `执行失败` | **改** |
| 心跳告警 | `执行中` 且 `now - 最后心跳(无心跳则 dispatch_time) > heartbeat_timeout_seconds`（默认 120s） | 只在 `abort_reason` 写 `[心跳告警]` | **不改**，杀不杀由 Master 定 |
| 熔断 | 同一 trace `执行失败` 任务数 ≥ `failure_threshold`（默认 3） | pipeline 置 `failed` + 熔断表打开，后续派发 409 | **改**（pipeline） |
| 暂停 | `master:pause` 指令 | `pause_flag=True`，后续派发 409 | **改**（pause_flag） |
| 恢复 | `master:resume` | `pause_flag=False` + 复位熔断 | **改** |
| DAG 依赖 | `depends_on` 中任一任务不存在或状态 ≠ `验收通过` | 派发 409，列出未满足项 | **不改** |

三条原则：判定只扫「执行中」任务；超时对同一任务只处理一次（幂等）；拒绝派发一律不落任何状态。

## 三、真实输出（Windows 实跑）

```
=== A 派发超时判定 ===
  [PASS] 派发 202 — status=202
  [PASS] 派发后状态执行中 — 执行中
  [PASS] 40s 未超时（阈值 60s） — []
  [PASS] 状态仍执行中 — 执行中
  [PASS] 70s 判超时 — ['t-b4-a-1']
  [PASS] 超时后状态执行失败 — 执行失败
  [PASS] abort_reason 标注 [超时] — [超时] 调度后超过 60s 未回传（判定时刻
  [PASS] 超时只处理一次（幂等） — []
=== B 心跳超时告警（只告警不改状态）===
  [PASS] 派发 202 — status=202
  [PASS] 心跳 202 — status=202
  [PASS] 距最后心跳 20s（阈值 30s）不告警 — []
  [PASS] 距最后心跳 40s 触发告警 — ['t-b4-b-1']
  [PASS] 告警后状态不变（仍执行中） — 执行中
  [PASS] abort_reason 标注 [心跳告警] — [心跳告警] 超过 30s 无心跳（最后心跳
=== C 熔断 ===
  [PASS] 失败 3 个达阈值触发熔断 — ['t-b4-c']
  [PASS] 熔断表已打开
  [PASS] pipeline 置 failed — failed
  [PASS] 熔断后新派发 → 409 — status=409
  [PASS] 409 提示含熔断 — 已熔断（连续失败任务数 3 ≥ 阈值 3）
=== D 暂停与恢复 ===
  [PASS] 先正常派发 202 — status=202
  [PASS] pause 200 — status=200
  [PASS] paused=true
  [PASS] 暂停期间派发 → 409 — status=409
  [PASS] 409 提示含暂停 — 处于暂停状态，需先 resume
  [PASS] resume 200 — status=200
  [PASS] paused=false
  [PASS] 恢复后派发 202 — status=202
  [PASS] 未知指令 → 400 — status=400
  [PASS] 不存在 trace → 404 — status=404
=== E DAG 依赖判定 ===
  [PASS] 上游派发 202 — status=202
  [PASS] 依赖未满足（上游执行中）→ 409 — status=409
  [PASS] 409 列出未满足依赖 — 依赖未满足 ['t-b4-e-a(执行中)']
  [PASS] 依赖满足后派发 202 — status=202
  [PASS] 下游任务执行中 — 执行中
=== F 收尾 ===
  [PASS] health 200
  [PASS] 无 DELETE / PUT / PATCH — ['GET', 'POST']
  [PASS] 接口总数 9
  [PASS] commands 有暂停指令说明
结果：全部通过
```

时间维度全部用 `FakeClock` 推进（40s→70s、20s→40s），没有一处 `sleep`，所以这批的时序结论可复现，不是"跑过了"。

## 四、自测中修掉的问题

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 所有派发返回 500：`wrapper() takes 2 positional arguments but 4 were given` | 加 `_require_dispatch_gate` 时装饰器 `@_idempotent` 被挂到了这个 4 参数辅助函数上，反而 `handle_task_dispatch` 丢了幂等装饰 | 把装饰器移回 `handle_task_dispatch` 上方 |
| 2 | B3 回归失败：`5 个只读/写入接口齐备` | B3 脚本把"接口总数 == 8"写死了，本批新增 `/api/commands` 变 9 | 改为断言"必需接口集合"包含，不再锁数量 |

## 五、回归

- `verify_b2.py`：**全部通过**
- `verify_b3.py`：**全部通过**（断言修正后）

## 六、监理侧扫描

- 业务词 0 命中；删除类调用 0 处（排除注释）；方法集合仍只有 GET / POST
- 判定线程异常不会自毁：`Scheduler._loop` 捕获异常后继续下一轮
- `run_once()` 与线程解耦：验收直接调用函数，不依赖后台线程是否跑起来

## 七、遗留（诚实列出）

1. **熔断状态在进程内存**：重启即复位。要做持久化熔断需在台账加字段（涉及迁移），本批不做，等实际需要再加。
2. **心跳超时不杀任务**：只告警。这是刻意保守——杀死任务属 Master 决策，平台不越权。
3. **DAG 只做"依赖满足才允许派发"**，不做拓扑排序与并行度控制（并行度属 Batch5/调度器增强）。
4. Codex 复核仍欠一轮（配额）。
5. Batch5 建议范围：极简前端（台账看板 + 暂停/恢复按钮 + 报文投递面板），把前四批的接口串成人能用的界面。
