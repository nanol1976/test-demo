# Batch18 验收单 —— 审计覆盖全部运维指令 + 只读查询接口（10 → 11）

## 范围

1. `master:pause` / `master:resume` / `master:clear-breaker` 也落审计（同一张 `worker_quota_audit`，**不改结构**）
2. 新增只读 `GET /api/audit` —— **接口面 10 → 11**（用户明确同意）
3. 看板新增「运维审计」面板：最近 20 条 + **是否生效**

## 数据表达（不改结构的前提）

流水线指令没有"实例"概念，审计里统一写成：

| 列 | 值 |
| --- | --- |
| `worker_id` | `master` |
| `instance_id` | `trace:<trace_id>` |
| `desired_limit` / `prev_limit` | 0（不涉及配额） |
| `command` | 三条指令之一 |

## `effective` 语义（只对最新一条配额下发有意义）

| 情形 | effective |
| --- | --- |
| 期望值已落库，且 Worker 自报 `concurrency` 等于它 | `true` 已生效 |
| 期望值已落库，但 Worker 还没按它跑 | `false` 未生效 |
| 期望值为 0（撤销） | `true` 已生效 |
| 被更晚的下发覆盖 | `null`（历史行） |
| 流水线指令（暂停/恢复/复位） | `null`（没有生效概念） |

## 验收（`scripts/verify_b18.py`）

| 组 | 覆盖 |
| --- | --- |
| A | 三条流水线指令各一行；目标写成 `master/trace:<trace_id>`；原因落库；effective 为 null；items 倒序 |
| B | `meta.total/shown/limit`、`current` 注册表快照；过滤参数（worker_id / instance_id / command）生效 |
| C | 下发 3 但自报 2 → `false`；自报改 3 → 同一条翻 `true`；再下发 4 → 旧行 `null`、新行 `false`；撤销 0 → `true` |
| D | 接口 11 个；`/api/audit` 只有 GET；源码无 `db.add/db.commit/delete` |
| E | 看板有审计面板；前端有 loadAudit 与生效判定 |

## 门禁

`ci.py` 新增 3 条（接口文件在、只 GET 只读、流水线指令也落审计、看板面板在），接口数断言统一从 10 改为 11（涉及 ci、smoke_http、verify_b4/b9/b10/b12/b13/b14/b15/b16）。`run_all.py` 纳入 Batch18（17 套件）。

## 未做

- 审计面板没有分页，只显示最近 20 条（接口支持 `limit` 最大 200）。
- 没有"谁下的指令"这一列——当前只有 `_require_owner` 的 token 校验，没有操作者身份。要做需要先引入身份，单开一批。
