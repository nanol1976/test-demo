# 09 · Worker 契约

Worker 是执行层。本文件定义 Worker 怎么接活、怎么报活、怎么交活、怎么报错。

---

## 一、你的三个动作

1. **接活**：收到属于你的 `task_dispatch`（`target_worker` 是你），任务状态转「执行中」。
2. **报活**：执行期间按周期发 `task_heartbeat`，让平台知道你还活着。
3. **交活**：做完发 `task_return`；做不了发 `task_reject`；被叫停发 `task_abort`。

## 一·补 怎么"接活"：轮询（Batch9 落地方式）

平台不主动推给你——**你自己去平台取**。Sandbox 里只要两个 HTTP 动作就够，不需要任何额外接口：

```http
GET /api/tasks?status=执行中&target_worker=<我>&instance_id=<我的岗位名>   # 接活：拿派给我的、正在等我干的任务
POST /api/messages                                    # 报活 / 交活 / 报做不了
```

**`instance_id` 不是可选的（Batch31-D 起）**：它就是你的岗位名（形如 `qa-mac`）。

- 带上它：能看到「没指定岗位的活」+「指定给我的活」；
- 不带：只能看到「没指定岗位的活」，**任何指定给别人的活对你不可见**（哪怕 `target_worker` 同族）。

这是为了防止同一身份族的多个实例互相抢活——首单就发生过 Windows 抢在质检前 5 秒做完、质检白跑一趟还被 409 拒。回执里也要带 `metadata.instance_id`，否则台账记不下「到底谁干的」（`done_instance` 会留空）。

响应里的每个任务带 `payload`（派发报文原样搬运），这就是"要干什么"的来源：

```json
{
  "task_id": "t-demo-1",
  "trace_id": "t-demo",
  "target_worker": "workbuddy",
  "status": "执行中",
  "dispatch_time": "2026-11-19T08:00:00",
  "payload": {
    "task_goal": "跑个样例",
    "target_worker": "workbuddy",
    "exec": { "script": "worker/examples/hello_task.py", "args": ["--label", "demo"] }
  }
}
```

三条要点：

1. **`target_worker` 取 `payload.target_worker`**（缺省回退派发报文 `sender`）。Master 派活儿时把 `payload.target_worker` 写成你的名字，任务才会进你的队列。
2. **轮询只按自己的名字筛**，别去捞全表——别人的活不该出现在你的队列里。
3. **拿到就跑，跑完就回执**；平台侧状态是「执行中」，你不回执它永远停在那儿。

参考实现：`worker/agent_worker.py`（纯标准库，可直接用，也可照着自己写）。

## 一·补二 执行规格 `payload.exec`（可选约定）

平台**不解释**这个字段，只搬运；怎么执行是 Master 与 Worker 之间的约定。参考实现认这一种：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `script` | string | 必填，允许目录（`--allow-root`）内的 `.py`；越界/非 `.py`/不存在 → 直接 `task_reject` |
| `args` | string[] | 可选，原样作为位置参数 |
| `timeout_seconds` | number | 可选，只能比 Worker 全局上限更短 |
| `artifacts` | string[] | 可选，跑完回传这些相对路径（含是否存在、字节数） |

安全边界（参考实现强制）：只跑白名单目录内的 `.py`、拒绝 `..` 越界、拒绝拼 shell、单任务硬超时。

### 一·补三 `exec.adapter`：交给外部 agent 干（Batch33）

前一种规格是「跑一个脚本」。要交给 dsh（DeepSeek Harness）这类外部 agent 干时，`exec` 换成长这样——**不需要 `script`，也不需要 `args`**，`payload.task_goal` 就是喂给它的那句话：

