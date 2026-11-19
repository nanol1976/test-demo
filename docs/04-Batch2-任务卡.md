# Batch2 任务卡：消息总线 + 报文路由（MVP）

- 版本：v1.0 ｜ 生成日期：2026-11-19
- 上游依据：`docs/00-基线规范-v1.0.md`（协议 v1.0、7 状态机、台账铁律）
- 前置状态：Batch1-R2 已验收通过，tag `batch1-r2`（commit 5d690b4），Windows/Mac 双端实跑一致
- 执行方：Windows Codex（主开发）｜交叉复核：Mac Codex ｜监理：主审

---

## 一、本批目标（一句话）

把「报文进来 → 校验 → 路由 → 状态迁移 → 落盘归档 → 可查询」这条主链路打通，让平台第一次具备可运行的消息流。

完成后平台应能做到：**外部用一条 HTTP 报文驱动一次子任务的生命周期，并把每一条报文原样留在不可删除的归档里。**

---

## 二、交付物（7 个文件，1 个脚本，1 份验收单）

| # | 文件 | 类型 | 说明 |
| --- | --- | --- | --- |
| 1 | `app/bus/__init__.py` | 新增 | 进程内消息总线（见 §四.1） |
| 2 | `app/bus/router.py` | 新增 | 报文路由与 handler 注册表（见 §四.2） |
| 3 | `app/services/dispatch.py` | 新增 | 状态迁移编排（见 §四.3） |
| 4 | `app/api/routes_messages.py` | 新增 | 报文入口与查询接口（见 §四.4） |
| 5 | `app/api/schemas_http.py` | 新增 | HTTP 入参/出参模型（见 §四.5） |
| 6 | `app/main.py` | 修改 | 挂载新 router、生命周期内启动/停止总线 |
| 7 | `app/core/config.py` | 修改 | 新增总线参数（队列容量、worker 数） |
| 8 | `scripts/verify_b2.py` | 新增 | 本批验收脚本（含 5 组自检，见 §六） |
| 9 | `docs/05-Batch2-验收单.md` | 新增 | 实际跑出的输出贴在此处（不许写"应通过"，只贴真实输出） |

**禁止新增其它文件。** 需要动其余已有文件，先在验收单里写理由，监理批准后再动。

---

## 三、硬约束（违反即退回，不计分批）

1. **台账铁律**：任何路径不得出现删除/清空/批量覆盖。仓储层已无 delete，本批也不许引入。
2. **业务无关**：不得出现任何业务词（量化、回测、股票、台账明细、订单、客户等）。出现即驳回。
3. **职责边界**：本批只做「运输 + 记录 + 状态迁移」。
   - 不做 DAG 拆解（Master 职责）
   - 不做验收判定（只按报文里的 msg_type 迁移状态，不判断"做得好不好"）
   - 不做审批决策（`human_confirm_req/resp` 只记录，不改状态）
   - 不做超时/心跳超时/熔断（属 Batch4）
   - 不做前端（属 Batch5）
4. **一律不得改协议**：8 种 msg_type、6 个公共必带字段、7 个任务状态、5 个 pipeline 状态、5 个 sender 全部照基线，一个字都不许动。
5. **写入方式**：沿用 `AGENTS.md` —— 一次只写一个文件、单条 here-string、禁止嵌套引号、禁止在沙箱里 `pip install`。
6. **每完成一个文件就立刻跑一次自检**，不许攒到最后一起跑。

---

## 四、逐件规格

### 1. `app/bus/__init__.py` — 进程内消息总线

要求：
- `MessageBus` 类：`__init__(capacity: int = 1000, workers: int = 2)`
- `publish(topic: str, envelope: Envelope) -> None`：topic 固定用 `trace_id`
- `subscribe(topic: str) -> Iterator[Envelope]` 或 `get_queue(topic) -> Queue`
- **有界队列**：`queue.Queue(maxsize=capacity)`
- **背压**：队列满时抛自定义异常 `BusBackpressureError`，**绝不丢消息、绝不阻塞死锁**
- `start()` / `stop()`：启动/停止消费线程，`stop()` 必须在 `join(timeout)` 后把残留消息数记录下来（日志 + 返回值）
- 线程安全：内部持锁维护 topic → queue 映射；同一 topic 只创建一次队列
- 日志走 `app.core.logging` 既有 logger，不要 print

