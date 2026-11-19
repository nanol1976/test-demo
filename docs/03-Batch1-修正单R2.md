# Batch1 修正单 R2（双端评审合并后的一次性整改）

来源：Mac 端 Codex 独立评审 + 主审静态走查/动态验证，合并裁定后一次性下发。
解释器固定 `.venv311\Scripts\python.exe`，禁止 pip install、禁止新建 venv。

## 一、必须修（P0，清不完不许进 Batch2）

1. `app/schemas/__init__.py:48`
   `acceptance_criteria` 现为 `Optional[Dict[str, Any]]`，基线是**数组**。
   → 改为 `Optional[List[str]]`，并在 docstring 说明"逐条验收项"。

2. `app/api/deps.py:9-18`
   `get_engine()` / `get_session_factory()` 每次请求新建 engine（连接池泄漏）。
   → 改为模块级 `@lru_cache` 单例，`get_db()` 只负责 yield session 并 close。

3. `app/models/__init__.py` PipelineLedger
   `overall_status` 加约束：只允许 `pending / running / success / aborted / failed`。
   → `CheckConstraint`，写进 `__table_args__`。

4. `app/models/__init__.py` TaskLedger
   `status` 加约束：只允许 7 种状态
   `待拆解 / 待调度下发 / 执行中 / 待验收 / 验收通过 / 验收驳回 / 执行失败`。
   → 同上，用 CheckConstraint。

## 二、应修（P1）

5. 新增 `app/repositories/ledger.py`
   - `create_pipeline(...)` / `create_task(...)`
   - `update_task_status(...)` / `update_pipeline_status(...)`
   - `get_pipeline(trace_id)` / `get_task(task_id)` / `list_tasks(trace_id)`
   - **不得提供** delete / truncate / bulk_update / upsert-覆盖
   - 文件顶部注释写明"台账只追加与状态更新，禁止删除覆盖"

6. `app/schemas/__init__.py` Envelope.timestamp
   validator 拒绝 naive datetime（无 tzinfo 直接抛错，提示必须带时区）。

7. `app/schemas/__init__.py` TaskDispatchPayload.task_goal
   → `Field(..., min_length=1)`，去掉默认空串。

8. `app/models/__init__.py` MessageArchive
   加唯一约束 `(trace_id, task_id, msg_type, timestamp)`，防止重发产生重复索引行。

9. `app/services/__init__.py:22`
   归档目录日期从 `datetime.now()` 改为 `datetime.now(timezone.utc)`，与报文时区一致。

## 三、建议修（P2）

10. `MessageArchive.sender` 加 CheckConstraint，5 值：master/dsh/workbuddy/codex/user。
11. `app/core/config.py` 用 `model_config = SettingsConfigDict(...)` 替换 `class Config`。
12. `scripts/init_db.py` demo 数据里的 raw_json 改用 `Envelope(...).model_dump_json()` 生成，不手工拼串。
13. 补 `.gitignore`：`.venv*/`、`__pycache__/`、`data/*.db`、`archive/`、`*.log`。

## 四、明确禁止改动的（评审已否决）

- ❌ 不要把 `Envelope.task_id` 改成 Optional。基线 3.1 规定所有报文必带 trace_id + task_id。
- ❌ 不要把 `default=list` 改成 `default_factory=list`。SQLAlchemy `mapped_column(default=list)` 传的是可调用对象，本来就是对的。
- ❌ 不要提前实现状态机流转校验、pause/resume、依赖调度器 —— 属于 Batch2/Batch4，不在本批次范围。

## 五、改完自检（必须真跑并贴输出）

```
.venv311\Scripts\python.exe -c 'import app.models,app.schemas,app.services,app.repositories.ledger,app.main; print(1)'
.venv311\Scripts\python.exe -c 'from app.schemas import Envelope; e=Envelope(trace_id="t",task_id="k",msg_type="task_dispatch",sender="master",timestamp="2026-11-19T10:00:00+08:00"); print(type(e))'
.venv311\Scripts\python.exe -c 'from app.schemas import Envelope; Envelope(trace_id="t",task_id="k",msg_type="task_dispatch",sender="master",timestamp="2026-11-19T10:00:00"); print("naive 未拒绝")'
.venv311\Scripts\python.exe scripts\init_db.py
```
第二条要成功；第三条要报错（naive datetime 被拒）。

写文件规则不变：一次一个文件，单条 here-string，内容禁止出现转义双引号，写完 `Get-Content` 校验。