```json
"exec": { "adapter": "dsh", "timeout_seconds": 120, "model": "minimaxai/minimax-m3.1-flash" }
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `adapter` | string | 必填，目前只认 `dsh`；写别的值 → `task_reject`「不认识的 adapter：…」 |
| `timeout_seconds` | number | 可选，同样只能比 Worker 全局上限更短 |
| `model` | string | 可选，按单指定模型（Batch33-1）。不填就看环境变量 `MWP_DSH_MODEL`，再没有就用仓库 patch 里的默认 |
| `reasoning_effort` | string | 可选，思考档（Batch33-2），只认 `minimal` / `low` / `medium` / `high`；不填就看 `MWP_DSH_EFFORT`，再没有就不写这一行（用 dsh 自己的默认） |
| `script` / `args` | — | adapter 单里不填；填了也不看 |

`model` 的取值顺序：**`exec.model` > `MWP_DSH_MODEL` > 仓库 patch 默认 `nvidia/nemotron-3.5-lightning-30b-a3b`**。名字只允许 `A-Za-z0-9._:@/-`，不许出现 `..` 或空格，否则整单直接拒（不合法就不跑，不猜）。

执行语义（`worker/dsh_adapter.py`）：

- 命令固定是 `dsh --profile headless --patch <patch 路径> "<task_goal>"`。headless profile 自身是空 patch，所以每次都用 `--patch` 临时叠上英伟达路由，**不改用户自己的 `~/.dsh` 配置**。
- 运行 home 也收进仓库：Worker 给子进程设 `DSH_HOME=run/dsh_home`（`MWP_DSH_HOME` 可改），只把用户 `~/.dsh/profiles/headless` 里的四个小文件（`cordis.yml` / `cordis.patch.yml` / `package.json` / `pnpm-workspace.yaml`）复制过去，node_modules 不复制、bundle 仍用全局装的那份。原因：dsh 每次启动都会重写 `profiles/<name>/cordis.yml`，指着用户的 home 就等于让 Worker 改用户配置，而且很多机器禁止普通进程写用户目录，会直接 `EPERM` 起不来。凭证仍只从 `~/.dsh/.credentials.yaml` 读（只读，不复制）。临时目录（`TMP`/`TEMP`/`TMPDIR`）也指到 `run/tmp_dsh/temp`，免得插件写系统临时目录时被拦。
- **不叠加机器级偏好**：运行 home 隔开后，用户 `~/.dsh/cordis.patch.yml` 那层机器本地偏好不会作用到 headless 单上。这是有意的——headless 单的 provider/模型已经由 `--patch` 明确写死，少一层隐式叠加反而更可预测。
- patch 路径怎么来：模型等于仓库默认时直接用 `worker/dsh/nvidia-patch.yml`；按单换成别的模型时，把这份 yml 当**模板**渲染（`models` 列表和 `agent-default-model` 两处一起换，`name:` 换成模型短名），写到 `run/tmp_dsh/patch-<模型名>.yml`。同一个模型只占一个文件、内容每次重写，**不删**（有些机器上删文件会被安全软件拦，删不干净还留垃圾）。仓库里那份永远不动。每次执行前顺手清掉 `run/tmp_dsh` 里 **7 天**没动过的 `patch-*.yml`（`CACHE_DAYS` 常量可改）；清不掉就跳过，不影响这一单。
- key 从 `~/.dsh/.credentials.yaml` 的 `refs.NVIDIA_API_KEY` 读出（`MWP_DSH_CREDENTIALS` 可指向别处），**只塞进子进程 env**，不落仓库、不进台账。找不到 key → 直接拒单，理由写明找的是哪一段。
- 判定只看退出码：`0` 即成功，**stdout 就是产出**（进回执 `logs`），非零即拒单并把 stderr（或 stdout 末 400 字）带进理由。
- 注意 dsh 的推理过程写在 **stderr**（前缀 `dsh: reasoning:`），答案才在 stdout，所以 `logs` 只取 stdout 是对的，stderr 留给拒单理由。
- 回执 `payload.output` 带 `{adapter, exit_code, duration_ms, model, reasoning_effort, artifact, artifact_bytes, artifact_sha256, artifact_error}`，方便一眼看出这单是谁跑的、跑了多久、用的哪个模型和哪档思考、产出存在哪。
- 已知：dsh 是完整 agent，会自己去读目录里的文件，耗时可能远超单纯问答（实测一次 3+3 让它读了仓库，95s、18 次心跳）。要它「只回答、别用工具」得在 `task_goal` 里写清楚。

### 一·补四 产物留档与思考档（Batch33-2）

前两批的产出只活在回执 `logs`（且截到 2000 字）里，任务一结就查不到干了什么。这一批把它落到文件上：

- **每次执行都写一份 `run/artifacts/<task_id>.log`**（相对仓库根）：开头六行是头（`# task_id` / `# model` / `# reasoning_effort` / `# exit_code` / `# duration_ms` / `# patch`），然后 `=== stdout ===`、`=== stderr ===` 两段全文。推理过程留在 stderr 段里，产出在 stdout 段里，跟 dsh 的分流口径一致。
- 回执里带 `artifact`（相对路径）、`artifact_bytes`、`artifact_sha256`（前 12 位）。**字节数与哈希按落盘后的真实字节算**——Windows 会把换行写成 CRLF，按内存里的文本算必然对不上。
- 写不进去（磁盘满、被安全软件拦）**不影响这一单的结论**：`artifact` 置 `null`，`artifact_error` 写清原因，`metadata.known_issues` 同步带上。不让留档失败把一单正常的活判成失败，也不假装写成功了。
- 任务号当文件名用，先按白名单洗：只留 `A-Za-z0-9._-`，连续的 `.` 一律换成 `_`，开头不许是点。所以 `../../evil` 会变成 `____evil.log`，产物永远落在 `run/artifacts` 里。
- **平台不加接口路径**（接口面铁律仍是 11 条）：`app/services/artifacts.py` 把本机能看到的 `run/artifacts` 扫成一张 `{task_id: {path, bytes}}` 索引（按目录 mtime 缓存，不每刷一次全盘 stat），挂到 `TaskView` 的 `artifact` / `artifact_bytes` / `artifact_error` 三个字段上——跟 Batch32-1 加复核三字段是同一个做法：**路径不动，返回体变厚**。
- 看板在 task_id 后面加一枚「产物 N KB」小签：只有平台**本机**真能看到文件才出现，鼠标悬停显示相对路径；Worker 写档失败则标一枚黄的「产物未留档」，悬停显示原因。
- **留 30 天，之后由 Worker 自己清**：每次执行前顺手清掉 `run/artifacts` 里超过 30 天没动过的 `.log`（`ARTIFACT_DAYS` 常量，或用 `MWP_ARTIFACT_DAYS` 改），别的文件一概不碰，清不掉就跳过。产物是"这一单干过什么"的证据，所以留得比 patch 缓存（7 天）久。
- 点开看全文也**不新增路径**：`GET /api/tasks/{task_id}?include_artifact=true` 在已有单任务接口上多带一个 `artifact_text` 字段（超 8000 字符截断并标注），看板用抽屉面板展示，口令仍在请求头里、不进 URL。列表接口永远不填这个字段，免得把列表撑成下载站。平台读不到（产物在别的机器上）就空着，并在抽屉里直说"回到执行方打开 `run/artifacts/<task_id>.log`"。

