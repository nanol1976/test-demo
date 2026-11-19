# Batch7 验收单：补能力（增量迁移 / 看板自动刷新 / CI 门禁）

- 执行机：Windows DESKTOP-C1V00J3，`.venv311`（Python 3.11）
- 执行日期：2026-11-19
- 本轮性质：**补三个"长期欠账"的能力**，不改协议、不改状态机、不加业务功能

## 一、交付物

| # | 类别 | 文件 | 说明 |
| --- | --- | --- | --- |
| 1 | 迁移框架 | `migrations/runner.py` | 发现 / 校验 / 按序执行 / 幂等 / 失败回滚 |
| 2 | 迁移脚本 | `migrations/versions/v001_baseline.py` | 基线三表（把 Batch6 的 create_all 收编） |
| 3 | 迁移脚本 | `migrations/versions/v002_archive_msg_type_index.py` | 第一条真正的增量：`msg_type` 索引 |
| 4 | 迁移入口 | `deploy/migrate.py` | 重写为版本驱动：`--check` / `--status` / `--to` / `--dry-run` |
| 5 | 看板 | `app/static/index.html` `app/static/app.js` | 自动刷新（关/5s/15s/30s）+ 上次刷新时间 |
| 6 | CI | `scripts/ci.py` | 静态红线 + 迁移三段式 + 全量测试 |
| 7 | CI | `.github/workflows/ci.yml` | 推到仓库即可跑（Python 3.11） |
| 8 | 验收 | `scripts/verify_b7.py` | **48 条断言** |
| 9 | 文档 | `docs/base/13-迁移规范.md` | 迁移约定、命令、回滚立场、常见错误 |
| 10 | 文档 | `docs/base/00-索引目录.md` `12-版本变更规范.md` `README.md` | 同步更新 |

## 二、迁移框架：为什么不用 Alembic

| 维度 | Alembic | 本平台 runner |
| --- | --- | --- |
| 安装面 | alembic + Mako + markupSafe | 零新增依赖（sqlalchemy 已在） |
| 配置面 | alembic.ini + env.py + 路径替换 | 一个目录 + 一条命名约定 |
| 适用规模 | 多分支、多人并行、autogenerate | 单库、单线、无分支 |
| 当前迁移量 | — | 2 个版本 |

Mac 侧 pip 镜像限流（429）已经证明"多一个安装依赖 = 多一处卡死可能"。当前规模下 Alembic 带来的是维护面而不是能力。`schema_version` 表就是 Alembic 认的版本锚点，真要迁过去按版本号一一对应即可。

## 三、真实输出

### 3.1 verify_b7（48 条，全 PASS）

```
A. 迁移框架
  [PASS] 发现迁移文件（>=2 个） — [1, 2]
  [PASS] 版本号单调递增
  [PASS] v001 是基线建表
  [PASS] v002 是增量（索引）
  [PASS] 重复版本号被拒绝 — 版本号重复：v010 同时出现在 v010_a.py 和 v010_b.py
  [PASS] VERSION 与文件名不符被拒绝 — v011_a.py 的 VERSION=12 与文件名前缀 11 不一致
  [PASS] 缺少 up() 被拒绝 — v012_bad.py 缺少必需属性：up
  [PASS] 失败迁移会抛异常
  [PASS] 失败迁移不写 schema_version — 0 -> 0
  [PASS] 空库 --check 不通过（exit 1） — exit=1
  [PASS] 空库 --check 说明缺表
  [PASS] 空库 --check 列出待应用迁移
  [PASS] 空库 upgrade 成功（exit 0） — exit=0
  [PASS] upgrade 打印应用 v001
  [PASS] upgrade 打印应用 v002
  [PASS] 三张台账表已建 — ['message_archive', 'pipeline_ledger', 'schema_version', 'task_ledger']
  [PASS] v002 建的索引存在 — ['ix_message_archive_msg_type', ...]
  [PASS] schema_version 到 2
  [PASS] 升级后 --check 通过（exit 0） — exit=0
  [PASS] 幂等：重复 upgrade 不重复应用
  [PASS] schema_version 仍只有 2 行 — rows=2
  [PASS] 旧库 upgrade 成功 — exit=0
  [PASS] 旧库只应用增量 v002（不重跑 v001）
  [PASS] 旧库版本升到 2
  [PASS] 旧库 v001 的应用时间保持不变
  [PASS] --to 1 只升到 v001
  [PASS] --to 1 后版本为 1
  [PASS] --status 显示 v002 待应用
  [PASS] --to 1 后 --check 不通过（版本落后） — exit=1
  [PASS] 继续 upgrade 补齐 v002

B. 看板自动刷新
  [PASS] 页面有自动刷新开关
  [PASS] 开关含关闭选项
  [PASS] 页面有上次刷新时间位
  [PASS] JS 有刷新间隔常量
  [PASS] JS 用 setTimeout 而非 setInterval（避免请求堆积）
  [PASS] 页面隐藏时暂停刷新
  [PASS] 自动刷新走静默模式（不重复拉列表）
  [PASS] 自动刷新不覆盖用户正在编辑的报文
  [PASS] 上次刷新时间会更新
  [PASS] 刷新间隔持久化到 localStorage
  [PASS] 看板零外部依赖（无 CDN/框架）

C. CI 门禁
  [PASS] scripts/ci.py 存在
  [PASS] .github/workflows/ci.yml 存在
  [PASS] workflow 调 scripts/ci.py
  [PASS] workflow 指定 Python 3.11
  [PASS] ci.py --static-only 可通过 — exit=0
  [PASS] ci.py 输出结论行 — CI 结果：通过

结果：全部通过
```

