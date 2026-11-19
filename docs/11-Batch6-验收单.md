# Batch6 验收单：收口（文档落盘 / 测试收敛 / 部署脚本 / 迁移）

- 执行机：Windows DESKTOP-C1V00J3，`.venv311`（Python 3.11）
- 执行日期：2026-11-19
- 本轮性质：**不加功能，只收口**——把前五批散落的东西固化成能交付、能复现、能交接的状态

## 一、交付物

| # | 类别 | 文件 | 说明 |
| --- | --- | --- | --- |
| 1 | 底座文档 | `docs/base/00`~`12`（13 份） | 架构 / SOP / 权限卡点 / 协议 / 状态机 / 运维指令 / 台账字段 / Master 契约 / Worker 契约 / 故障案例 / 自检清单 / 变更规范 / 索引 |
| 2 | 测试收敛 | `scripts/run_all.py` | 一键跑 verify_b2/b3/b4/b5 + 冒烟，汇总给结论 |
| 3 | 启动冒烟 | `scripts/smoke_http.py` | 真起 uvicorn、打 16 项关键路径、关服务 |
| 4 | 迁移 | `deploy/migrate.py` | 显式建表 + `schema_version` 表，支持 `--check` 预检 |
| 5 | 启动脚本 | `deploy/run.ps1` / `deploy/run.sh` | Windows / macOS 一键：建环境 → 装依赖 → 迁移 → 起服务 |
| 6 | 配置 | `app/core/config.py` | `class Config` → `SettingsConfigDict`（pydantic v2 正式写法） |
| 7 | 配置样例 | `.env.example` | 补全全部 12 个参数与注释 |
| 8 | 说明 | `README.md` | 重写（原版还是 Batch1 时代的，指令已过时） |
| 9 | 清理 | `scripts/init_db.py` | **删掉 `--demo` 分支**（会绕过平台直接 INSERT 假数据） |
| 10 | 清理 | `docs/_archive/legacy-logs/` | 20 个 Batch1 时代的过程日志从 docs 根移走 |

## 二、底座文档落盘清单（这块欠了五批）

| 文件 | 核心内容 |
| --- | --- |
| `00-索引目录.md` | 12 份规范的入口 + "平台不做什么"边界 + 代码落点对照 |
| `01-四层架构.md` | 决策层 / 执行层 / 平台层 / 存储层，各层职责与禁区 |
| `02-六步SOP.md` | 受理拆解 → 落流水线 → 派发 → 执行 → 验收 → 归档 |
| `03-权限与高危卡点.md` | 权限矩阵、5 类高危动作、卡点实现位置、越权表现 |
| `04-协议v1.0.md` | 6 必带字段、8 msg_type、幂等键、8 个状态码、长度校验 |
| `05-七状态机.md` | 7 中文状态、合法迁移表、非法迁移、pipeline 5 值、sender 5 值 |
| `06-运维指令集.md` | 3 条指令、暂停/熔断期间会发生什么、速查表 |
| `07-台账两表字段.md` | 三张表全字段、abort_reason 前缀、归档目录、三条数据卫生要求 |
| `08-Master主契约.md` | 可直接当系统提示词：必做 4 件、禁做 6 件、状态码处置、自检三问 |
| `09-Worker契约.md` | 三个动作、四条硬规矩、失败标准姿势、重试语义 |
| `10-故障案例.md` | **8 组真实踩过的坑** + 每个的"别再犯"，附录 TestClient 假绿 |
| `11-启动自检清单.md` | A~G 七组勾选项（环境/配置/库/起服务验证/脚本/护栏/遗留） |
| `12-版本变更规范.md` | 何时升版本、六步流程、不许做的事、回滚（数据没有回滚） |

## 三、真实输出

### 3.1 一键全量

```
总结果
  [PASS] Batch2 消息总线
  [PASS] Batch3 查询/导出/鉴权
  [PASS] Batch4 超时/熔断/暂停/DAG
  [PASS] Batch5 极简前端
  [PASS] 真实起服务冒烟
全部通过
```

### 3.2 真实起服务冒烟（16 项，空库启动）