### 2. `app/bus/router.py` — 报文路由

要求：
- `Router` 类，`register(msg_type: MsgType, handler: Callable[[Session, Envelope], HandlerResult])`
- `route(db: Session, raw: dict) -> HandlerResult`
  1. `Envelope` 校验（含 timestamp 时区，已在 Batch1 生效）
  2. `validate_envelope()` 做 payload 校验（Batch1 已有）
  3. msg_type 不在 8 种之内 → 抛 `UnknownMsgTypeError`
  4. 查 handler；无 handler → `NoHandlerError`
  5. 调 handler，拿到结果
- `HandlerResult`：`dataclass`，字段 `archive_id: int`、`status_changed: bool`、`note: str`
- **幂等**：同一 `(trace_id, task_id, msg_type, timestamp)` 重复投递，第二次必须返回**同一个 archive_id**，不得重复写 JSONL、不得报错（依赖 Batch1 已建的唯一约束）
- 归档顺序固定：**先写 JSONL，再写索引行**；索引撞唯一约束时按幂等处理，不回滚 JSONL

### 3. `app/services/dispatch.py` — 状态迁移编排

按 msg_type 定义 handler，全部走 `LedgerRepository`（**不许直接写 SQL、不许自己 new Session**）：

| msg_type | 状态迁移 | 附带动作 |
| --- | --- | --- |
| `task_dispatch` | `待调度下发 → 执行中` | 写 `dispatch_time`；trace 不存在则先建 pipeline（`pending`）|
| `task_heartbeat` | 不改状态 | 只记归档；`progress` 字段留痕 |
| `task_return` | `执行中 → 待验收` | 写 `return_time` |
| `task_reject` | `待验收 → 验收驳回` | `remaining_retry - 1`；**不自动重派**（重派决策属 Master）|
| `task_abort` | 任意 → `执行失败` | 记 `abort_reason`；pipeline 置 `aborted` |
| `human_confirm_req` | 不改状态 | 仅归档（高危卡点留痕）|
| `human_confirm_resp` | 不改状态 | 仅归档 |
| `task_summary` | 不改任务状态 | 按 payload `pipeline_status` 更新 pipeline（走白名单校验）|

非法迁移（例如对 `待拆解` 任务发 `task_return`）必须抛 `IllegalTransitionError`，报文返回 409，且**不落任何状态改动**。

