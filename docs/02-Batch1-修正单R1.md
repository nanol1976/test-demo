# Batch1 修正单 R1（主审核验意见）

## 环境（已由主审准备好，你不需要再装依赖）
- 可用解释器：`.venv311\Scripts\python.exe`（Python 3.11，已装 fastapi 0.165 / sqlalchemy 2.1.9 / pydantic 2.14）
- **不要再执行 pip install**，不要再创建 venv。上一次你花大量时间在 pip 上，全部失败。

## 写文件的硬规则（上次失败的根因）
上次 `app/schemas/__init__.py`、`app/services/__init__.py` 内容里所有 `"` 都被写成了 `\"`，Python 直接语法错误。
根因：写文件的命令被外层再次转义了。修正方法：

1. 每次只写一个文件，命令里只有一条 here-string，不要再嵌套别的引号或参数。
2. 内容中**不要出现反斜杠转义的双引号**；需要字符串时统一用单引号。
3. 写完立刻 `Get-Content <file> -TotalCount 5` 检查是否含 `\"`，含则重写。
4. 写完立刻用 `.venv311\Scripts\python.exe -c 'import app.schemas'` 验证能导入。

## P0 缺陷（必须修）

| # | 位置 | 问题 | 要求 |
| --- | --- | --- | --- |
| P0-1 | app/schemas/__init__.py | 全文 `\"` 转义污染，无法导入 | 重写为合法 Python，用单引号 |
| P0-2 | app/services/__init__.py | 同上 | 同上 |
| P0-3 | app/models/__init__.py 第23行 | `cascade="all, delete-orphan"` 与 `ondelete="CASCADE"` 违反"台账不可删除"铁律 | 去掉级联删除与外键 ondelete，改为 `cascade="save-update, merge"`（或直接不设 cascade），并在 repository 层显式禁止 delete |
| P0-4 | 缺失 app/api 骨架 | 任务卡要求 api 分层，但 api 目录不存在 | 建 `app/api/__init__.py`、`app/api/deps.py`（get_db 依赖）、`app/api/routes_health.py`（GET /health 返回 {status: ok}）。**不要实现业务路由** |
| P0-5 | 缺失 app/core/logging.py | 任务卡要求统一日志初始化 | 补齐 setup_logging(level) |
| P0-6 | 缺失 README.md | 任务卡硬性要求 | 补齐：安装、初始化、启动、目录说明、边界声明（明确写"平台不做 DAG 拆解与校验"） |

## P1 缺陷（必须修）

| # | 位置 | 问题 | 要求 |
| --- | --- | --- | --- |
| P1-1 | scripts/init_db.py | 建库脚本里硬写入了 test-trace-001 / test-task-001 三条测试数据，会污染正式台账 | 默认只建表建索引；演示数据改为 `--demo` 开关，默认关闭 |
| P1-2 | scripts/init_db.py | `echo=True` 生产不合适 | 改为由 settings.debug 控制 |
| P1-3 | 仓库根 | 多出一个 `.venv`（主审会用 `.venv311`，不用它） | 不要动 `.venv311`；`.venv` 忽略不管 |

## 自检要求（必须实际执行并贴输出）
```
.venv311\Scripts\python.exe -c 'import app.models, app.schemas, app.services, app.core.logging'
.venv311\Scripts\python.exe -c 'from app.schemas import Envelope; Envelope(trace_id="t", task_id="k", msg_type="bad_type", sender="master", timestamp="2026-01-01T00:00:00+08:00")'
.venv311\Scripts\python.exe scripts\init_db.py
.venv311\Scripts\python.exe -c 'from app.main import app; print([r.path for r in app.routes])'
```
第二条必须报校验错误（证明非法 msg_type 被拒）。
`app/main.py` 需要 Created（FastAPI 实例 + 注册 health 路由 + setup_logging）。

## 仍然不变的红线
- 不做 DAG 拆解、Schema 校验、验收判定、审批决策
- 不引入业务词汇（量化/回测/股票）
- msg_type 只有 8 种
- 台账无删除能力