思考档：

- `exec.reasoning_effort` 只认 `minimal` / `low` / `medium` / `high`，写进渲染 patch 的 `agent-default-model.config.reasoningEffort`（跟 `provider` / `model` 平级的那一行），**只写一处**，不碰 `llm-pi-ai` 的 `models` 列表。
- 不指定就不写这一行——仓库 patch 里本来没有 `reasoningEffort`，不替别的调用方定档。
- 优先级：**`exec.reasoning_effort` > `MWP_DSH_EFFORT` > 不写**。缓存文件名带档位（`patch-<模型>-<档位>.yml`），同一组合复用，7 天没动过照样被 `prune_cache` 清掉。

## 二、报文格式（与 Master 同协议）

```json
{
  "trace_id": "<从派发报文原样带回>",
  "task_id": "<你的任务 id，原样带回>",
  "msg_type": "task_heartbeat",
  "sender": "workbuddy",
  "timestamp": "<aware ISO 时间>",
  "version": "v1.0",
  "payload": { "progress": "50%" }
}
```

`sender` 只能是 `master` / `dsh` / `workbuddy` / `codex` / `user` 之一——你是谁就写谁，**不许冒充**。

## 三、四条硬规矩

1. **只碰自己的 task_id（平台已强校验，Batch10 起生效）**。`task_heartbeat` / `task_return` / `task_reject` 的 `sender` 必须等于该任务的 `target_worker`，否则 **409 且不落归档**；`task_abort` 只有两类人可以发：任务归属者本人，或 Master（叫停权）。派发（`task_dispatch`）只有 Master 能发，Worker 派活一律 409。
2. **必须带 trace_id，且要与任务原属 trace 一致**。平台用 `_require_same_trace` 校验，不一致 409。
3. **心跳要持续**。平台默认 `heartbeat_timeout_seconds=120`；超了只写 `[心跳告警]` 留痕，**不会杀你的任务**，但 Master 会看到告警并可能决定中止。
4. **做不了要明说**。发 `task_reject` 并给 `reject_reason`，不要硬扛到超时——超时会被判 `执行失败`，比主动驳回更难看。
5. **别指望改 timestamp 重发能再来一次（Batch10）**。平台按 `sender + payload` 指纹做重放拦截：同内容换时间重发 → 202 幂等忽略，不推进状态、不重复扣重试次数、不追加归档。想让平台当新报文处理，就必须真的改了内容。