### 4. `app/api/routes_messages.py` — 对外接口（仅 4 个）

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/api/messages` | 收一条报文，走总线 → 路由 → 归档，返回 `{archive_id, status_changed, note}`，成功 202 |
| GET | `/api/pipelines/{trace_id}` | 返回 pipeline + 其全部 task（含状态、剩余重试）|
| GET | `/api/tasks/{task_id}` | 返回单任务台账 |
| GET | `/api/archive/{trace_id}` | 返回该 trace 的归档索引行数与 JSONL 路径（不返回原始报文正文）|

**不提供任何 DELETE / PUT / PATCH。** 状态只能由报文驱动，不许外部直接改。

错误码约定：未知 msg_type → 400；payload 校验失败 / naive timestamp → 422；非法迁移 → 409；总线背压 → 503（带 `Retry-After` 语义的提示文本）；trace/task 不存在 → 404。

### 5. `app/api/schemas_http.py` — HTTP 模型

`MessageAccepted`（archive_id / status_changed / note）、`PipelineView`、`TaskView`、`ArchiveView`、`ErrorBody`（`code` + `message`）。字段名与台账列名保持一致，不要另造叫法。

### 6. `app/main.py` 修改

`lifespan` 内 `bus.start()`，退出时 `bus.stop()` 并把残留消息数写日志；`include_router` 挂上 routes_messages。**不许在这里做任何业务判断。**

### 7. `app/core/config.py` 修改

加 `bus_capacity: int = 1000`、`bus_workers: int = 2`，沿用 `SettingsConfigDict`，环境变量同名可覆盖。

---

## 五、完成定义（DoD）

- 9 个交付物全部存在，`scripts/verify_b2.py` 一次跑完 5 组全绿
- 全模块 `import` 通过（含 `app.bus.router`、`app.services.dispatch`）
- 默认建库后经 6 类报文驱动，台账数据与预期逐条对得上
- 归档 JSONL 行数 = 入库索引行数（幂等场景除外，重复报文两边都不增加）
- 无任何删除路径、无任何业务词
- `docs/05-Batch2-验收单.md` 内贴的是真实终端输出，不是描述

---

## 六、`scripts/verify_b2.py` 必含 5 组自检

**A 组｜主链路**
POST 一条合法 `task_dispatch`（带时区）→ 202；pipeline 自动建为 `pending`；任务状态 `执行中`；`dispatch_time` 非空。

**B 组｜状态迁移全表**
依次投 `task_return`（→待验收）、`task_reject`（→验收驳回，remaining_retry 3→2）、`task_abort`（→执行失败，pipeline→aborted），逐条断言状态字符串，任何一个不对就 FAIL。

**C 组｜非法输入四种**
① 未知 msg_type → 400；② payload 缺 `task_goal` → 422；③ naive timestamp → 422；④ 对 `待拆解` 任务发 `task_return` → 409，且该任务状态仍为 `待拆解`（证明未落状态改动）。

**D 组｜幂等与归档一致性**
同一条报文连投 2 次 → archive_id 相同、JSONL 仍 1 行、索引仍 1 行；另投 20 条不同报文 → 索引 20 行、JSONL 20 行，逐一比对无丢无重。

**E 组｜背压与线程安全**
把 `bus_capacity` 设成 2，连投 5 条 → 至少 1 条抛 `BusBackpressureError`（HTTP 侧 503），且**已入队的消息一条不丢**（最终归档数 = 入队成功数）；并发 20 线程投递不同报文 → 全部入库、无异常。

---

## 七、执行步骤（Codex 按序做，一步一验）

1. `app/bus/__init__.py` → 自检：`bus.publish/subscribe` 本机收发一条
2. `app/bus/router.py` → 自检：未知 msg_type 被拒
3. `app/services/dispatch.py` → 自检：dispatch 迁移生效
4. `app/api/schemas_http.py` → 自检：import 通过
5. `app/api/routes_messages.py` → 自检：POST 一条走通
6. `app/main.py` + `app/core/config.py` → 自检：`/api/health` 仍 200
7. `scripts/verify_b2.py` → 5 组全绿
8. `docs/05-Batch2-验收单.md` → 贴真实输出
9. 交监理复核

---

## 八、明确不在本批（避免越界，也避免返工）

- 超时计时、心跳超时判定、熔断、pause/resume 指令 → **Batch4**
- DAG 依赖排序与并行调度 → **Batch4**（本批 `depends_on` 只存不算）
- 重试自动重派 → **Master 侧职责**，本批只扣次数
- Web 界面、看板 → **Batch5**
- 部署脚本、CI → **Batch6**

Batch2 只需为这些留好接口位：`depends_on` 存好、`remaining_retry` 扣准、`pause_flag` 留着不用。

---

## 九、监理验收方式（我会这么做，提前写清）

1. 逐文件静态走查：先核对 §三 六条硬约束，再核对 §四 规格
2. 独立跑 `scripts/verify_b2.py`，不接受"你看日志吧"
3. 自己另跑三条你没写的边界：报文里 `trace_id` 与任务已有 `trace_id` 不一致、超长 `task_id`（>64 字符）、`task_reject` 连扣到 0 后再扣一次
4. 打包送 Mac 交叉复核（同一份脚本，双端必须逐行一致）
5. 有异议我出修正单（R1/R2…），只列差异不改你的活

---

## 十、Batch2 完成后的平台能力（对用户可交付的说明口径）

能说清楚这三句，才算真的做完：

1. 一条报文进来，能被完整校验、路由、写入不可删除的归档；
2. 一个子任务的 7 种状态，只能由报文推动，外部改不动；
3. 归档与台账索引始终对得上，重复投递不会产生双份记录。
