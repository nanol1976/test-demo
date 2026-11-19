# Batch8 验收单 · 查询增强 / 差量刷新 / 迁移接管

日期：2026-11-19　平台版本：`batch8`　上一版：`batch7`

## 一、本批做了什么

| # | 能力 | 落在哪 |
| --- | --- | --- |
| 1 | 列表关键字搜索（trace_id / 原始需求 / task_id） | `app/repositories/ledger.py`、`app/api/routes_queries.py` |
| 2 | 列表排序（白名单，非法值 422） | 同上（`PIPELINE_SORTS` / `TASK_SORTS`） |
| 3 | 计数改 SQL `func.count()`（不再全表拉回） | `ledger.py` |
| 4 | 看板搜索框 + 排序下拉 + 400ms 防抖 | `app/static/index.html`、`app.js` |
| 5 | 行级差量渲染（不再整表重建） | `app.js`（`listRows` / `taskRows`） |
| 6 | 迁移 `--baseline N` 接管既有库 + `--force` | `deploy/migrate.py` |
| 7 | 门禁扩充：查询契约 / 前端契约 / JS 语法 | `scripts/ci.py` |
| 8 | 新增 `verify_b8.py`（40 条断言） | `scripts/verify_b8.py` |

## 二、Windows 实测（真实输出）

### 2.1 查询增强

```
[PASS] q 命中 trace_id 子串 — {'total': 1, ...}
[PASS] q 命中原始需求子串（2 条）
[PASS] q 无命中 → 0 条且 total=0
[PASS] sort=asc 最早在前 — ['alpha-trace', 'beta-trace', 'gamma-trace']
[PASS] sort=desc 最新在前 — ['gamma-trace', 'beta-trace', 'alpha-trace']
[PASS] 默认排序为最新在前
[PASS] 非法 sort → 422（不落 SQL） — 422
[PASS] q 超长 → 422 — 422
[PASS] q + 分页：total 为过滤后总数 — {'total': 2, 'page': 2, 'size': 1, 'pages': 2}
[PASS] q + 状态组合过滤
[PASS] count 与 list 一致（total == 返回条数）
[PASS] 任务 q 命中 task_id 子串（3 条）
[PASS] 任务 sort=desc 倒序 — ['alpha-a03', 'alpha-a02', 'alpha-a01']
[PASS] 任务非法 sort → 422
[PASS] 导出回归：CSV 200 且含表头
```

### 2.2 迁移接管（手工 + 脚本双重验）

```
$ python deploy/migrate.py --baseline 2
→ 接管 v001..v002（共 2 条，未执行 up）
schema_version：0 → 2
接管完成；后续新迁移会从 v002 之后继续

$ python deploy/migrate.py --check
预检结果：通过

$ python deploy/migrate.py --baseline 2      # 第二次
拒绝接管：已有版本记录（当前 2）；确需覆盖请加 --force

$ python deploy/migrate.py --baseline 1      # 空库
拒绝接管：缺表 ['message_archive', 'pipeline_ledger', 'task_ledger']
（baseline 只用于表已建好的库，缺表请直接跑迁移）
```

### 2.3 前端

- `node --check app/static/app.js` 通过（node v22.23.2）
- `verify_b8` C 段 10 条断言全 PASS：搜索框、排序下拉、`q`/`sort` 参数、400ms 防抖、行复用、只改变化单元格、表格只在首次创建

## 三、回归（全量）

```
▶ Batch2 消息总线             全部通过
▶ Batch3 查询/导出/鉴权       全部通过
▶ Batch4 超时/熔断/暂停/DAG   全部通过
▶ Batch5 极简前端             全部通过
▶ Batch7 迁移框架/自动刷新/CI 全部通过
▶ Batch8 查询增强/差量刷新/接管 全部通过
▶ 真实起服务冒烟              全部通过
```

`ci.py --static-only`：静态门禁通过（含新增的 3 条查询契约、3 条前端契约、JS 语法）。

## 四、修掉的问题（本批真实踩坑）

| # | 问题 | 性质 | 处理 |
| --- | --- | --- | --- |
| 1 | `verify_b5.py` 用 `"delete" not in js` 判"前端不删台账" | **断言过粗**：`Map.delete(key)` 被误判为违规 | 收窄成"无 DELETE 请求 / 无 `DELETE FROM` / 无 `DROP_TABLE`" |
| 2 | `verify_b8` 收尾 `PermissionError [WinError 32]` | 脚本问题（引擎未释放） | `get_engine().dispose()` + 容错删除 |
| 3 | `migrate.py` 直接用 `text` 未导入 | 真 bug | 顶部 import 补 `text` |

## 五、边界与取舍

- **搜索是子串匹配**（`LIKE '%q%'`），不是全文检索：够用、零依赖，代价是大数据量下无索引加速（已写进 README 遗留 9）。
- **排序白名单**：`sort` 只能取 4 个常量值，非法值抛 `ValueError` → 路由转 422。**绝不把用户串拼进 SQL**。
- **`--baseline` 不执行任何 `up()`**：它是"认账"，不是"建表"。缺表、超范围、已有版本三种情况都拒绝，只有 `--force` 才清 `schema_version`（只碰元数据表，台账三表不动）。
- **差量渲染只重绘变化单元格**：状态用 `tag()` 重建，其余用 `textContent` 覆盖；行顺序变化靠 `insertBefore` 对齐，行消失才 `remove()`。

## 六、未做（明确留下）

| # | 遗留 | 原因 |
| --- | --- | --- |
| 1 | CI 未真正推到仓库 | 仓库无远端、无凭据；已做"推送就绪"（`.gitattributes` + 文档 + workflow） |
| 2 | 搜索无索引 / 无 FTS | 当前数据量不需要，加索引会绑死 SQLite 方言 |
| 3 | 看板无"按执行方筛选" | 后端已支持 `target_worker`，前端未加控件，等真实接入后再定 |

## 七、下一步建议

1. 拿到远端地址 → 按 `docs/14-CI接入指南.md` 推送，让门禁真正跑起来。
2. 转真实业务接入（第一个 Worker 真连平台），用看板与导出做第一轮真实流水线。
3. 若台账上量，再评估 FTS/检索表。