## 四、回传内容要求

`task_return` 的 payload 里应包含：
- **产出位置**（文件路径 / URL / 关键结论）
- **自检结论**（你验过什么、结果如何）
- **已知问题**（没做完的、有风险的，如实写）

平台不判内容质量，但你写清楚，Master 才能判。

## 五、你不需要做什么

- 不用自己算超时（平台算）
- 不用自己写台账（平台写）
- 不用自己判并行/依赖（平台做闸门）
- 不用自己维护心跳表（平台记）

## 六、失败时的标准姿势

| 情况 | 发什么 | payload 关键字段 |
| --- | --- | --- |
| 干完了 | `task_return` | 产出位置、自检结论、已知问题 |
| 干不了 | `task_reject` | `reject_reason`（具体到"缺什么/为什么做不了"） |
| 被叫停 | `task_abort` | `abort_reason` |
| 还活着 | `task_heartbeat` | `progress`（可粗略） |

## 六·补 回执里该写什么（Batch9 参考实现的字段）

`task_return` 的 `payload` 现成模板：

```json
{
  "output": {
    "artifacts": [{"path": "/abs/path/artifacts/hello.txt", "exists": true, "size_bytes": 210}],
    "exit_code": 0,
    "duration_ms": 82,
    "script": "worker/examples/hello_task.py"
  },
  "logs": "written: /abs/path/artifacts/hello.txt",
  "metadata": {
    "self_check": "脚本正常退出（退出码 0）；产出路径已核验存在",
    "known_issues": "",
    "heartbeats": 2,
    "worker_id": "workbuddy",
    "finished_at": "2026-11-19T08:00:00+00:00"
  }
}
```

`task_reject` 的 `payload.reason` 要写成"下次能据此改"的话，例如
`exec 规格不合法：脚本越界（不在允许目录内）：/tmp/x/escaping_task.py`、
`执行失败（退出码 3）：boom: 故意失败`。

## 六·补二 并发、优先级与停止（Batch11）