```
  [PASS] 服务在 25s 内起来（空库）
  [PASS] /api/health 200
  [PASS] / 首页 200
  [PASS] 首页含看板标题
  [PASS] /static/app.js 200
  [PASS] /static/style.css 200
  [PASS] 空库查列表不 500 — status=200 body={"meta":{"total":0,...},"items":[]}
  [PASS] openapi 可读
  [PASS] 接口数 9
  [PASS] 方法只有 GET/POST — ['GET', 'POST']
  [PASS] 派发 202 — {"archive_id":1,"status_changed":true,"note":"任务已下发，状态：执行中"}
  [PASS] 任务进入执行中
  [PASS] 导出 CSV 200
  [PASS] 暂停指令 200
  [PASS] 恢复指令 200
  [PASS] 未知指令 400
```

**关键设计**：冒烟脚本**故意不先建表**，就是要验「空库启动自检」这条兜底真的生效——Batch5 的 500 缺陷就是靠这个场景才被抓到的。

### 3.3 迁移三段验证

```
=== --check（空库）===
预检结果：不通过
  缺表：['message_archive', 'pipeline_ledger', 'task_ledger']；版本落后：0 < 1

=== migrate ===
→ 补建缺失表：['message_archive', 'pipeline_ledger', 'task_ledger']
→ 记录 schema_version = 1
迁移完成，缺表：无

=== --check（迁移后）===
现有表：['message_archive', 'pipeline_ledger', 'schema_version', 'task_ledger']
schema_version：1（目标 1）
预检结果：通过
```

### 3.4 `init_db.py` 拒绝越权参数

```
$ python scripts/init_db.py --demo
不支持的参数：['--demo']
本脚本只建表、不写数据。演示数据请走 POST /api/messages（经状态机与归档）。
exit=1
```

## 四、本轮修掉的问题

| # | 问题 | 级别 | 处置 |
| --- | --- | --- | --- |
| 1 | `init_db.py` 保留 `--demo` 分支，会绕过状态机直接 INSERT 台账 | 中（违反台账原则） | 删除分支；传入未知参数明确报错退出 |
| 2 | `config.py` 仍用 pydantic v1 的 `class Config` | 低（未来会炸） | 改 `SettingsConfigDict` |
| 3 | `README.md` 是 Batch1 时代版本，指令过时（写的是"不要新建虚拟环境"、`--demo` 等） | 中 | 全量重写 |
| 4 | `.env.example` 只列 3 个参数，漏了 Batch3/4 新增的 9 个 | 低 | 补全并注释 |
| 5 | `deploy/run.ps1` 手误 `-ErrorOperationAction`（不是合法参数） | 中（脚本会崩） | 改 `-ErrorAction` |
| 6 | docs 根混着 20 个 Batch1 过程日志（含 313KB 的 run.log） | 低 | 移入 `docs/_archive/legacy-logs/` |

第 1 条值得展开：`--demo` 分支从来没被跑过（里面 `archive.id` 字段名都是错的，模型是 `archive_id`），说明它是"写了但没人用"的死代码。**死代码不可怕，可怕的是它给了一个"绕过平台造数据"的入口**——以后有人图省事就会用它，台账立刻被污染。所以删掉比修好更对。

## 五、回归

`run_all.py` 全绿（含 config 改写后的重跑）：Batch2 / Batch3 / Batch4 / Batch5 / 冒烟，五段全 PASS。

## 六、监理侧扫描

- 业务词 0 命中；删除类调用 0 处（`init_db` 的 demo 分支一并清掉）
- 接口方法集合仍只有 GET / POST，接口数仍 9
- 文档与代码一致性：协议 6 字段 / 8 msg_type / 7 状态 / 5 pipeline 状态 / 5 sender / 3 条指令 / 9 个接口，逐项对过代码与 CHECK 约束

## 七、遗留（交付时要知情）

1. 熔断状态在进程内存，重启复位
2. 心跳超时只告警不杀任务
3. DAG 只做闸门，不做拓扑排序与并行度控制
4. 看板无自动刷新
5. 令牌存前端 localStorage（正式部署建议 HttpOnly Cookie）
6. `ensure_schema` 自动建表是兜底，生产走 `deploy/migrate.py`
7. `schema_version` 只有 v1，尚无增量迁移脚本框架（真要换 PG 或加字段时再补 Alembic）
8. 未做 CI（本轮只做了本地一键脚本）

