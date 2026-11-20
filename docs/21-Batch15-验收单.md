# Batch15 验收单 —— 实例级并发配额下发 + 看板筛选

- **提交**：见 `git log --oneline -1`（`feat(batch15)`）｜**tag**：`batch15`
- **接口面**：**10 个路径不变**，方法仍只有 GET/POST —— 配额走已有的 `POST /api/commands`，不新增路径
- **迁移**：`v007_worker_desired_limit`（只 `ADD COLUMN`，不重建表，老行一行不丢）

## 为什么做

Batch11/12 的弹性并发的上限 `--concurrency` 是**启动参数**。要收放只能改命令行、重启进程——夜间批处理想临时拉高、白天想压低，运维只能去改计划任务再重启，期间在飞任务还得等收尾。

Batch15 把上限变成**运行时可下发**的一条指令，与 Worker 既有上报通道合并（下发值随上报响应回来），代价是零新增接口。

## 数据模型

| 列 | 含义 |
| --- | --- |
| `concurrency` | Worker **自报**的当前上限（我开几个） |
| `desired_limit` | 平台**下发**的期望上限（Batch15）；`0` = 未下发 |

两者分开存，是为了区分两类故障：下发失败（desired 没写进去）vs 下发没生效（desired 有值但 concurrency 没跟上）。

## 指令

```json
{ "command": "master:worker-limit", "worker_id": "workbuddy",
  "instance_id": "DESKTOP-C1V00J3", "limit": 3 }
```

- `limit` 0~64；`0` = **撤销**，Worker 回到自己的 `--concurrency`（用 `local_concurrency` 记住原值，不会被配额永久改写）
- 实例没上报过 → **404**（不凭空建行）
- 缺 `worker_id` / `limit`、越界 → **422**
- 未知指令仍 400；流水线指令缺 `trace_id` 仍 422

## Worker 侧行为

- 每轮注册表上报都读响应里的 `desired_limit`：`>0` 生效（夹在 `min_concurrency..64`），`0` 撤销
- 线程池按硬上限 64 建池、实跑由 `self.concurrency` 闸住 —— 否则上调配额会被池容量卡死
- 下调只影响**下一次拉活**，在飞任务不打断（实测已跑完并正常回执）
- 生效/撤销都打日志 `limit_applied` / `limit_restored`，并写进 status 文件

## 看板

- 执行方面板加：**执行方下拉**、**仅在线**勾选、**实例名关键字**（纯客户端过滤，不打额外请求）
- 新增「期望上限」列 + 「配额下发」输入框/按钮，直接对某个实例下发

## 验收（`scripts/verify_b15.py`）

| 组 | 覆盖 |
| --- | --- |
| A | v6 库 → v7：两行老数据都在、`desired_limit` 默认 0；幂等；`--check` 通过 |
| B | 下发 200 且有回显；`GET /api/workers` 可见；未注册 404；缺参/越界 422；未知指令 400；`limit=0` 撤销 |
| C | 真 Worker：初始 4 → 下发 2 → 下一轮上报 2 → 撤销 → 回到 4；下调时进程不被杀，在飞的两个慢任务照常跑完 |
| D | 路径仍 10 个、方法只有 GET/POST；看板筛选控件、「期望上限」列、下发入口在 |

## 门禁

`scripts/ci.py` 新增红线：v007 存在、`desired_limit` 在模型、白名单含 `master:worker-limit`、404 分支在、Worker 应用配额、撤销路径在（`limit_restored`）、线程池按硬上限建、看板筛选入口在。`run_all.py` 纳入 Batch15（14 套件）。

顺带修掉一条过时红线：`Worker 并发用线程池且有上限` 原来硬要求 `max_workers=self.concurrency`，Batch15 改成硬上限建池后假红，已改成接受两种写法。

## 未做

- 没有"配额历史/审计"：目前只保留最新一次下发值，中间下发过几次不留痕。真要审计得单独开张表，属于台账范畴，值得单开一批。
- 看板下发是即时生效，没有二次确认；误下发靠再发一次覆盖。