| 项 | 口径 |
| --- | --- |
| 并发（上限） | `--concurrency N` 最多同时执行 N 个任务；槽位满了就停止拉新活，不堆积 |
| 优先级 | `payload.priority`（数字，越大越先）；同优先级按 `dispatch_time` 升序、再按 `task_id` 兜底。**平台只搬运不排序**，排序是实现方的事 |
| 停止 | 收到 `SIGTERM`/`SIGINT` → 不再拉新活 → 等在跑任务收尾（`--grace-seconds`，默认 30）→ 退出 0。**不要在主流程里杀掉子进程**，否则任务会留在「执行中」等平台判超时 |
| 被强杀 | `SIGKILL` 时任务留在「执行中」，由平台 `task_timeout_seconds` / `heartbeat_timeout_seconds` 处置；不会被伪造成成功或失败 |
| 弹性并发（下限） | `--min-concurrency M` **Batch12**：每轮按积压 `待办+在跑` 算 `limit = clamp(积压, M, N)`，只提交 `limit - 在跑` 个。积压 4 个、上限 4 下限 1 → 并发打开到 4；只剩 1 个活 → 回落到 1。省线程也省上游压力 |
| 状态可观测 | `--status-file` 每轮落一个小 JSON（`worker_id`/`updated_at`/`concurrency`/`limit`/`backlog`/`in_flight`/`handled`/`poll`），守护脚本与运维直接读它 |
| 多实例标识（Batch14） | `--instance-id`（默认**主机名**，截断 64 字符）。注册表主键是 `(worker_id, instance_id)`，同机跑两个 Worker 不会互相覆盖 `last_seen`；同机重启沿用同一行，不堆垃圾行 |
| 注册表上报（Batch13） | `--registry-interval`（默认 15s）往 `POST /api/workers` 报一次：`worker_id/status/concurrency/limit/in_flight/handled/meta`。**忙闲都报**，所以空转的 Worker 在看板上也看得见。收到 SIGTERM 先报 `stopping`、退出前报 `stopped` |
| 在线判定 | 平台算：`status=running` 且 `last_seen` 在 `heartbeat_timeout_seconds`（默认 120s）内 → 在线；`stopping/stopped` 一律不算在线。平台没有 `/api/workers`（老版本）时 Worker 只记一条 `registry_unsupported`，照常干活 |
| 与任务心跳的区别 | 任务心跳（`task_heartbeat`）说"某个任务还在跑"并刷新 `last_heartbeat_time`；注册表说"这个进程还活着"。两者互补，缺一个看板就有盲区 |
| 关闭上报 | `--no-register`（不想被记录时用；不影响领活/回传） |
| Windows 静默启动（Batch17） | 计划任务**动作必须是 `wscript.exe` 跑隐藏 VBS**，不能直接挂 `cmd.exe` / `powershell.exe`：后者在"仅当用户登录时"运行会先建控制台窗口，`-WindowStyle Hidden` 只是事后隐藏 → 桌面闪一下。VBS 用 `sh.Run cmd, 0, True`（0=不创建窗口，True=等子进程结束，任务状态才等于服务存活）。看门狗**运行时不自注册**，注册动作只在人工执行安装脚本时发生 |
| Windows 装/拆（Batch23） | 三个任务的注册统一收进 `deploy\install-windows-tasks.ps1`：`-DryRun` 预演、默认注册、`-StartTasks` 装完即起、`-Uninstall` 一键拆。动作一律 `wscript.exe //nologo <仓库>\deploy\launch-*-hidden.vbs`，经 `Run(cmd, 0, True)` 启动，不建控制台。启动器路径由 VBS 自身位置推出，**仓库里不写死用户名目录**；worker 的命令行只由安装脚本生成 `run\worker-run.bat`（含 PYTHONPATH、日志重定向、退出码透传），`launch-worker-hidden.vbs` 只负责隐藏执行它。老入口 `worker-windows-task.ps1` 保留为转发 shim |
| 查询 | `GET /api/workers` 全量 / `?online=true` 只看活的 / `?worker_id=workbuddy` 只看某个执行方的全部实例 |

## 七、重试

被驳回后 Master 可以重派同一 task_id（Batch9 起「验收驳回」可直接重派），`remaining_retry` 在驳回当时已减 1，重派不再重复扣；扣到 0 后任务转「执行失败」，不能再派。你不需要自己管次数，但要保证**重派时能接着做**（幂等：别把上一次的半成品重复叠加）。

`remaining_retry` 扣到 0 后不会变负数，也不会再允许派发。
## 八、岗位与实例级路由（Batch31-D 起）

Worker 从「一台机器一个身份」升级成「一台机器可以挂多个岗位，派单能派到岗位」。

| 事项 | 约定 |
|---|---|
| 岗位名 | `--instance-id <角色-主机>`，例如 `qa-mac`、`ops-mac`、`ops-win`；不传则回落主机名（老行为） |
| 任务上的两个字段 | `target_instance`（派给谁）、`done_instance`（谁干的，回执时由平台按实例填）；两者都在 `TaskView` 里能看 |
| 平台侧过滤 | `GET /api/tasks?instance_id=ops-win` 只看该岗位的活；`?include_all=true` 看全部 |
| **不传 instance 时的可见范围** | 只看到「未指定岗位」的活（`target_instance` 为空）。这是 **有意隔离**，不是漏洞：老 Worker 不知道岗位概念，让它去抢定向单会派错人 |
| 老 Worker 兼容策略（2026-11-21 决策） | **保持现状，不做自动补派、不做后台改写**。理由：定向单必须由登记过的岗位接（未登记 `worker_registered` 时平台直接 409 拒派），老 Worker 只能领「未指定岗位」的活；这条边界由平台在派单前就挡住，不需要 Worker 侧改造。真要收编老机器，走一次 `deploy\install-windows-tasks.ps1 -OnlyBat -InstanceId <名> -UseWorkerToken` 重生成启动脚本即可 |
| 未升级的代价（如实记） | 老 Worker 的「累计完成」恒为 0（没有 `done_instance` 就计不进台账口径），看板上会显示成静默或 0，容易被误读成「没干活」；因此收编一台机器时应当同时做三件事：改岗位名、换口令档、重启单进程 |

Windows 那台的收编记录在 `15-岗位与权限.md` 十一·四。