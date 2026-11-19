# Master-Worker 主控集群平台

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

脚本会自动：找/建虚拟环境 → 装依赖 → 执行迁移 → 起服务。

打开 <http://127.0.0.1:8000/> 就是台账看板；<http://127.0.0.1:8000/docs> 是接口文档。

### 手动起（不想用脚本）

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt   # Windows: .venv311\Scripts\python.exe
python deploy/migrate.py            # 建表 + 记 schema_version
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 九个接口（只有 GET / POST）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（免鉴权） |
| POST | `/api/messages` | 投递协议 v1.0 报文（8 种） |
| GET | `/api/pipelines/{trace_id}` | 流水线详情 + 任务列表 |
| GET | `/api/tasks/{task_id}` | 单任务 |
| GET | `/api/archive/{trace_id}` | 归档索引行数 + JSONL 路径 |
| GET | `/api/pipelines` | 流水线分页列表（状态/时间筛选） |
| GET | `/api/tasks` | 任务分页列表（trace/状态/执行方筛选） |
| GET | `/api/export/{trace_id}` | 导出台账快照（json / csv） |
| POST | `/api/commands` | 运维指令：pause / resume / clear-breaker |

页面：`GET /`（看板）、`GET /static/*`（静态资源，不参与鉴权）。

## 8 种报文与 7 个状态

报文：`task_dispatch` / `task_heartbeat` / `task_return` / `task_reject` / `task_abort` / `human_confirm_req` / `human_confirm_resp` / `task_summary`

任务状态：`待拆解` → `待调度下发` → `执行中` → `待验收` → `验收通过` / `验收驳回` / `执行失败`

必带 6 字段：`trace_id` / `task_id` / `msg_type` / `sender` / `timestamp`(aware) / `version` + `payload`

详细口径见 [docs/base/04-协议v1.0.md](docs/base/04-协议v1.0.md) 与 [docs/base/05-七状态机.md](docs/base/05-七状态机.md)。

## 测试

```bash
python scripts/run_all.py            # 一键：verify_b2/b3/b4/b5 + 真实起服务冒烟
python scripts/run_all.py --no-smoke # 跳过冒烟
python scripts/smoke_http.py         # 单独跑冒烟（起 uvicorn 打关键路径再关掉）
```

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
deploy/         run.ps1 / run.sh / migrate.py
scripts/        init_db / verify_b2..b5 / run_all / smoke_http
docs/           Batch 任务卡与验收单
docs/base/      底座规范（架构·SOP·协议·状态机·契约·故障案例…）
```

## 已知遗留（上线要知情）

| # | 遗留 | 影响 |
| --- | --- | --- |
| 1 | 熔断状态在进程内存 | 重启复位 |
| 2 | 心跳超时只告警不杀任务 | 需 Master 决定 |
| 3 | DAG 只挡不排序 | 无拓扑排序/并行度控制 |
| 4 | 看板无自动刷新 | 需手点刷新 |
| 5 | 令牌存前端 localStorage | 正式部署建议 HttpOnly Cookie |
| 6 | `ensure_schema` 自动建表 | 仅兜底，生产走 `deploy/migrate.py` |

## 台账铁律

只追加与状态更新，**禁止删除**。仓储层不提供任何 delete 方法；要撤销只能追加报文改状态。历史留在 `archive/{trace_id}/{date}.jsonl`。
