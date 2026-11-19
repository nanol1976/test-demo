# 20 · Batch14 验收单 —— 多实例标识（`worker_id` + `instance_id`）

> Batch13 的注册表拿 `worker_id` 当唯一键：同一 `worker_id` 的两台机器互相覆盖 `last_seen`，
> 谁后报谁算"在线"，另一台被顶成静默，看板上分辨不出来。
>
> 验收：`python scripts/verify_b14.py` → **全部通过（0 项失败）**
> 回归：`run_all.py` **13 项全绿**（Batch2~14 + 冒烟）；`ci.py` 全绿
> 接口面：**仍 10 个**（多实例没再开接口，只改了主键与字段）

---

## 一、改了什么

| 项 | 变化 |
|---|---|
| 主键 | `worker_id` → **`(worker_id, instance_id)`**（迁移 v006，SQLite 走重建表） |
| 字段 | 新增 `instance_id`；老数据统一补 `'default'`，**一行不丢** |
| 接口 | `POST /api/workers` 接受 `instance_id`（默认 `default`，兼容老 Worker）；`GET /api/workers` 新增 `?worker_id=` 过滤 |
| Worker | 新增 `--instance-id`，**默认取主机名**（同机重启沿用同一行，不堆垃圾行） |
| 看板 | 「执行方」面板加「实例」列，标题改成"在线 N / 共 M 个实例" |

---

## 二、顺手修掉的隐藏坑：`limit` 是 SQL 关键字

Batch13 的 v005 里写了 `limit INTEGER NOT NULL DEFAULT 1` —— `limit` 是 SQLite 保留字，**裸写在 DDL 里直接语法错误**。

它当时没炸，是因为建表被模型抢先做了（模型生成 DDL 时会给关键字加引号），v005 检查到表已存在就空转了。也就是说：**v005 其实从没真正执行过建表**，只是被掩盖住了。

本批把物理列名改成 `concurrency_limit`：

| 位置 | 处理 |
|---|---|
| 模型 | `limit: Mapped[int] = mapped_column("concurrency_limit", Integer, ...)` —— API 字段还叫 `limit`，前端不用改 |
| v005 | 同步改成 `concurrency_limit`（新库直接建对） |
| v006 | 重建时自动识别老列名：`limit` 或 `concurrency_limit`，都能把值带过来 |

`verify_b14` 专门造了一个"物理列叫 `limit`"的怪库，验证升级后 `instance_id=default`、`handled=8`、`concurrency_limit=3` 都在。

---

## 三、验收清单

| # | 断言 | 结果 |
|---|---|---|
| A1 | v5 → v6 升级成功，老行保留并补 `instance_id='default'` | PASS |
| A2 | 主键是复合键 `(worker_id, instance_id)` | PASS |
| A3 | 重复升级幂等 / `--check` 通过 | PASS |
| A4 | 旧 `limit` 列形态也能升级，值被带到 `concurrency_limit` | `('default', 8, 3)` |
| B1 | 同 `worker_id` 两个实例各一行，`instance_id` 独立 | PASS |
| B2 | `host-b` 再报不改 `host-a` 的 `last_seen`/`handled` | PASS |
| C1 | `?worker_id=` 只回该执行方；`?online=true` 排除已停机实例 | PASS |
| C2 | `instance_id` 超长 → 422 | PASS |
| D1 | 真 Worker 默认实例名 = 主机名 | PASS |
| D2 | 同机两 Worker（`--instance-id extra-1`）**同时在线** | PASS |
| E1 | 接口路径仍 10 个，方法只有 GET/POST | PASS |
| E2 | 看板有「实例」列 | PASS |

---

## 四、没做的

| # | 未做 | 说明 |
|---|---|---|
| 1 | 实例级凭证 | 仍共享一个 `X-API-Token`；`worker_id/instance_id` 都是自称，不能当身份用 |
| 2 | 僵尸行清理 | 只增不删；换机器名后会多一行"停机"记录，属于可接受的可观测性噪音 |
| 3 | 看板按实例过滤任务 | `GET /api/tasks` 只按 `target_worker` 过滤，任务不区分实例（任务归属本来就是执行方级别） |
