# Batch2 验收单：消息总线 + 报文路由

- 执行方：主审（按用户决定代 Codex 落地，Codex 保留复核权）
- 执行机：Windows DESKTOP-C1V00J3，`.venv311`（Python 3.11）
- 时间：2026-11-19
- 验收脚本：`scripts/verify_b2.py`（独立库 `data/verify_b2.db`、独立归档目录 `archive/_verify_b2`）

---

## 一、交付物清单

| # | 文件 | 行数 | 状态 |
| --- | --- | --- | --- |
| 1 | `app/bus/__init__.py` | 132 | 新增 |
| 2 | `app/bus/router.py` | 47 | 新增 |
| 3 | `app/services/dispatch.py` | 187 | 新增 |
| 4 | `app/api/routes_messages.py` | 139 | 新增 |
| 5 | `app/api/schemas_http.py` | 35 | 新增 |
| 6 | `app/main.py` | 37 | 修改（lifespan 启停总线 + 挂路由） |
| 7 | `app/core/config.py` | 14 | 修改（bus_capacity / bus_workers） |
| 8 | `scripts/verify_b2.py` | 250 | 新增 |
| 9 | `docs/05-Batch2-验收单.md` | 本文件 | 新增 |

附加必要改动（任务卡 §三.5 要求说明理由）：
- `app/repositories/ledger.py`：`create_pipeline` / `create_task` 改为 **get-or-create**，捕获 `IntegrityError` 回退为既有记录。理由：并发投递时多线程同时建同一 trace 的流水线会撞主键，实测复现 12 次 500 错误；不修则并发组不可能通过。改动仅新增容错，未引入删除语义。

---

## 二、真实输出（Windows 实跑，逐行粘贴）

```
=== A 主链路：一条 dispatch 报文驱动任务 ===
  [PASS] POST /api/messages 返回 202 — status=202
  [PASS] 返回 archive_id — 1
  [PASS] status_changed=True
  [PASS] pipeline 自动建立 — status=200
  [PASS] pipeline 初始状态 pending — pending
  [PASS] trace 内有 1 个任务 — 1
  [PASS] 任务状态=执行中 — 执行中
  [PASS] dispatch_time 非空
  [PASS] 归档索引 1 行 — 1
=== B 状态迁移全表 ===
  [PASS] heartbeat 202 — status=202
  [PASS] heartbeat 不改状态
  [PASS] task_return 202 — status=202
  [PASS] 回传后=待验收 — 待验收
  [PASS] return_time 非空
  [PASS] task_reject 202 — status=202
  [PASS] 驳回后=验收驳回 — 验收驳回
  [PASS] remaining_retry 3→2 — 2
  [PASS] 驳回原因落库 — 缺关键字段
  [PASS] task_abort 202 — status=202
  [PASS] 中止后=执行失败 — 执行失败
  [PASS] pipeline=aborted — aborted
=== C 非法输入四种 ===
  [PASS] 未知 msg_type → 400 — status=400
  [PASS] 400 未落库
  [PASS] 缺 task_goal → 422 — status=422
  [PASS] naive timestamp → 422 — status=422
  [PASS] 待拆解任务 task_return → 409 — status=409
  [PASS] 409 后状态未变（仍待拆解） — 待拆解
=== D 幂等与归档一致性 ===
  [PASS] 首次 202 — status=202
  [PASS] 重投 202（不报错） — status=202
  [PASS] 两次 archive_id 相同 — 6 vs 6
  [PASS] 索引仍 1 行 — 1
  [PASS] JSONL 仍 1 行 — 1
  [PASS] 20 条全部 202 — 成功 20/20
  [PASS] 索引 20 行 — 20
  [PASS] JSONL 20 行 — 20
  [PASS] 归档接口行数一致 — 20
  [PASS] 归档接口给出 JSONL 路径 — archive\_verify_b2\20261119\t-b2-d20.jsonl
=== E 背压与并发 ===
  [PASS] 容量 2 投 5 条：至少 3 条被明确拒绝 — 拒绝 3 条
  [PASS] 队列内仍保留 2 条（未丢消息） — 2
  [PASS] 队列满时 HTTP 返回 503 — [503, 503, 503, 503, 503]
  [PASS] 503 带 Retry-After 提示（背压路径） — ['1', '1', '1', None, None]
  [PASS] 并发 20 条无异常 — []
  [PASS] 并发索引 20 行 — 20
  [PASS] 并发 JSONL 20 行 — 20
=== F 收尾：健康检查与无删除路径 ===
  [PASS] health 仍 200
  [PASS] 无 DELETE / PUT / PATCH — ['GET', 'POST']
结果：全部通过
```

---

## 三、自测中修掉的 3 个真缺陷（不是一开始就对，留痕在此）

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 同一条 dispatch 重投返回 409 | 状态校验先于幂等判定，第二次投递时任务已是「执行中」被判非法迁移 | 加 `_idempotent` 前置装饰器：同一 `(trace_id,task_id,msg_type,timestamp)` 已归档则直接返回 archive_id、`status_changed=False` |
| 2 | 并发 20 条出现 12 次 500（`UNIQUE constraint failed: pipeline_ledger.trace_id`） | 多线程同时 get(None)→create，抢建同一流水线 | 仓储 `create_pipeline`/`create_task` 改 get-or-create，捕 `IntegrityError` 回退既有记录 |
| 3 | 归档 JSONL 行数翻倍（40 行 / 应为 20） | 验收脚本未清 `archive/_verify_b2`，上一轮残留被累加 | 脚本启动即清库清目录（测试夹具问题，非产品缺陷） |

修正后连续两次实跑均「全部通过」。

---

## 四、监理独立扫描（我另跑的，不在脚本里）

| 检查 | 结果 |
| --- | --- |
| 业务词扫描（量化/回测/股票/订单/客户/基金/持仓） | 0 命中 |
| 删除类调用扫描（`.delete(` / truncate / bulk_update / DROP TABLE） | 0 处真实调用（唯一命中是 `repositories/__init__.py` 里的禁止性注释） |
| 对外接口方法集合 | 仅 `GET` + `POST`；路径 5 个：`/api/health`、`/api/messages`、`/api/pipelines/{trace_id}`、`/api/tasks/{task_id}`、`/api/archive/{trace_id}` |
| 8 种 msg_type handler 注册数 | 8/8 |
| 职责边界 | 无 DAG 拆解、无验收判定逻辑、无审批决策；`human_confirm_req/resp` 只留痕不改状态；`task_reject` 只扣次数不自动重派 |

---

## 五、待办

1. Mac 双端复核：同一份 `scripts/verify_b2.py` 在 Mac 新建 venv 跑，须与本验收单逐行一致（脚本已内置清库清目录，Mac 只需 `mkdir -p data`）。
2. Codex 复核：NIM 配额仍低，待额度宽裕用单文件 bundle 补一次。
3. 本轮新增改动尚未提交 git，复核通过后统一提交并打 tag `batch2`。