PASS 48 / FAIL 0。

### 3.2 CI 静态 + 迁移门禁

```
1) 静态门禁
  [PASS] 语法编译（37 个 .py）
  [PASS] 底座业务无关（代码/迁移业务词 0 命中）
  [PASS] 台账铁律（无删除/覆盖调用）
  [PASS] 接口方法只有 GET/POST — get,post
  [PASS] 接口数仍为 9
  [PASS] 8 种 msg_type 枚举齐全
  [PASS] 路由白名单由枚举派生（不手写）
  [PASS] 7 种任务状态在 CHECK 约束里
  [PASS] 幂等唯一约束仍在
  [PASS] pipeline 状态 5 值在 CHECK 里
  [PASS] ensure_schema 启动自检仍在
  [PASS] 看板零外部依赖（无 CDN/框架）
  [PASS] 看板自动刷新开关在位
  [PASS] 迁移文件可发现且递增 — [1, 2]

2) 迁移门禁（三段式）
  [PASS] 空库 --check 必须不通过 — exit=1
  [PASS] upgrade 必须成功 — exit=0
  [PASS] 升级后 --check 必须通过 — exit=0
  [PASS] 重复 upgrade 幂等

CI 结果：通过
```

### 3.3 全量（六段）

```
▶ Batch2 消息总线            结果：全部通过
▶ Batch3 查询/导出/鉴权       结果：全部通过
▶ Batch4 超时/熔断/暂停/DAG   结果：全部通过
▶ Batch5 极简前端            结果：全部通过
▶ Batch7 迁移框架/自动刷新/CI  结果：全部通过
▶ 真实起服务冒烟              结果：全部通过
总结果：全部通过   exit=0
```

### 3.4 旧库增量（模拟 Batch6 生产库，版本 1）

```
数据库：sqlite:///./data/verify_b7_legacy.db
现有表：['message_archive', 'pipeline_ledger', 'schema_version', 'task_ledger']
schema_version：1（目标 2）
→ 应用 v002：message_archive.msg_type 加索引 ix_message_archive_msg_type
schema_version：1 → 2
迁移完成，缺表：无
```

关键点：**v001 没有被重跑**，v001 的 `applied_at` 保持原值，只补了增量。

### 3.5 真实起服务（`run.ps1 -Port 8798`）

```
工作目录：C:\Users\longf\codex-workspace\master-worker-platform
检查依赖…
  依赖已满足，跳过安装
执行迁移…
数据库：sqlite:///./data/b7_run.db
现有表：[]
schema_version：0（目标 2）
```

请求验证：首页 200、`/static/app.js` 200；页面含 `id="autoRefresh"`、app.js 含 `AUTO_KEY` / `visibilitychange`、**不含 `setInterval(`**。

## 四、本轮修掉的问题

| # | 问题 | 类型 | 处置 |
| --- | --- | --- | --- |
| 1 | `verify_b7.run_cli` 把 `_args`（list）塞进环境变量 → `TypeError: environment can only contain strings` | 脚本自身 bug | 参数与 env 分开传 |
| 2 | CI 业务词扫描把 `docs/base` 也扫了，误伤"平台不做什么"里的边界示例 | 规则过宽 | 只扫 `app/` 与 `migrations/`，文档里的业务词是刻意的 |
| 3 | CI 在 `router.py` 里找 8 种 msg_type，但真源是 `schemas.MsgType` 枚举 | 断言找错文件 | 改扫枚举，并另加"白名单由枚举派生"断言 |
| 4 | `verify_b7` 的"无 setInterval"被注释里的 `setInterval` 误伤 | 断言过粗 | 改成查 `setInterval(` 调用形态 |
| 5 | `verify_b7` 的"无 CDN"断言写法有误（替换后仍匹配到 `<script src`） | 断言写错 | 改为按 token 列表查 `http(s):// / cdn. / unpkg ...` |
| 6 | 空库执行 upgrade 时会先打"补建缺失表（版本已记录但表缺失）"这句误导性提示 | 文案错误 | 加 `ver > 0` 前置条件 |

第 2~5 条都不是产品 bug，而是**验收脚本自己写错**——这也是为什么 CI 要自己能被验证（`verify_b7` 会跑 `ci.py --static-only` 并断言其退出码）。

## 五、回归

`run_all.py` 六段全 PASS（Batch2/3/4/5/7 + 冒烟），exit=0。旧库增量路径、幂等路径、`--to` 分段上线路径三条都单独验过。

## 六、监理侧扫描

- 业务词 0 命中（代码与迁移；文档边界示例不计）
- 删除类调用 0 处（含 `drop_table`）
- 接口数 9，方法集合仍 `{GET, POST}`
- 协议常量：8 msg_type 在枚举、7 状态在 CHECK、`uq_archive_message` 仍在
- `ensure_schema` 未被"为了干净"删掉

## 七、遗留

1. 迁移**不支持自动回滚**：数据不可回滚，只能写"向前"迁移
2. CI workflow 已就位，但**未接远端仓库**，推送后才真正跑
3. 迁移没有 `--baseline`（把既有库标记为某版本）能力；当前靠 v001 记录天然兼容
4. 自动刷新是"整表重绘"，数据量大时列表会闪；后续可改成按 trace_id 做差量更新
5. 看板无排序/搜索，只有状态筛选 + 分页
6. `run.sh` 首次裸机运行仍需装依赖（镜像限流时要人工配源）
7. 依赖检测只查 4 个顶层包，不查版本号
