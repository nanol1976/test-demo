# 17 · Batch11 验收单 —— Worker 并发 / 优先级 / 优雅退出 / 三平台守护

> Batch9 的 Worker 是"串行 + 前台跑"，只能证明链路通；Batch10 把权限收口。
> 这一批让 Worker **具备上线条件**：能并发、能按优先级挑活、能被系统托管、能被优雅停止。
>
> 验收：`python scripts/verify_b11.py`（36 项断言）→ **全部通过（0 项失败）**
> 回归：`ci.py` 全绿；`run_all.py` 10 项全绿（Batch2/3/4/5/7/8/9/10/11 + 冒烟）

---

## 一、Worker 新增能力

| 能力 | 参数 / 字段 | 行为 |
| --- | --- | --- |
| 并发执行 | `--concurrency N`（默认 1） | 线程池跑任务，槽位满了就不再拉新活；每个任务各自独立心跳 |
| 优先级 | `payload.priority`（数字，越大越先） | 拉取后本地排序：优先级降序 → `dispatch_time` 升序 → `task_id` 兜底；**平台只搬运、不排序** |
| 优雅退出 | `--grace-seconds`（默认 30） | 收到 `SIGTERM`/`SIGINT` → 停止拉新活 → 等在跑任务收尾（照常回执）→ 退出码 0 |
| 运行状态 | `--status-file` | 每轮写一个小 JSON（`worker_id`、`updated_at`、`concurrency`、`in_flight`、`handled`、`poll`），给守护与运维看 |
| 本地去重 | `--state-file` | 记录已完成任务，重启不重复处理（跨版本仍然幂等，因平台侧状态已收敛） |

平台侧只加了一个字段：`TaskDispatchPayload.priority`（可选 int）。**不加接口、不加表、不改状态机**。

---

## 二、端到端实测（`verify_b11.py` 摘关键）

环境：干净库 + 干净归档 + 随机端口真起 uvicorn + 真起 Worker 子进程。

| 段落 | 断言 | 实测 |
| --- | --- | --- |
| A 并发 | 3 个各 4 秒的任务，`--concurrency 3` 一轮跑完 | 总耗时 **4.7s**（串行需 ≥12s） |
| A 收敛 | 3 个任务全部「待验收」+ 3 个产物文件真实存在 | PASS |
| B 优先级 | 低优先级 id 字母序更靠前（`…a-low`）也不影响顺序 | 领取顺序 `['t-b11-p-z-high', 't-b11-p-a-low']` ✅ |
| B 落库 | `payload.priority=9` 能从 `GET /api/tasks/{id}` 读回（没被 schema 丢掉） | 9 ✅ |
| C 优雅退出 | 长任务执行中发 `SIGTERM` | 进程 **rc=0**，等任务收尾 **5.4s** 后退出 |
| C 不误伤 | 退出前任务照常回执 | 状态仍「待验收」，**没被打成执行失败** |
| D 状态文件 | 运行期可读，含 `worker_id / concurrency / in_flight / poll` | PASS |
| E 守护模板 | systemd / launchd / Windows 三份模板字段齐全 | 8 项断言全 PASS |

### 一个必须避开的坑（本批已修）

第一版实现里 `--once` 是"派一个任务就退出"，`--concurrency 1` 下只跑到 1 个任务就结束，剩下的留在「执行中」。
现在改成"派完继续循环，直到没有待办且没有在跑才退出"。

另外优先级用例一开始用 `…-high` / `…-low` 两个 id，字母序恰好与优先级一致——**测的是 id 排序不是优先级**。已改成 `…-z-high` / `…-a-low` 让两者冲突，并补了一条"`payload.priority` 真进了台账"的断言（第一版里 `priority` 被 payload schema 丢掉，Worker 根本看不到）。

---

## 三、三平台守护模板

| 平台 | 文件 | 关键点 |
| --- | --- | --- |
| Linux (systemd) | `deploy/worker-systemd.service` | `Restart=always` + `RestartSec=10`；`KillSignal=SIGTERM` + `TimeoutStopSec=90`（大于 `--grace-seconds 30`）；`NoNewPrivileges`；日志走 journalctl |
| macOS (launchd) | `deploy/worker-launchd.plist` | `RunAtLoad` + `KeepAlive`；`ExitTimeOut=90`；标准输出/错误重定向到 `run/` |
| Windows (计划任务) | `deploy/worker-windows-task.ps1` | `Register-ScheduledTask` + `AtStartup` 触发；`MultipleInstances IgnoreNew`（禁多实例）；`RestartCount 999` / 间隔 1 分钟；输出重定向 `run\worker.out.log` |

安装步骤都写在文件头注释里（改 4~5 处路径/参数即可）。

**强杀不会把任务打成失败**：Worker 被 `SIGKILL` 时任务仍留在平台「执行中」，由平台的超时/心跳规则处置（Batch4 的 `task_timeout_seconds` / `heartbeat_timeout_seconds`）。这是刻意的——执行现场死了，平台不该假装任务成功或失败。

---

## 四、门禁新增（`ci.py`）

| 门禁 | 检查 |
| --- | --- |
| 可运维参数 | `--concurrency` / `--grace-seconds` / `--status-file` 必须都在 |
| 优雅退出 | `signal_stop` 与 `SIGTERM` 处理在 |
| 并发实现 | `ThreadPoolExecutor` 且 `max_workers=self.concurrency`（防止被改成无限线程） |
| 守护模板 | 三份模板存在，且各自关键字段（`Restart=always` / `KeepAlive` / `Register-ScheduledTask` 等）在 |

`run_all.py` 加入 Batch11。

---

## 五、本批没做

| # | 未做 | 说明 |
| --- | --- | --- |
| 1 | 抢占式调度 / 动态并发 | 现在并发数启动即固定；按队列长度自动伸缩需要先有"平台侧积压指标"，建议下一批 |
| 2 | 跨机 Worker 注册与心跳表 | 平台仍不知道"有哪些 Worker 在线"，只能靠任务状态反推；要做就得加接口（会破 9 接口约束，需先定策略） |
| 3 | 任务级 CPU/内存限额 | Linux 可用 cgroup、Windows 用 Job Object，属部署细化 |
| 4 | 模板的自动化安装（Ansible / 脚本一键装） | 现在给的是"复制粘贴 + 改 4 处"的手工方案 |
