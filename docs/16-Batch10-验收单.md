# 16 · Batch10 验收单 —— 越权强校验 + 重放拦截

> 上一批（Batch9）留下的一条明确缺口：09 契约写"只碰自己的 task_id"，平台却只校验 trace 同源。
> 结果是**任何人拿别人的 task_id 发 heartbeat / return / reject / abort 都能落账，甚至改别人的任务状态**。
> 再加上幂等键是 `(trace, task, msg_type, timestamp)`——**只要改掉 timestamp，同一条 reject 就能反复重发、反复扣重试次数**。
>
> 本批把这两条堵死。验收方式：`python scripts/verify_b10.py`（43 项断言）
> 结论：**全部通过（0 项失败）**；`ci.py` 全绿；`run_all.py` 9 项全绿（Batch2/3/4/5/7/8/9/10 + 冒烟）

---

## 一、改了什么（平台侧，接口面不动）

| # | 规则 | 实现位置 | 失败表现 |
| --- | --- | --- | --- |
| 1 | 派发只允许 Master | `dispatch.handle_task_dispatch` 首行 | 409「task_dispatch 越权：只有 Master 可以派发，报文来自 X」，**连流水线都不建** |
| 2 | 心跳 / 回传 / 驳回只能是任务归属方 | `dispatch._require_owner`，覆盖 `task_heartbeat` / `task_return` / `task_reject` | 409「task_heartbeat 越权：任务 X 归属 Y，报文来自 Z」，**不落归档、状态不动** |
| 3 | 中止：Master 可叫停任何任务，Worker 只能中止自己的 | 同上，`allow_master=True` | 403 → 实际用 409（与全站一致）：非归属非 Master → 409 |
| 4 | 重放拦截（sender + payload 指纹） | `dispatch._find_replay` + `_idempotent` 包装 | 换 timestamp 重发同一条报文 → **202**、`status_changed=false`、note「重放拦截」，不推进状态、不重复扣次数、不追加归档 |

### 两个刻意的取舍

**① 重放拦截只覆盖"执行方动作"（heartbeat / return / reject / abort），刻意排除 `task_dispatch`。**
因为"驳回后用同样 payload 重派"是 05 状态机允许的**合法重试**，若按内容判重会把重试误杀。派发的精确重放仍由幂等键 `(trace, task, msg_type, timestamp)` 拦住。

**② 校验顺序：幂等键 → 重放指纹 → trace 同源 → 归属 → 状态机。**
先判归属再判状态，是为了让"越权"和"状态不允许"两类 409 的 detail 能区分开；幂等与重放必须在最前面，否则重复投递会被状态机误判成 409（Batch2 踩过的坑）。

---

## 二、验收断言（`verify_b10.py` 摘关键）

| 段落 | 断言 | 实测 |
| --- | --- | --- |
| A 派发越权 | `sender=codex` 派发 → 409，且 `GET /api/pipelines/t-b10-unauth` **404**、任务 **404** | PASS |
| B 越权矩阵 | 任务归属 `codex`，分别用 `dsh` 发心跳、`dsh` 发回传、`master` 发驳回、`workbuddy` 发中止 | 4×4 = **16 项全 PASS** |
| B 副作用 | 每次 409 后：归档行数不变（1 行）、任务状态仍「执行中」 | PASS |
| C 中止权限 | `master` 中止任一任务 → **202** → 执行失败 | PASS |
| D 合法路径 | 归属方 `workbuddy`：心跳 202、回传 202→待验收、驳回 202→验收驳回、次数 3→2 | PASS |
| E 重放（reject） | 同 payload 换 timestamp 重发 → 202 + note 含「重放」+ `status_changed=false`；**次数仍 2**；归档 2 行不变 | PASS |
| F 重放（heartbeat） | 同 payload 换 timestamp → 202 拦截，归档不增（没有伪造的第二次心跳） | PASS |
| G 不误杀 | payload 真变了的心跳正常受理，归档 +1 | PASS |
| H 接口面 | 仍 9 个接口、只有 GET/POST | PASS |

---

## 三、既有验收脚本的同步改动（重要）

越权校验上线后，老脚本里"用 A 派发、用 B 回执"的写法会 409。按真实语义修正的是**派发报文**（声明 `payload.target_worker`），不是放宽校验：

| 脚本 | 改动 |
| --- | --- |
| `verify_b2` | 主链路派发补 `payload.target_worker=codex`；驳回报文 `sender` 由 `dsh` 改为归属方 `codex`（原写法本身就是越权，只是以前没人拦） |
| `verify_b4` | 心跳用例的派发补 `payload.target_worker=codex`（心跳 `sender=codex` 与归属一致） |
| `verify_b3` | 不动（任务由 seed 直建；`sender=codex` 的派发用例本来就断言 409） |
| `verify_b5` | 不动（派发已带 `target_worker=workbuddy`，无回执） |

> 顺带说明：b2 原来那条"`sender=dsh` 驳回别人任务"能过，恰恰证明了越权是敞开的——现在它被改成合法归属，越权场景由 b10 专门覆盖。

---

## 四、门禁新增（`ci.py`）

| 门禁 | 检查内容 |
| --- | --- |
| 越权覆盖 | `_require_owner` 必须覆盖 heartbeat / return / reject / abort 四类 |
| 叫停权限 | `task_abort` 必须带 `allow_master=True` |
| 派发权限 | `handle_task_dispatch` 首行必须有"只允许 master"判断 |
| 重放拦截 | `REPLAY_GUARDED_TYPES` 与 `_find_replay` 必须在 |
| 顺序 | 重放判定在源码中的位置必须早于状态校验（防止被重排） |

`run_all.py` 加入 Batch10。

---

## 五、本批没做

| # | 未做 | 说明 |
| --- | --- | --- |
| 1 | 报文签名 / 防伪 | 目前防的是"平台内越权"，不是"外部伪造"；对外网暴露时必须配合 `API_TOKEN` + 网关，报文签名属下一步 |
| 2 | 时间窗校验（拒绝过旧/过未来 timestamp） | 会影响补录与时钟漂移场景，需要先定策略；内容指纹已挡住最主要的重放危害（重复扣次数） |
| 3 | 越权的审计告警 | 现在只返回 409 + 日志，没有"越权计数/告警"面板 |
| 4 | Worker 侧同样校验 | Worker 已只轮询自己的任务；主动伪造属恶意行为，平台侧拦截已足够 |
