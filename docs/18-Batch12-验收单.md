# 18 · Batch12 验收单 —— 执行方在线视图 / 弹性并发

> 目标：运维能看见"谁在干活、谁静默了"，同时系统在积压多时自动把并发打开、积压少时自动收回来。
>
> 验收：`python scripts/verify_b12.py` → **全部通过（0 项失败）**
> 回归：`run_all.py` / `ci.py` 全绿（11 个套件 + 冒烟）

---

## 一、关键取舍：在线视图不加接口

"哪些 Worker 在线"最自然的做法是加一个 `GET /api/workers`。但那会破掉从 Batch1 起就立着的硬约束：

- `ci.py` 断言 `len(paths) == 9`；
- 文档里写死了 9 个接口、方法只有 GET/POST。

所以本批换了条路：**用任务心跳反推**。

| 做法 | 代价 |
|---|---|
| 加 `/api/workers` + Worker 注册表 | 精确，但要改约束、改 9 份文档、CI 门槛重做 |
| **（本批）台账记 `last_heartbeat_time`，按 `target_worker` 聚合** | 不需新接口；缺点见下 |

**这个方案的代价，要说清楚**：

1. 只在"有任务"的执行方身上有数据——一个 Worker 空转、没领到活时，看板显示的是"它上次干活时的最后心跳"，会被判「静默」。要精确知道"空闲但活着"必须加注册表接口。
2. 聚合只取最近 100 条任务（`size` 上限），任务特别多时需要翻页或按 worker 过滤。
3. 阈值 120 秒写在前端（`WORKER_ONLINE_MS`），与平台 `heartbeat_timeout_seconds` 默认值同口径；改配置要同步改前端常量。

如果你要"精确在线列表"，那就得开一个接口——这是下一批的选择题，不是这批悄悄加进去的东西。

---

## 二、平台侧改动（1 列 + 1 处写入 + 1 个字段）

| 位置 | 改动 |
|---|---|
| `migrations/versions/v004_task_last_heartbeat.py` | 加列 `task_ledger.last_heartbeat_time DATETIME`（`inspect` 幂等，可空，不做删除） |
| `app/services/dispatch.py` → `handle_task_heartbeat` | 归档后调 `repo.mark_heartbeat(task_id, envelope.timestamp)`；**不改任务状态** |
| `app/repositories/ledger.py` | 新增 `mark_heartbeat()` |
| `app/api/schemas_http.py` + `routes_messages._task_view` | `TaskView.last_heartbeat_time`，序列化成**带时区**的 ISO（`ensure_utc`），前端能直接 `Date.parse` |

没有新接口、没有新表、状态机不变。

---

## 三、看板「执行方」面板

首屏第一个面板，数据来自已有的 `GET /api/tasks?page=1&size=100`：

| 列 | 含义 |
|---|---|
| 执行方 | `target_worker` |
| 任务总数 / 在跑 / 待验收 / 失败·驳回 | 按状态计数 |
| 最近心跳 | 该执行方所有任务里最新的 `last_heartbeat_time` |
| 状态 | 心跳在 120s 内 → 「在线」；有历史心跳但超时 → 「静默」；从没心跳 → 「未知」 |

前端是纯 fetch + 差量渲染那套的延续，零 CDN、无 `setInterval`（沿用 `setTimeout` 排期），`b5` 前端门禁照常通过。

---

## 四、弹性并发

`--concurrency` 从"固定值"变成**上限**，新增 `--min-concurrency`（默认 1）作为下限：

```text
每轮：backlog = 待办 + 在跑
      limit   = clamp(backlog, min_concurrency, concurrency)
      slots   = limit - 在跑
      提交 min(slots, 待办数) 个
```

- 积压 4 个、`--concurrency 4 --min-concurrency 1` → 并发打开到 4，4 个 3s 任务 **4.4s** 跑完（实测）
- 只剩 1 个任务 → `limit=1`，状态文件里 `limit` 回落（实测）
- 上限仍硬：到达 `concurrency` 时连轮询都跳过，不会偷偷超发

---

## 五、本批发现的真 Bug（重要）

**Batch10 的重放拦截把心跳拦死了。**

Batch10 的重放指纹是 `sender + payload`（故意忽略 timestamp，防"改时间戳重发驳回反复扣重试次数"），并把 `task_heartbeat` 也列进了 `REPLAY_GUARDED_TYPES`。问题是：

> Worker 每 2~5 秒发一条心跳，payload 往往是**一模一样**的（`{"progress": 0.5, "message": "正在执行 xxx"}`）。

于是第二条起全部命中"重放"，202 返回、**不落库、不刷新心跳**。Batch10 当时的用例 `F` 还专门断言了这个行为（"重放拦截同样作用于 heartbeat"）——断言本身把 bug 固化成了"正确行为"。

本批把它纠正：

| 项 | 改动 |
|---|---|
| `REPLAY_GUARDED_TYPES` | 去掉 `task_heartbeat`，只护 `task_return / task_reject / task_abort` |
| 心跳的重复投递 | 交给幂等键（`trace_id+task_id+msg_type+timestamp`）：同时间戳才忽略，换时间戳照常受理 |
| `verify_b10` F 段 | 改成断言"同内容新时间戳的心跳照常受理 + 归档 +1"，并补"同时间戳被幂等挡" |
| `ci.py` | 新增红线：`task_heartbeat` 不得出现在 `REPLAY_GUARDED_TYPES` 里 |

这是"两批各自正确、合起来出错"的典型：如果不做在线视图，谁也不会发现 Worker 的心跳其实只落了第一条。

---

## 六、验收清单

| # | 断言 | 结果 |
|---|---|---|
| A1 | 干净库升级到 v4 | PASS |
| A2 | `task_ledger` 有 `last_heartbeat_time` | PASS |
| A3 | 重复升级幂等 / `migrate --check` 通过 | PASS |
| B1 | 执行中 `GET /api/tasks/{id}` 有 `last_heartbeat_time` | PASS |
| B2 | 两次观测都在执行中，心跳时间**前进** | `…40.875 → …43.025` |
| B3 | 心跳时间是带时区 ISO | `+00:00` |
| B4 | 心跳不改状态，跑完收敛「待验收」 | PASS |
| C1 | 列表项带该字段；聚合能算在跑/待验收/最近心跳 | PASS |
| C2 | 接口数 **仍 9**，没有 `/api/workers` | 9 个 |
| C3 | 看板有「执行方」面板 + 聚合函数 + 120s 阈值 | PASS |
| D1 | 4 个 3s 任务并发 4.4s 完成，峰值 `in_flight=4`，不超上限 | PASS |
| D2 | 只剩 1 个任务时 `limit` 回落到 1 | PASS |

---

## 七、本批没做

| # | 未做 | 需要什么 |
|---|---|---|
| 1 | 精确 Worker 在线列表（空转也算在线） | 新增 `GET /api/workers` + 注册表；要动 9 接口约束，需你点头 |
| 2 | 空闲 Worker 保活心跳（无任务也发心跳） | 同上，得有"无任务心跳"的语义与落点 |
| 3 | 按执行方过滤任务（`GET /api/tasks?target_worker=` 已支持，但看板没做入口） | 前端加个下拉即可，属体验优化 |
| 4 | 并发上限的运行时热调整 | 现在改并发要重启 Worker（改参数） |