## 八、收尾补丁：部署脚本加固（2026-11-19 双端实测）

Batch6 首次双端复核时，Mac 端 `pip install` 长时间无进展，查出根因是 **pip 镜像返回 429（限流）**，而非脚本逻辑错。这件事暴露了两个启动脚本的真实缺陷：**它们每次都无条件 `pip install`，一旦镜像限流，"一键启动"就会挂死**。据此做了一轮加固。

### 8.1 改动内容

| # | 文件 | 改动 | 原因 |
| --- | --- | --- | --- |
| 1 | `deploy/run.sh` | `echo "安装依赖…"` + 无条件 `pip install` → 先 `python -c "import fastapi, uvicorn, sqlalchemy, pydantic_settings"` 探测，**满足则跳过安装**，不满足才装 | 镜像限流时不再卡死；日常重启秒过 |
| 2 | `deploy/run.ps1` | 同上，用 `$LASTEXITCODE` 判断；装失败 `exit 1` | 同上 |
| 3 | `deploy/run.ps1` | 删除 `$Error = $null`（`$Error` 是 PowerShell 自动变量，本不该赋值），改为 `$OutputEncoding` / `[Console]::OutputEncoding` 置 UTF-8 | 中文提示写进重定向日志时乱码 |
| 4 | `deploy/run.ps1` | 文件存为 **UTF-8 with BOM**（`UTF8Encoding($true)`） | Windows PowerShell 5.1 读"UTF-8 无 BOM + 中文"脚本会解析崩，报"缺少右 }"，且行号错位 |

### 8.2 实测证据

**Windows（`run.ps1`，PowerShell 5.1）**

```
> powershell -ExecutionPolicy Bypass -File deploy\run.ps1 -Port 8795
工作目录：C:\Users\longf\codex-workspace\master-worker-platform
检查依赖…
  依赖已满足，跳过安装
执行迁移…
期望表：['message_archive', 'pipeline_ledger', 'task_ledger']
→ 补建缺失表：['message_archive', 'pipeline_ledger', 'task_ledger']
→ 记录 schema_version = 1
迁移完成，缺表：无
看板地址：http://127.0.0.1:8795/
```

同时请求验证：`/` 200、`/api/health` 200、`/api/pipelines?page=1&size=3` 200。日志中文正常（前 3 字节 `239,187,191` 确认带 BOM）。

**macOS（`run.sh`，BSD userland）**

```
工作目录：/Users/liuhao/codex-workspace/mwp-b6v2/master-worker-platform
检查依赖…
  依赖已满足，跳过安装
执行迁移…
→ 补建缺失表：['message_archive', 'pipeline_ledger', 'task_ledger']
→ 记录 schema_version = 1
迁移完成，缺表：无
```

请求验证 `/` 200、`/api/health` 200、`/api/pipelines` 200；`file` 判定为 `Bourne-Again shell script text executable, Unicode text, UTF-8 text`，CR 字节数 0（LF 换行未被破坏）。

### 8.3 回归

改动只涉及 `deploy/` 两个脚本，不影响应用代码。双端各跑一次全量：

- Windows `.venv311`：Batch2 / Batch3 / Batch4 / Batch5 / 冒烟 **五段全 PASS**，exit=0
- macOS（复用 Batch5 已建 venv，规避镜像 429）：同样 **五段全 PASS**

### 8.4 遗留

1. `run.sh` 首次在裸机运行时仍要装依赖，若镜像限流需要人工配源；建议部署文档补一行 `PIP_INDEX_URL` 说明
2. `run.ps1` 依赖检测只查 4 个顶层包，不查版本号；requirements 升版本时不会自动重装
3. Mac 端本轮是用 Batch5 的 venv 复用的（`ln -s`），**未验证"全新机器从零建 venv"路径**——该路径受镜像限流阻塞，属环境问题非脚本问题

