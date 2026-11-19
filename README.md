# Master-Worker 主控集群平台

[![ci](https://github.com/nanol1976/test-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/nanol1976/test-demo/actions/workflows/ci.yml)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue)
![业务无关](https://img.shields.io/badge/domain-agnostic-success)

给「多 Agent 主控集群」用的基础设施层：收报文、排队、路由、落台账、判超时/心跳/熔断、提供查询与看板。

**业务无关**——它不认识"订单""持仓""回测"，只认协议字段和状态机。量化、回测只是演示用例，任何业务都能套。

## 一句话定位

> Master 负责判断，Worker 负责干，平台只负责**传输 + 记账 + 兜底**。

## 边界（明确不做）

- 不做 DAG 拆解（只做"依赖满足才允许派发"的闸门）
- 不做 Schema 校验（只校验协议必带字段与类型）
- 不做验收判定（产出好不好由 Master 判）
- 不做审批决策（高危只留痕，决策权在人）
- 不删数据（台账只追加与状态更新）

## 快速开始

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File deploy\run.ps1
# 可选：-Port 8000 -Token "你的令牌"
```

### macOS / Linux

```bash
bash deploy/run.sh
# 可选：PORT=8800 TOKEN=你的令牌 bash deploy/run.sh
```

脚本会自动：找/建虚拟环境 → **查依赖（已满足就跳过安装）** → 执行迁移 → 起服务。

打开 <http://127.0.0.1:8000/> 就是台账看板；<http://127.0.0.1:8000/docs> 是接口文档。

### 手动起（不想用脚本）

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt   # Windows: .venv311\Scripts\python.exe
python deploy/migrate.py            # 版本化迁移到最新（建表 + 记 schema_version）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 接入一个真 Worker（Batch9）

平台不推任务，Worker 自己轮询取活——只有两条 HTTP，不需要新接口：

```bash
python worker/agent_worker.py --base-url http://127.0.0.1:8000 --worker-id workbuddy
# 常驻；加 --once 处理一轮就退出（验收脚本用的就是这模式）
```

Worker 干什么由派发报文的 `payload.exec` 决定（白名单目录内的 `.py` + 参数 + 硬超时）：

```json
{"target_worker": "workbuddy", "task_goal": "跑个样例",
 "exec": {"script": "worker/examples/hello_task.py", "args": ["--label", "demo"],
          "artifacts": ["artifacts/hello.txt"]}}
```

链路：派发 → 任务进 `workbuddy` 队列（「执行中」）→ Worker 轮询拿到 → 执行 → 期间发心跳 → 回执 → 「待验收」→ 台账与看板可查。
细节见 [docs/base/09-Worker契约.md](docs/base/09-Worker契约.md) 与 [docs/15-Batch9-验收单.md](docs/15-Batch9-验收单.md)。

## 九个接口（只有 GET / POST）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（免鉴权） |
| POST | `/api/messages` | 投递协议 v1.0 报文（8 种） |
| GET | `/api/pipelines/{trace_id}` | 流水线详情 + 任务列表 |
| GET | `/api/tasks/{task_id}` | 单任务 |
| GET | `/api/archive/{trace_id}` | 归档索引行数 + JSONL 路径 |
| GET | `/api/pipelines` | 流水线分页列表（状态/时间筛选 + 关键字 + 排序） |
| GET | `/api/tasks` | 任务分页列表（trace/状态/执行方筛选 + 关键字 + 排序） |
| GET | `/api/export/{trace_id}` | 导出台账快照（json / csv） |
| POST | `/api/commands` | 运维指令：pause / resume / clear-breaker |

页面：`GET /`（看板）、`GET /static/*`（静态资源，不参与鉴权）。

## 8 种报文与 7 个状态

报文：`task_dispatch` / `task_heartbeat` / `task_return` / `task_reject` / `task_abort` / `human_confirm_req` / `human_confirm_resp` / `task_summary`

任务状态：`待拆解` → `待调度下发` → `执行中` → `待验收` → `验收通过` / `验收驳回` / `执行失败`

必带 6 字段：`trace_id` / `task_id` / `msg_type` / `sender` / `timestamp`(aware) / `version` + `payload`

详细口径见 [docs/base/04-协议v1.0.md](docs/base/04-协议v1.0.md) 与 [docs/base/05-七状态机.md](docs/base/05-七状态机.md)。

## 数据库迁移

库结构走**版本化迁移**，不靠「改模型 + 启动自动建表」兜底：

```bash
python deploy/migrate.py                    # 升级到最新
python deploy/migrate.py --check            # 只检查（不通过 exit 1）
python deploy/migrate.py --status           # 每个版本：已应用 / 待应用
python deploy/migrate.py --to 2             # 只升到 v002（当前最新 v003）
python deploy/migrate.py --dry-run          # 只打印将要执行什么
python deploy/migrate.py --baseline 2       # 接管既有库（表已建好，只补版本记录）
python deploy/migrate.py --baseline 2 --force  # 已有版本记录时强制重写（慎用）
```

迁移文件在 `migrations/versions/vNNN_xxx.py`，约定见 [docs/base/13-迁移规范.md](docs/base/13-迁移规范.md)。
每个迁移必须导出 `VERSION` / `NOTE` / `up(conn)`，`up` 要在事务内可重复执行；重号或编号与文件名不符会被直接拒绝。

`--baseline N` 用于"表已建好但没有版本记录"的既有库：**只写版本记录，不执行任何 `up()`**。默认拒绝已有版本、拒绝缺表的库；`--force` 才会清 `schema_version` 重写（唯一允许的删除，且只碰元数据表）。

## 测试与 CI

```bash
python scripts/run_all.py            # 一键：verify_b2/b3/b4/b5/b7/b8/b9/b10 + 真实起服务冒烟
python scripts/run_all.py --no-smoke # 跳过冒烟
python scripts/ci.py                 # CI 门禁：静态红线 + 迁移三段式 + 全量测试
python scripts/ci.py --static-only   # 只跑静态门禁（快，本地提交前跑）
python scripts/smoke_http.py         # 单独跑冒烟（起 uvicorn 打关键路径再关掉）
```

CI 门禁查什么：
| 门禁 | 检查内容 |
| --- | --- |
| 语法 | 全部 `.py` 可编译 |
| 依赖齐备 | `requirements.txt` 声明齐全，且每个包真能 `import` |
| 业务无关 | 代码/迁移里业务词 0 命中（文档里的边界示例不算） |
| 台账铁律 | 无 `.delete()` / `DELETE FROM` / `drop_table` |
| 接口面 | 仍为 9 个接口，方法只有 GET/POST |
| 协议 | 8 种 msg_type 在枚举里、7 状态在 CHECK 里、幂等唯一约束在 |
| 前端 | 零 CDN、有自动刷新开关、无 setInterval |
| 前端契约 | 有搜索框/排序下拉、请求带 `q` 与 `sort`、行差量渲染 |
| JS 语法 | `node --check app/static/app.js`（本机无 node 则跳过并留痕） |
| 查询契约 | `q` 参数存在、排序走白名单常量、非法排序抛 ValueError |
| Worker 红线 | Worker 只走 HTTP：不 `import app.*`、不碰 sqlalchemy；无删除调用 |
| 越权/重放 | `_require_owner` 覆盖 heartbeat/return/reject/abort；`task_abort` 允许 master；派发只允许 master；重放指纹拦截存在且早于状态校验 |
| 派发载荷 | `task_ledger.dispatch_payload` 列在（否则 Worker 轮询拿到任务也拿不到"干什么"） |
| 迁移 | 空库 --check 不通过 → upgrade 成功 → --check 通过 → 幂等 |

怎么把门禁接到仓库（GitHub / Gitee / 内网 GitLab 三种写法、换行符与 BOM 的坑）：见 [docs/14-CI接入指南.md](docs/14-CI接入指南.md)。

**当前状态**：已推到 `https://github.com/nanol1976/test-demo`，`main` / `master` 两条分支都跑 GitHub Actions，最近一次 success（46 秒）。默认分支 `main` 上就是全量代码。

### 本地怎么验才算数（三条口径）

前七轮"本地全绿"上 CI 连爆三次，全是本地环境遮着的：

1. **清空 `PYTHONPATH` 再跑** —— 本地常设 `PYTHONPATH=<工程根>`，CI 没有，脚本自身必须能 import；
2. **把 `data/` 改名后再跑** —— `data/` 被 gitignore、空目录不进 Git，干净 clone 里没有，sqlite 会 `unable to open database file`；
3. **依赖只按 `requirements.txt` 装** —— 手动装过的包（比如 `httpx`）不会写进依赖，干净环境一装就红。

```bash
# 三条口径合成一条命令（Linux/macOS）
mv data data_bak 2>/dev/null; unset PYTHONPATH; python scripts/ci.py; mv data_bak data 2>/dev/null
```

## 分支约定（代码在 master，文档在 main）

| 分支 | 用途 | 内容 | CI |
| --- | --- | --- | --- |
| `master`（默认） | 代码分支 | `app/ deploy/ migrations/ scripts/ .github/workflows/` + 依赖 + 测试 | 推送即跑门禁 |
| `main` | 文档分支 | `README.md`（文档索引）+ `docs/` | 无 workflow，不跑 |

**改文档不要从 `master` 推 `main`** —— 那会把文档分支整条覆盖成代码：

```bash
git checkout docs                                   # 跟 origin/main 同源的文档分支
git merge --ff-only origin/main                     # 先对齐远端
# 改 README.md 或 docs/ ...
git commit -am "docs: xxx"
git push origin docs:main
```

仓库自带一道 `pre-push` 守卫（`.githooks/pre-push`，已设 `core.hooksPath=.githooks`）：只要把非 `docs`/`main` 的分支往 `refs/heads/main` 推，会被直接拦下并打出正确姿势。确需覆盖时的显式放行开关：

```bash
ALLOW_OVERWRITE_MAIN=1 git push --force origin master:main
```

新克隆的仓库没有 `core.hooksPath`，需要执行一次 `git config core.hooksPath .githooks` 才有这道守卫。

### 服务端兜底（不依赖本地钩子）

`master` 与 `main` 两条分支都已开分支保护（2026-11-19 设置，`administration` 权限开启后配置）：

- **禁止强推**（`allow_force_pushes: false`）
- **禁止删除分支**（`allow_deletions: false`）
- **管理员同样受约束**（`enforce_admins: true`）
- 未开启"必须走 PR"，所以正常的快进推送（`git push origin master` / `docs:main`）不受影响

实测：`git push --force origin HEAD:main` → `remote: error: GH006: Protected branch update failed for refs/heads/main. - Cannot force-push to this branch`，`main` 保持 `139baf4` 未被动。

## 看板

- 自动刷新：列表工具栏可选 关 / 5s / 15s / 30s，间隔记在 localStorage；页面切到后台自动暂停（`visibilitychange`）。
- 用 `setTimeout` 逐个排期，不用 `setInterval`——请求慢时不会堆积。
- 自动刷新是「静默」的：只重拉列表与当前详情，不清空命令结果、不覆盖正在编辑的报文、不重置列表滚动位置。
- **搜索**：列表工具栏输入 trace_id / 需求关键字，400ms 防抖后带 `q` 重新查询（服务端子串匹配，不是前端过滤）。
- **排序**：时间最新在前 / 最早在前，走服务端白名单排序；非法值直接 422，不会把参数透给 SQL。
- **差量刷新**：表格按 `trace_id` / `task_id` 复用 DOM 行，只改变化的单元格（状态、暂停、时间、需求、执行方…），不再每次整表重建——滚动位置与选中行不会跳。

任何 FAIL 都不算通过。双端（Windows / Mac）须逐行一致。

## 目录

```
app/
  api/          路由（health / messages / queries / commands）
  bus/          有界队列 + 多 worker + 回执；报文路由（8 种白名单）
  core/         config / logging / security / clock（可注入时钟）
  models/       三张台账表 + CHECK 约束 + 唯一约束
  repositories/ 台账读写（无任何 delete）
  schemas/      Envelope 与 payload
  services/     dispatch（状态机 + 幂等 + 闸门）/ scheduler（超时·心跳·熔断）/ breaker
  static/       看板（零依赖前端）
worker/         agent_worker.py（真 Worker：轮询/执行/心跳/回执）+ examples/
deploy/         run.ps1 / run.sh / migrate.py
migrations/     runner.py + versions/（版本化增量迁移）
.github/        workflows/ci.yml（接仓库即可用）
scripts/        init_db / verify_b2..b10 / run_all / smoke_http / ci
docs/           Batch 任务卡与验收单
docs/base/      底座规范（架构·SOP·协议·状态机·契约·故障案例…）
```

## 已知遗留（上线要知情）

| # | 遗留 | 影响 |
| --- | --- | --- |
| 1 | 熔断状态在进程内存 | 重启复位 |
| 2 | 心跳超时只告警不杀任务 | 需 Master 决定 |
| 3 | DAG 只挡不排序 | 无拓扑排序/并行度控制 |
| 4 | ~~看板无自动刷新~~ | Batch7 已补：可配置间隔 + 后台暂停 |
| 5 | 令牌存前端 localStorage | 正式部署建议 HttpOnly Cookie |
| 6 | `ensure_schema` 自动建表 | 仅兜底，生产走 `deploy/migrate.py` |
| 7 | 迁移无自动回滚 | 数据不可回滚，只能写「向前」迁移 |
| 8 | ~~CI 未接远端仓库~~ | Batch8 已补：已推 `nanol1976/test-demo`，GitHub Actions 真跑并转绿（`main`/`master` 双分支） |
| 9 | 关键字搜索是 `LIKE '%q%'` 子串匹配 | 台账到十万级会慢，届时改用检索表或 FTS |
| 10 | `--baseline --force` 会清 `schema_version` | 只碰元数据表、需人工显式加参数，但仍属"删"，用前先备份 |
| 11 | 依赖只在 `requirements.txt` 里声明 | 本地手工装的包一律不算，`ci.py` 第 1.1b 条门禁会拦 |
| 12 | ~~越权强校验未做~~ | Batch10 已补：`sender` 必须等于 `task.target_worker`（越权 409 且不落归档）；重放按 sender+payload 指纹拦截；派发只允许 Master |
| 13 | Worker 单进程串行处理 | 并发度与优先级需先定调度策略；当前一轮内的任务按列表顺序逐个跑 |

## 台账铁律

只追加与状态更新，**禁止删除**。仓储层不提供任何 delete 方法；要撤销只能追加报文改状态。历史留在 `archive/{trace_id}/{date}.jsonl`。
