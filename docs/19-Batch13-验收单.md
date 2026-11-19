# 19 · Batch13 验收单 —— 执行方注册表 / 精确在线

> 唯一一次动接口面的批次：9 → 10（新增 `GET/POST /api/workers`）。
> 理由：Batch12 的在线视图靠任务心跳反推，**空转但活着的 Worker 看不见**；要看见它，必须有 Worker 主动上报的落点。
>
> 验收：`python scripts/verify_b13.py` → **全部通过（0 项失败）**
> 回归：`run_all.py` **12 项全绿**（Batch2~13 + 冒烟）；`ci.py` 全绿

---

## 一、接口面的变化，明码标价

| 之前 | 现在 |
|---|---|
| 9 个路径 | **10 个路径**：`/api/health`、`/api/messages`、`/api/pipelines/{trace_id}`、`/api/tasks/{task_id}`、`/api/archive/{trace_id}`、`/api/pipelines`、`/api/tasks`、`/api/export/{trace_id}`、`/api/commands`、**`/api/workers`** |
| 方法 GET/POST | 不变（`/api/workers` 一个路径挂 GET + POST） |

`ci.py` 的计数方式也修了：原来数 `@router.get/post` 装饰器个数（同一路径两方法会数成 2），现在按**去重路径**数，断言 `== 10`。

同批把所有旧断言（`verify_b4/b9/b10/b12`、`smoke_http`）从 9 改成 10——**不是放宽，是跟着契约走**，并在各自的说明里注了"Batch13 起"。

---

## 二、`POST /api/workers`（上报）

请求体都可缺省，缺省按"还在跑、并发 1"理解：

```json
{
  "worker_id": "workbuddy",
  "status": "running",
  "concurrency": 4,
  "limit": 2,
  "in_flight": 2,
  "handled": 7,
  "note": "可选备注",
  "meta": {"pid": 1234, "min_concurrency": 1}
}
```

| 校验 | 结果 |
|---|---|
| `worker_id` 不在白名单（master/dsh/workbuddy/codex/user） | **409** |
| `status` 不是 running/stopping/stopped | **422** |
| 缺 `worker_id` | 422 |
| 正常上报 | **202**，返回 `online` / `age_seconds` 等算好的状态 |

upsert 语义：同一 `worker_id` 永远只有一行，`first_seen` 保持不动（能看出"从什么时候开始在线"），`last_seen` 每次刷新。

> 注意：**注册表是可观测性，不是权限依据**。谁都能说自己是 workbuddy（只要在白名单里），所以权限判定仍然只看任务归属 `_require_owner`。这一点写进了 03-权限文档，避免以后有人拿注册表当身份凭证。

---

## 三、`GET /api/workers`（列表）

```
GET /api/workers            全量（含静默/停机，方便看"谁掉了"）
GET /api/workers?online=true 只看在线的
```

每条含：`worker_id / status / online / last_seen / first_seen / age_seconds / concurrency / limit / in_flight / handled / note / meta`。

在线判定在平台侧算完再给前端，不在 JS 里各算一套：

```
online = (status == "running") and (now - last_seen <= heartbeat_timeout_seconds)
```

`stopping` / `stopped` 一律不算在线——Worker 自己说"我在收尾/我停了"，比心跳超时更准。

---

## 四、Worker 侧

| 参数 | 默认 | 行为 |
|---|---|---|
| `--registry-interval` | 15s | 启动立刻报一次，之后每 N 秒报一次；忙闲都报 |
| `--no-register` | 关 | 不上报（老平台没有该接口、或不想被记录时用） |

- 上报内容取自运行态快照（`in_flight` / `limit` / `handled`），与 `--status-file` 同源，不另算一份
- 收到 `SIGTERM` → 先报 `stopping` → 等任务收尾 → 退出前报 `stopped`
- 平台返回 **404**（没有该接口）时只记一条 `registry_unsupported`，**继续正常领活/回传**，不因上报失败罢工

---

## 五、看板

「执行方」面板优先读 `GET /api/workers`：

| 列 | 内容 |
|---|---|
| 执行方 | worker_id |
| 状态 | 在线 / 静默 / 收尾中 / 停机 |
| 在跑 | `in_flight` |
| 并发 | `limit/max`（弹性并发当时开到多少） |
| 累计完成 | `handled` |
| 最近上报 | `last_seen` |

平台没有该接口时自动退回 Batch12 的任务聚合逻辑（`aggregateWorkers`），旧部署不会报错。面板标题会显示"在线数/总数"。

---

## 六、验收清单

| # | 断言 | 结果 |
|---|---|---|
| A1 | v005 建 `worker_registry` 表；日志 `0 → 5` | PASS |
| A2 | 重复升级幂等；`--check` 通过 | PASS |
| B1 | 注册 202，返回 `online=true`，字段原样回显，`meta` 解析成对象 | PASS |
| B2 | 重复上报是 upsert：表里**只有一行**，`first_seen` 不变 | PASS |
| B3 | 非法 worker_id → 409；非法 status → 422；缺字段 → 422 | PASS |
| C1 | 列表含 `online/age_seconds/last_seen`；`?online=true` 只回活的 | PASS |
| C2 | `last_seen` 推到 10 分钟前 → `online=false`（`age=600s`） | PASS |
| C3 | `stopped` 状态不算在线 | PASS |
| D1 | **空转（没有任何任务）也能在注册表里看到**，`online=true`、`in_flight=0`、`concurrency=2` | PASS |
| D2 | 真跑一个任务后 `handled ≥ 1`，任务收敛「待验收」 | PASS |
| D3 | SIGTERM → 状态变 `stopped`、`online=false` | PASS |
| E1 | 路径 10 个，方法只有 GET/POST | PASS |
| E2 | 前端优先读 `/api/workers`，保留聚合兜底 | PASS |

**D1 就是这批存在的理由**：Batch12 的空转 Worker 在台账里一行都没有，看起来像消失了；现在它照样在列表里，只是 `in_flight=0`。

---

## 七、本批没做

| # | 未做 | 说明 |
|---|---|---|
| 1 | 注册表的清理/归档 | 只增不删（台账铁律），历史停机的 Worker 会一直留在表里；目前看板靠"静默/停机"区分，属可接受 |
| 2 | 多实例同 worker_id | 同 id 的两台机器会互相覆盖 `last_seen`（谁后报谁算）。真要区分得用 `worker_id + instance_id`，属下一批 |
| 3 | 认证级身份 | Worker 现在只靠共享 `X-API-Token`，没有每 Worker 独立凭证；注册表里的 id 不可信 |
| 4 | 看板按执行方过滤任务入口 | 接口已支持 `target_worker` 过滤，前端补下拉即可 |
