# Batch3 验收单：台账查询增强 + 导出视图 + 鉴权占位 + 契约固化

- 执行方：主审代 Codex 落地，Codex 保留复核权
- 执行机：Windows DESKTOP-C1V00J3，`.venv311`（Python 3.11）
- 执行日期：2026-11-19
- 验收脚本：`scripts/verify_b3.py`（独立库 `data/verify_b3.db`、独立归档 `archive/_verify_b3`、强制开启 API_TOKEN）

## 一、交付物

| # | 文件 | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | `app/core/security.py` | 新增 | 最小鉴权：`API_TOKEN` 为空则不校验；配置后要求 `X-API-Token`，常量时间比对；健康检查永久放行 |
| 2 | `app/api/routes_queries.py` | 新增 | 3 个只读接口：流水线分页筛选、任务分页筛选、台账导出（json/csv） |
| 3 | `app/api/schemas_http.py` | 修改 | 新增 `PageMeta` / `PipelineSummary(Page)` / `TaskPage` / `ExportPayload` |
| 4 | `app/api/routes_messages.py` | 修改 | 挂鉴权依赖；POST 补幂等键与全部状态码说明（契约固化） |
| 5 | `app/repositories/ledger.py` | 修改 | 新增 4 个只读方法：`list_pipelines` / `count_pipelines` / `search_tasks` / `count_tasks`（含状态白名单校验） |
| 6 | `app/core/config.py` | 修改 | 新增 `api_token`、`page_size_default=20`、`page_size_max=100` |
| 7 | `app/main.py` | 修改 | 挂载 `routes_queries` |
| 8 | `scripts/verify_b3.py` | 新增 | 6 组自检，43 条断言 |
| 9 | `docs/06-Batch3-验收单.md` | 新增 | 本文件 |

## 二、接口变化

新增 3 个只读接口（仍无 DELETE / PUT / PATCH）：

| 方法 | 路径 | 能力 |
| --- | --- | --- |
| GET | `/api/pipelines` | 分页（page/size）+ 按 `status` + 按 `start_from`/`start_to` 时间范围筛选 |
| GET | `/api/tasks` | 分页 + 按 `trace_id` / `status` / `target_worker` 筛选 |
| GET | `/api/export/{trace_id}` | 台账快照导出，`format=json` 结构体、`format=csv` 返回 `text/csv` 附件 |

分页契约：`page` 从 1 开始；`size` 默认 20、上限 100；越界一律 422，不做静默截断。
状态筛选非法值一律 422（复用仓储的状态白名单，不允许绕过 CHECK 约束的取值）。

## 三、真实输出（Windows 实跑）

```
=== A 流水线风水页与筛选 ===
  [PASS] 列表 200 — status=200
  [PASS] total=25 — 25
  [PASS] 首页 10 条 — 10
  [PASS] pages=3 — 3
  [PASS] 末页 5 条 — 5
  [PASS] status=success 命中 5 条 — 5
  [PASS] 非法状态 → 422 — status=422
  [PASS] size 超上限 → 422 — status=422
  [PASS] page=0 → 422 — status=422
=== B 任务筛选 ===
  [PASS] 状态筛选有结果 — 25
  [PASS] 执行方筛选 — 25
  [PASS] trace 筛选命中 1 条 — 1
=== C 导出视图 ===
  [PASS] json 导出 200 — status=200
  [PASS] json 含 tasks — 1
  [PASS] 导出即快照（状态一致） — success
  [PASS] csv 导出 200 — status=200
  [PASS] csv Content-Type 为 text/csv — text/csv; charset=utf-8
  [PASS] csv 表头 + 1 数据行 — 2
  [PASS] csv 含关键列 — trace_id,task_id,target_worker,status,di
  [PASS] 不存在 trace → 404 — status=404
  [PASS] 非法 format → 422 — status=422
=== D 鉴权占位 ===
  [PASS] 健康检查不需要令牌
  [PASS] 无令牌 → 401 — status=401
  [PASS] 错令牌 → 401 — status=401
  [PASS] 投报文无令牌 → 401 — status=401
  [PASS] 带令牌投报文 → 202 — status=202
=== E 三个遗留边界 ===
  [PASS] trace 与任务已有 trace 不一致 → 409 — status=409
  [PASS] task_id 65 字符 → 422 — status=422
  [PASS] 首次驳回 202 — status=202
  [PASS] remaining_retry 1→0 — 0
  [PASS] 再驳一次仍 202（不崩溃） — status=202
  [PASS] remaining_retry 不为负 — 0
=== F 契约与收尾 ===
  [PASS] 无 DELETE / PUT / PATCH — ['GET', 'POST']
  [PASS] POST /messages 有幂等键说明
  [PASS] 5 个只读/写入接口齐备 — ['/api/archive/{trace_id}', '/api/export/{trace_id}', '/api/health', '/api/messages', '/api/pipelines', '/api/pipelines/{trace_id}', '/api/tasks', '/api/tasks/{trace_id}']
  [PASS] 导出接口标注 csv 类型
结果：全部通过
```

## 四、自测中修掉的缺陷

| # | 现象 | 根因 | 修法 |
| --- | --- | --- | --- |
| 1 | 报文 trace 与任务已有 trace 不一致时仍被受理（202） | 只有 dispatch 校验了 trace 归属，heartbeat/return/reject/abort 只查任务存在 | 新增 `_require_same_trace`，四个 handler 全部补上，实测转为 409 |

## 五、回归

- `scripts/verify_b2.py` 重跑：**全部通过**（鉴权默认空值，对老接口零影响）。

## 六、监理侧扫描

| 检查 | 结果 |
| --- | --- |
| 业务词扫描 | 0 命中 |
| 删除类调用 | 0 处（排除注释） |
| 接口方法集合 | 仅 GET / POST |
| 状态白名单 | 查询筛选复用 `PIPELINE_STATUSES` / `TASK_STATUSES`，非法值 422，不允许写库 |

## 七、待办

1. Mac 双端复核（同一份脚本，须逐行一致）。
2. Codex 复核仍欠（配额）。
3. Batch4 建议范围：超时计时 + 心跳超时判定 + 熔断 + pause/resume 指令 + DAG 依赖排序（本批 `depends_on` 已存好、`pause_flag` 已留位）。
