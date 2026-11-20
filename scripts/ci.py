"""CI 门禁：把"监理扫描"变成可执行的检查。

分三段：
  1) 静态门禁：编译 + 底座红线（业务词 / 删除调用 / 接口集合 / 协议常量 / 状态机）
  2) 迁移门禁：空库 --check 必须不通过 → upgrade → --check 必须通过（三段式）
  3) 全量测试：scripts/run_all.py（可 --no-smoke 跳过真实起服务）

用法：
    python scripts/ci.py                # 全跑
    python scripts/ci.py --static-only  # 只跑静态门禁 + 迁移门禁
    python scripts/ci.py --no-smoke     # 全跑但跳过起服务冒烟

退出码：0 全绿 / 1 有失败。
"""
import importlib.util
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
shutil_which = shutil.which

# 直接 `python scripts/ci.py`（不设 PYTHONPATH）时也能 import app / migrations。
# 本地一直靠 PYTHONPATH 遮着，一上 CI 就报了 ModuleNotFoundError: migrations —— 这条就是补丁。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 门禁永远跑「无鉴权」模式（Batch27）：仓库里一旦有 .env（正式 API_TOKEN），
# 验收脚本的请求会被 401 打回，红的是环境不是代码。空串优先于 .env，等于显式关掉鉴权。
# Batch28：只读口令也是鉴权开关，一起清空——只清全权那一枚的话，无口令请求会变 401。
os.environ["API_TOKEN"] = ""
os.environ["API_TOKEN_READONLY"] = ""

FAILS = []

# 底座必须业务无关：这些词不许出现在应用代码、迁移与底座文档里
BUSINESS_WORDS = ["量化", "回测", "股票", "持仓", "止损", "收益", "策略池", "选股"]

# 台账铁律：只追加与状态更新，禁止删除 / 覆盖
DELETE_PATTERNS = [
    r"\.delete\(",
    r"DELETE\s+FROM",
    r"\.drop_table\(",
]

EXPECTED_MSG_TYPES = [
    "task_dispatch",
    "task_heartbeat",
    "task_return",
    "task_reject",
    "task_abort",
    "human_confirm_req",
    "human_confirm_resp",
    "task_summary",
]

EXPECTED_TASK_STATES = [
    "待拆解",
    "待调度下发",
    "执行中",
    "待验收",
    "验收通过",
    "验收驳回",
    "执行失败",
]


def tail_of(out, n=3, width=160):
    """失败时把子进程输出的最后几行带出来，省得 CI 只报一个 exit=1 无从下手。"""
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return " | " + " / ".join(ln[:width] for ln in lines[-n:])


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)
    return ok


def walk_py(paths):
    for base in paths:
        for dirpath, _dirnames, filenames in os.walk(base):
            if "__pycache__" in dirpath:
                continue
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def stage_static():
    print("=" * 60)
    print("1) 静态门禁")
    print("=" * 60)

    # 1.1 语法编译
    targets = [os.path.join(ROOT, p) for p in ("app", "scripts", "deploy", "migrations", "worker")]
    tmpdir = tempfile.mkdtemp(prefix="mwp_ci_pyc_")
    bad = []
    total = 0
    for path in walk_py(targets):
        if os.path.basename(path).startswith("_tmp"):
            continue
        total += 1
        out = os.path.join(tmpdir, f"{total}.pyc")
        try:
            py_compile.compile(path, cfile=out, doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"{os.path.relpath(path, ROOT)}: {e.msg.splitlines()[0] if e.msg else e}")
    check(f"语法编译（{total} 个 .py）", not bad, "; ".join(bad[:3]))

    # 1.1b 依赖齐备（requirements.txt 里声明过的，必须真能 import）
    # 历史上 httpx 就是本地手动装着、requirements 没写，CI 清环境一装就红。
    need = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "sqlalchemy": "sqlalchemy",
        "pydantic": "pydantic",
        "pydantic-settings": "pydantic_settings",
        "python-dotenv": "dotenv",
        "alembic": "alembic",
        "httpx": "httpx",
    }
    req_text = ""
    req_path = os.path.join(ROOT, "requirements.txt")
    if os.path.exists(req_path):
        with open(req_path, encoding="utf-8") as f:
            req_text = f.read()
    declared = {
        ln.split("==")[0].split(">=")[0].strip()
        for ln in req_text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    missing_dep = [pkg for pkg in need if pkg not in declared]
    broken_dep = [mod for mod in need.values() if importlib.util.find_spec(mod) is None]
    check(
        "依赖齐备（requirements 声明齐全且可 import）",
        not missing_dep and not broken_dep,
        f"未声明：{missing_dep} 未安装：{broken_dep}",
    )

    # 1.2 业务词（只扫代码与迁移；docs/base 里出现业务词是刻意的——那是在写"平台不做什么"的边界示例）
    hits = []
    for path in list(walk_py([os.path.join(ROOT, "app"), os.path.join(ROOT, "migrations"),
                              os.path.join(ROOT, "worker")])):
        text = read(path)
        for w in BUSINESS_WORDS:
            if w in text:
                hits.append(f"{os.path.relpath(path, ROOT)}:{w}")
    check("底座业务无关（代码/迁移业务词 0 命中）", not hits, ", ".join(hits[:5]))

    # 1.3 删除调用
    dels = []
    for path in walk_py([os.path.join(ROOT, "app"), os.path.join(ROOT, "worker")]):
        text = read(path)
        for pat in DELETE_PATTERNS:
            for m in re.finditer(pat, text):
                line_no = text[: m.start()].count("\n") + 1
                dels.append(f"{os.path.relpath(path, ROOT)}:{line_no}:{m.group(0).strip()}")
    check("台账铁律（无删除/覆盖调用）", not dels, "; ".join(dels[:3]))

    # 1.4 接口集合
    api_dir = os.path.join(ROOT, "app", "api")
    routes = []
    methods = set()
    for fn in sorted(os.listdir(api_dir)):
        if not fn.endswith(".py"):
            continue
        text = read(os.path.join(api_dir, fn))
        for m in re.finditer(r"@router\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']", text):
            methods.add(m.group(1))
            routes.append(f"{m.group(2)}")
    check("接口方法只有 GET/POST", methods <= {"get", "post"}, ",".join(sorted(methods)))
    # Batch13：9 → 10 个路径（新增 /api/workers 的 GET + POST 共用同一路径）
    unique_paths = sorted(set(routes))
    check("接口路径为 11 个（Batch18 加只读审计查询）", len(unique_paths) == 11,
          f"{len(unique_paths)} 个：{unique_paths}")

    # 1.5 协议常量（msg_type 的唯一真源是 schemas 里的 MsgType 枚举）
    schemas_py = read(os.path.join(ROOT, "app", "schemas", "__init__.py"))
    missing_types = [t for t in EXPECTED_MSG_TYPES if f'"{t}"' not in schemas_py]
    check("8 种 msg_type 枚举齐全", not missing_types, ",".join(missing_types))

    router_py = read(os.path.join(ROOT, "app", "bus", "router.py"))
    check("路由白名单由枚举派生（不手写）", "VALID_MSG_TYPES" in router_py and "MsgType" in router_py)

    models_py = read(os.path.join(ROOT, "app", "models", "__init__.py"))
    missing_states = [s for s in EXPECTED_TASK_STATES if s not in models_py]
    check("7 种任务状态在 CHECK 约束里", not missing_states, ",".join(missing_states))

    check("幂等唯一约束仍在", "uq_archive_message" in models_py)
    check("pipeline 状态 5 值在 CHECK 里", all(s in models_py for s in ["pending", "running", "success", "aborted", "failed"]))

    # 1.5b Worker 接入红线（Batch9）：执行方只能走 HTTP，不许绕过平台直连底座
    worker_files = list(walk_py([os.path.join(ROOT, "worker")]))
    check("Worker 端点存在（worker/agent_worker.py）",
          any(os.path.basename(p) == "agent_worker.py" for p in worker_files),
          f"{len(worker_files)} 个 .py")
    leaks = []
    for path in worker_files:
        text = read(path)
        rel = os.path.relpath(path, ROOT)
        if re.search(r"^\s*(from\s+app\b|import\s+app\b|from\s+app\.)", text, re.M):
            leaks.append(f"{rel}:import app")
        if re.search(r"^\s*(import|from)\s+sqlalchemy", text, re.M):
            leaks.append(f"{rel}:sqlalchemy")
        if re.search(r"^\s*(from|import)\s+app\.(models|repositories|services|core)", text, re.M):
            leaks.append(f"{rel}:直连底座模块")
    check("Worker 只走 HTTP（不 import app/不碰 sqlalchemy）", not leaks, "; ".join(leaks[:3]))

    # 派发载荷必须落库（否则 Worker 轮询拿到了任务也拿不到"干什么"）
    check("派发载荷落库（dispatch_payload 列）",
          "dispatch_payload" in read(os.path.join(ROOT, "migrations", "versions", "v003_task_dispatch_payload.py"))
          and "dispatch_payload" in models_py,
          "迁移或模型缺 dispatch_payload")

    # 1.5b2 Worker 可运维性红线（Batch11）：并发/优雅退出/状态文件/三平台守护模板
    worker_py = read(os.path.join(ROOT, "worker", "agent_worker.py"))
    for flag in ("--concurrency", "--grace-seconds", "--status-file"):
        check(f"Worker 支持 {flag}", flag in worker_py, "参数缺失")
    check("Worker 收到 SIGTERM 会优雅退出（signal_stop）",
          "signal_stop" in worker_py and "SIGTERM" in worker_py, "缺信号处理")
    check("Worker 并发用线程池且有上限",
          "ThreadPoolExecutor" in worker_py
          and ("max_workers=self.concurrency" in worker_py
               or "max_workers=HARD_MAX_CONCURRENCY" in worker_py))
    templates = {
        "deploy/worker-systemd.service": ["Restart=always", "TimeoutStopSec=90", "agent_worker.py"],
        "deploy/worker-launchd.plist": ["KeepAlive", "ExitTimeOut", "agent_worker.py"],
        "deploy/install-windows-tasks.ps1": ["Register-ScheduledTask", "agent_worker.py", "RestartCount"],
        "deploy/worker-windows-task.ps1": ["install-windows-tasks.ps1"],
    }
    missing_tpl = []
    for rel, needles in templates.items():
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            missing_tpl.append(f"{rel}(缺文件)")
            continue
        text = read(path)
        for needle in needles:
            if needle not in text:
                missing_tpl.append(f"{rel}:{needle}")
    check("三平台守护模板齐全且关键字段在", not missing_tpl, "; ".join(missing_tpl[:4]))

    # 1.5b3 执行方在线视图红线（Batch12）：靠任务心跳推断，不新增接口
    model_py = read(os.path.join(ROOT, "app", "models", "__init__.py"))
    check("台账有 last_heartbeat_time 列", "last_heartbeat_time" in model_py, "缺列")
    check("迁移 v004 存在", os.path.exists(os.path.join(ROOT, "migrations", "versions",
                                                       "v004_task_last_heartbeat.py")))
    dispatch_py = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    check("心跳落库（mark_heartbeat 在心跳处理里被调用）",
          "mark_heartbeat" in dispatch_py, "心跳没记时间")
    check("心跳不走内容级重放拦截（否则时间永远停在第一条）",
          "task_heartbeat" not in re.findall(r"REPLAY_GUARDED_TYPES\s*=\s*\{([^}]*)\}",
                                          dispatch_py, re.S)[0],
          "心跳被当成重放拦掉了")
    js = read(os.path.join(ROOT, "app", "static", "app.js"))
    html = read(os.path.join(ROOT, "app", "static", "index.html"))
    check("看板有执行方视图", "workerTableWrap" in html and "aggregateWorkers" in js)
    check("执行方视图优先走注册表（Batch13），仍保留任务聚合兜底",
          "workers: \"/api/workers\"" in js and "aggregateWorkers" in js)
    check("Worker 有弹性并发下限 --min-concurrency",
          "--min-concurrency" in worker_py, "缺参数")

    # 1.5b4 执行方注册表红线（Batch13）
    check("注册表模型在（worker_registry）", "class WorkerRegistry" in model_py, "缺模型")
    check("迁移 v005 存在", os.path.exists(os.path.join(ROOT, "migrations", "versions",
                                                       "v005_worker_registry.py")))
    workers_py = os.path.join(ROOT, "app", "api", "routes_workers.py")
    check("注册表接口文件存在", os.path.exists(workers_py))
    if os.path.exists(workers_py):
        wtxt = read(workers_py)
        check("注册表 GET/POST 同路径 /api/workers",
              '@router.post("/api/workers"' in wtxt and '@router.get("/api/workers"' in wtxt)
        check("注册表 worker_id 有白名单校验（不能随便造执行方）",
              "ALLOWED_WORKERS" in wtxt and "409" in wtxt)
    check("Worker 会周期上报注册表（忙闲都报）",
          "report(\"running\")" in worker_py or "self.report(\"running\")" in worker_py)
    check("Worker 退出时上报 stopping/stopped",
          '"stopping"' in worker_py and '"stopped"' in worker_py)
    check("注册表主键是 (worker_id, instance_id)（多实例不互相覆盖）",
          'instance_id: Mapped[str] = mapped_column(String(64), primary_key=True' in model_py)
    check("迁移 v006 存在", os.path.exists(os.path.join(ROOT, "migrations", "versions",
                                                       "v006_registry_instance_id.py")))
    check("注册表列名避开 SQL 关键字（物理列 concurrency_limit）",
          '"concurrency_limit"' in model_py, "裸写 limit 会语法错误")
    check("Worker 支持 --instance-id",
          "--instance-id" in worker_py, "缺参数")

    check("Worker 支持 --registry-interval 与 --no-register",
          "--registry-interval" in worker_py and "--no-register" in worker_py)

    # 1.5b5 并发配额下发红线（Batch15）
    check("迁移 v007 存在", os.path.exists(os.path.join(ROOT, "migrations", "versions",
                                                       "v007_worker_desired_limit.py")))
    check("注册表有 desired_limit 列（与 concurrency 分开，才能看出下发没生效）",
          "desired_limit" in model_py)
    cmds_py = read(os.path.join(ROOT, "app", "api", "routes_commands.py"))
    check("指令白名单含 master:worker-limit", "master:worker-limit" in cmds_py)
    check("未注册实例不下发配额（404，防止注册表被塞假行）",
          "执行方实例不存在" in cmds_py, "缺 404 分支")
    check("Worker 会应用平台下发的配额", "_apply_desired_limit" in worker_py)
    check("撤销配额的路径在（回到本地 --concurrency）",
          "limit_restored" in worker_py, "撤销后永久改配置")
    check("线程池不被配额卡死（按硬上限建池）",
          "max_workers=HARD_MAX_CONCURRENCY" in worker_py)
    check("看板有执行方筛选与配额下发入口",
          'id="workerFilter"' in html and 'id="workerOnlineOnly"' in html
          and "master:worker-limit" in js)

    # 1.5b6 配额审计红线（Batch16）
    check("迁移 v008 存在", os.path.exists(os.path.join(ROOT, "migrations", "versions",
                                                       "v008_worker_quota_audit.py")))
    check("审计模型 WorkerQuotaAudit 在", "class WorkerQuotaAudit" in model_py)
    check("下发配额时同时落审计", "db.add(WorkerQuotaAudit" in cmds_py, "漏审计")
    check("审计表是只追加（代码里没有删改审计记录）",
          "delete(WorkerQuotaAudit" not in cmds_py
          and "DELETE FROM worker_quota_audit" not in cmds_py, "审计被允许改写")
    check("审计查询走 CLI（没为它开接口）",
          os.path.exists(os.path.join(ROOT, "scripts", "quota_audit.py")))

    # Batch23 起注册只剩一份：install-windows-tasks.ps1；隐藏启动器是 deploy 下的 launch-*.vbs
    task_ps1 = read(os.path.join(ROOT, "deploy", "worker-windows-task.ps1"))
    inst_ps1 = read(os.path.join(ROOT, "deploy", "install-windows-tasks.ps1"))
    wd_ps1 = read(os.path.join(ROOT, "deploy", "worker-windows-watchdog.ps1"))
    check("三个任务动作用 wscript（不直接挂 cmd，避免桌面闪窗）",
          '-Execute "wscript.exe"' in inst_ps1 and '-Execute "cmd.exe"' not in inst_ps1)
    launchers = [read(os.path.join(ROOT, "deploy", f)) for f in
                 ("launch-platform-hidden.vbs", "launch-worker-hidden.vbs", "launch-watchdog-hidden.vbs")]
    check("隐藏启动器用 sh.Run cmd, 0, True（隐藏 + 等待子进程）",
          all("sh.Run cmd, 0, True" in b for b in launchers),
          "0 或 True 缺一个就会出现窗口/状态失真")
    wd_code = "\n".join(ln for ln in wd_ps1.splitlines() if not ln.lstrip().startswith("#"))
    check("看门狗运行时不自注册（注册只在人工执行安装脚本时）",
          "[switch]$Install" in wd_ps1 and "Register-ScheduledTask" not in wd_code,
          "运行期自注册会导致每分钟重建任务、反复弹窗")
    check("看门狗由隐藏启动器拉起", "sh.Run cmd, 0, True" in launchers[2])

    # 1.5b8 审计覆盖与只读查询（Batch18）
    audit_py = os.path.join(ROOT, "app", "api", "routes_audit.py")
    check("审计查询接口文件存在", os.path.exists(audit_py))
    if os.path.exists(audit_py):
        atxt = read(audit_py)
        check("审计查询只有 GET（只读，不给删改入口）",
              "@router.get(" in atxt and '"/api/audit"' in atxt and "db.commit(" not in atxt
              and "db.add(" not in atxt and "DELETE" not in atxt)
    cmds_py = read(os.path.join(ROOT, "app", "api", "routes_commands.py"))
    check("流水线指令也落审计（pause/resume/clear-breaker）",
          "_audit(db, command=payload.command" in cmds_py, "流水线指令没留痕")
    check("看板有运维审计面板与生效判定",
          'id="auditPanel"' in html and "loadAudit" in js and "auditEffective" in js)

    # 1.5b9 审计分页 + 操作者（Batch22）：翻页不许退回"硬顶 20 条"，操作者不许变成鉴权
    check("审计接口支持 offset（分页走服务端，不靠前端拉全量）",
          "offset: int = Query" in atxt and ".offset(offset)" in atxt)
    check("审计表中的 operator 只 ADD COLUMN 过（v009）",
          os.path.exists(os.path.join(ROOT, "migrations", "versions", "v009_audit_operator.py")))
    check("下发接口从 X-Operator 取操作者",
          'alias="X-Operator"' in cmds_py and "unquote(operator" in cmds_py, "操作者没接进来")
    check("看板有审计翻页控件", all(k in html for k in ("auditPrev", "auditNext")))
    check("看板有操作者输入，且随每个请求带上",
          'id="operatorInput"' in html and 'headers["X-Operator"]' in js)
    check("配额下发结果写进页面（不是塞 data 属性）",
          "workerCmdResult" in js and "setAttribute(\"data-last-cmd\"" not in js)

    # 1.5c 越权与重放红线（Batch10）：权限校验不能被"顺手删掉"
    dispatch_py = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    owner_calls = re.findall(r"_require_owner\(task, envelope, \"task_(\w+)\"", dispatch_py)
    check("越权校验覆盖 heartbeat/return/reject/abort",
          set(owner_calls) == {"heartbeat", "return", "reject", "abort"},
          sorted(set(owner_calls)))
    check("task_abort 允许 master 叫停（allow_master=True）",
          "_require_owner(task, envelope, \"task_abort\", allow_master=True)" in dispatch_py)
    check("派发只允许 Master（Worker 不得派活）",
          'if envelope.sender.value != "master":' in dispatch_py)
    check("重放拦截在（sender+payload 指纹）",
          "REPLAY_GUARDED_TYPES" in dispatch_py and "_find_replay" in dispatch_py,
          "缺重放拦截")
    check("重放拦截先于状态校验（仍在 _idempotent 包装里）",
          dispatch_py.find("_find_replay(db, envelope)") < dispatch_py.find("_require_status(task, TASK_RETURN_FROM"))

    # 1.6 启动自检仍在（防有人为了"看起来干净"把它删了）
    main_py = read(os.path.join(ROOT, "app", "main.py"))
    check("ensure_schema 启动自检仍在", "ensure_schema" in main_py)

    # 1.7 前端零依赖
    html = read(os.path.join(ROOT, "app", "static", "index.html"))
    js = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("看板零外部依赖（无 CDN/框架）", ("http://" not in html and "https://" not in js and "cdn." not in html))
    check("看板自动刷新开关在位", 'id="autoRefresh"' in html and "AUTO_KEY" in js)

    # 1.8 迁移文件命名与顺序
    from migrations import runner  # noqa: WPS433

    found = runner.discover()
    check("迁移文件可发现且递增", len(found) >= 2 and all(found[i].version < found[i + 1].version for i in range(len(found) - 1)), f"{[m.version for m in found]}")

    # 1.5b10 Windows 部署件入库（Batch23）：不许再出现"注册脚本只活在某人机器上"
    for rel in ("deploy/launch-platform-hidden.vbs", "deploy/launch-worker-hidden.vbs",
                "deploy/launch-watchdog-hidden.vbs", "deploy/install-windows-tasks.ps1",
                "deploy/launch-guard-hidden.vbs", "deploy/explorer-guard.ps1"):
        check(f"部署件在仓库里：{rel}", os.path.exists(os.path.join(ROOT, rel)))
    inst = read(os.path.join(ROOT, "deploy", "install-windows-tasks.ps1"))
    check("四个计划任务由同一个脚本注册（不许再散落）",
          all(f'"{n}"' in inst for n in ("MW-Platform", "MW-Worker", "MW-Watchdog", "MW-Guard")))
    check("任务动作是 wscript 跑隐藏 VBS（否则会闪控制台）",
          'Execute "wscript.exe"' in inst and "//nologo" in inst)
    check("安装脚本幂等且有卸载入口", "-Force | Out-Null" in inst and "-Uninstall" in inst)
    wdog = read(os.path.join(ROOT, "deploy", "worker-windows-watchdog.ps1"))
    wdog_body = "\n".join(l for l in wdog.splitlines() if not l.strip().startswith("#"))
    check("看门狗运行时不自注册（Batch17 的病不许复发）",
          "Register-ScheduledTask" not in wdog_body)
    check("部署件不写死用户名目录（换机器能直接用）",
          not re.search(r"C:\\Users\\[^\\]+\\codex-workspace", inst))

    check("Windows 部署脚本带 UTF-8 BOM（PS 5.1 无 BOM 会把中文按 ANSI 解析，直接语法崩）",
          all(open(os.path.join(ROOT, r), "rb").read(3) == b"\xef\xbb\xbf"
              for r in ("deploy/install-windows-tasks.ps1",
                        "deploy/worker-windows-watchdog.ps1",
                        "deploy/worker-windows-task.ps1",
                        "deploy/explorer-guard.ps1")))

    # 1.5b12 explorer 守护红线（Batch25）：守护可以清残留，但绝不许碰生产库
    guard = read(os.path.join(ROOT, "deploy", "explorer-guard.ps1"))
    guard_body = "\n".join(l for l in guard.splitlines() if not l.strip().startswith("#"))
    check("守护脚本把 mvp.db 写进保留白名单", '$keep = @("mvp.db")' in guard)
    check("守护脚本清理限定在白名单里（不在白名单的只报告）", "不在清理白名单里" in guard)
    check("守护脚本不自己注册任务（运行时只许注销）",
          "Register-ScheduledTask" not in guard_body)
    check("守护脚本默认只报告，清理要显式加 -AutoCleanData", "$AutoCleanData" in guard and "只报告" in guard)

    # 1.5b11 库地址红线（Batch24）：默认值不许再指向空库名
    cfg_py = read(os.path.join(ROOT, "app", "core", "config.py"))
    check("默认库地址是 data/mvp.db",
          'database_url: str = "sqlite:///./data/mvp.db"' in cfg_py, "默认库地址又指歪了")
    check("代码/部署件里不再出现历史空库名 mw_platform",
          all("mw_platform" not in read(os.path.join(ROOT, r)) for r in
              ("app/core/config.py", "deploy/run.ps1", "scripts/verify_r2.py")))

    # 1.5b13 看板台账增强（Batch26）：停滞 / 心跳 / 时间轴 / 快照
    appjs = read(os.path.join(ROOT, "app", "static", "app.js"))
    cssjs = read(os.path.join(ROOT, "app", "static", "style.css"))
    check("停滞判定有独立阈值常量（不许散落魔法数）", "const STALL_MS" in appjs)
    check("停滞与滞留分开命名（排队不许被误读成卡死）",
          "stallInfo" in appjs and "滞留" in appjs and "停滞" in appjs)
    check("执行方最近上报改成「N 前」+ 超时红字",
          "heartbeatNode" in appjs and "fmtAgo" in appjs and ".hb.late" in cssjs)
    check("并发用刻度渲染（点=在跑槽位）", "concurNode" in appjs and 'className = "dots"' in appjs)
    check("流水线有单行时间轴（按本页最长耗时归一）",
          "buildRail" in appjs and "rail-fill" in appjs and "pipeMaxMs" in appjs)
    check("快照在前端本地生成（Blob + createObjectURL，不添接口）",
          "function snapshotHtml" in appjs and "URL.createObjectURL" in appjs)
    snap_body = appjs.split("function snapshotHtml")[1].split("function saveSnapshot")[0]
    check("快照页不自带脚本、不引外部资源", "<script" not in snap_body and "http" not in snap_body)
    html = read(os.path.join(ROOT, "app", "static", "index.html"))
    check("看板有停滞计数与存快照入口", 'id="pipeStall"' in html and 'id="snapPipes"' in html)

    # 1.5b14 手机/局域网访问（Batch27）：绑定可配、默认可别裸奔、token 不许入库
    runps = read(os.path.join(ROOT, "deploy", "run.ps1"))
    check("run.ps1 有 -Bind 参数", "[string]$Bind" in runps)
    check("run.ps1 默认仍只绑本机 127.0.0.1（要开局域网得显式传）",
          '[string]$Bind = "127.0.0.1"' in runps, "默认绑定被改成对外了")
    check("run.ps1 用 -Bind 起服务（不是写死 127.0.0.1）", "--host $Bind" in runps)
    plat_vbs = read(os.path.join(ROOT, "deploy", "launch-platform-hidden.vbs"))
    check("平台启动器显式传 -Bind 0.0.0.0（手机才连得上）", "-Bind 0.0.0.0" in plat_vbs)
    inst2 = read(os.path.join(ROOT, "deploy", "install-windows-tasks.ps1"))
    check("安装脚本会从 .env 取 API_TOKEN 给 Worker（否则 Worker 被 401）",
          "API_TOKEN" in inst2 and ".env" in inst2)
    check("安装脚本不打印 token 明文", "不打印内容" in inst2)
    gitig = read(os.path.join(ROOT, ".gitignore"))
    check(".env 不进版本库（token 不能跟着代码走）", "\n.env" in gitig or gitig.startswith(".env"))
    check("安全模块仍保留「token 为空=不校验、health 永远放行」口径",
          "api_token" in read(os.path.join(ROOT, "app", "core", "security.py")))
    css2 = read(os.path.join(ROOT, "app", "static", "style.css"))
    check("窄屏样式存在（手机单栏 + 表格横滚）",
          "@media (max-width: 640px)" in css2 and "min-width: 560px" in css2)

    # 1.8b Batch28：只读口令——写接口必须在服务端拦，不能靠前端藏按钮
    sec_py = read(os.path.join(ROOT, "app", "core", "security.py"))
    cfg_py = read(os.path.join(ROOT, "app", "core", "config.py"))
    main_py = read(os.path.join(ROOT, "app", "main.py"))
    write_routes = "".join(read(os.path.join(ROOT, "app", "api", name)) for name in
                           ("routes_commands.py", "routes_messages.py", "routes_workers.py"))
    check("Batch28 只读口令有配置项（api_token_readonly）", "api_token_readonly" in cfg_py)
    check("Batch28 有 require_write 依赖（只读→403）", "def require_write" in sec_py and "403" in sec_py)
    check("Batch28 三条写接口都挂了 require_write",
          write_routes.count("Depends(require_write)") == 3,
          f"{write_routes.count('Depends(require_write)')} 处")
    check("Batch28 权限档由中间件写进 X-API-Mode 头",
          "MODE_HEADER" in main_py and "middleware" in main_py)
    check("Batch28 看板能切只读态（读头 + 禁用写入口 + 只读章）",
          "X-API-Mode" in read(os.path.join(ROOT, "app", "static", "app.js"))
          and "applyMode" in read(os.path.join(ROOT, "app", "static", "app.js"))
          and 'id="modeBadge"' in read(os.path.join(ROOT, "app", "static", "index.html")))
    check("Batch28 只读章有样式", ".mode-badge" in css2)
    check("Batch28 只读口令进 .env 时与全权分开（不共用一个变量名）",
          "API_TOKEN_READONLY" in inst2 or "API_TOKEN_READONLY" in sec_py,
          "环境变量名没落地")
    # 点「记住」必须连审计一起刷新：以前只刷健康/执行方/流水线，审计面板留着旧的 401，
    # 用户看到的就是「按钮没反应」。
    app_js = read(os.path.join(ROOT, "app", "static", "app.js"))
    save_token_block = app_js.split('el("saveToken").addEventListener("click"', 1)[-1][:900]
    check("Batch28 点「记住」后审计面板一起刷新（不再挂着旧 401）",
          "loadAudit()" in save_token_block,
          "saveToken 回调里没有 loadAudit()")

    # 1.9 查询契约：关键字与排序走白名单（不许把 sort 直通给 SQL）
    queries_py = read(os.path.join(ROOT, "app", "api", "routes_queries.py"))
    ledger_py = read(os.path.join(ROOT, "app", "repositories", "ledger.py"))
    check("列表接口支持 q（关键字）", 'q: Optional[str] = Query' in queries_py)
    check("排序走白名单常量", "PIPELINE_SORTS" in ledger_py and "TASK_SORTS" in ledger_py)
    check("非法排序抛 ValueError（→422）", "非法排序" in ledger_py)

    # 1.9b 前端契约：列表搜索/排序 + 行差量渲染
    html = read(os.path.join(ROOT, "app", "static", "index.html"))
    app_js = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("看板有搜索框与排序下拉", 'id="pipeSearch"' in html and 'id="pipeSort"' in html)
    check("列表请求带 q 与 sort", 'query.set("q", state.q)' in app_js and 'query.set("sort", state.sort)' in app_js)
    check("行级差量渲染（listRows/taskRows）", "const listRows" in app_js and "const taskRows" in app_js)
    check("启动时首刷审计面板（Batch18 漏了 loadAudit，面板会永远停在「加载中」）",
          "loadWorkers();\nloadAudit();" in app_js, "启动脚本没首刷审计")

    # 1.10 JS 语法（有 node 才查；无 node 不算失败，但要留痕）
    js_path = os.path.join(ROOT, "app", "static", "app.js")
    node = shutil_which("node")
    if node:
        proc = subprocess.run(
            [node, "--check", js_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        check("JS 语法（node --check）", proc.returncode == 0, (proc.stderr or "").strip()[:120])
    else:
        print("  [PASS] JS 语法（跳过：本机没有 node）")


def migrate_cli(args, db_url):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = db_url
    proc = subprocess.run(
        [PY, os.path.join(ROOT, "deploy", "migrate.py")] + args,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def stage_migrate():
    print("=" * 60)
    print("2) 迁移门禁（三段式）")
    print("=" * 60)
    db_name = "ci_migrate.db"
    data_dir = os.path.join(ROOT, "data")
    # 干净 clone / CI 里没有 data/（目录不进仓库），不先建出来 sqlite 会
    # "unable to open database file"，迁移门禁会假红。
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, db_name)
    if os.path.exists(db_path):
        os.remove(db_path)
    url = f"sqlite:///./data/{db_name}"

    code, out = migrate_cli(["--check"], url)
    check("空库 --check 必须不通过", code == 1, f"exit={code}")

    code, out = migrate_cli([], url)
    check("upgrade 必须成功", code == 0, f"exit={code}" + tail_of(out))

    code, out = migrate_cli(["--check"], url)
    check("升级后 --check 必须通过", code == 0, f"exit={code}" + tail_of(out))

    code, out = migrate_cli([], url)
    check("重复 upgrade 幂等", code == 0 and "无待应用迁移" in out, "" if code == 0 else tail_of(out))

    if os.path.exists(db_path):
        os.remove(db_path)


def stage_tests(no_smoke):
    print("=" * 60)
    print("3) 全量测试")
    print("=" * 60)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    args = [PY, os.path.join(HERE, "run_all.py")]
    if no_smoke:
        args.append("--no-smoke")
    proc = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = [ln for ln in out.splitlines() if ln.strip().startswith("[PASS]") or ln.strip().startswith("[FAIL]") or "失败项" in ln]
    for ln in tail:
        print("  " + ln.strip())
    check("run_all 全绿", proc.returncode == 0 and not any("失败项" in ln for ln in tail), f"exit={proc.returncode}")
    if proc.returncode != 0:
        print(out[-2000:])


def main():
    static_only = "--static-only" in sys.argv
    no_smoke = "--no-smoke" in sys.argv

    stage_static()

    if FAILS:
        print("\n静态门禁不通过，后续阶段不再执行（先修红线）")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("\n静态门禁通过")

    stage_migrate()
    if FAILS:
        for f in FAILS:
            print("  - " + f)
        return 1
    print("\n迁移门禁通过")

    if static_only:
        print("\n（--static-only：跳过全量测试）")
        print("CI 结果：通过")
        return 0

    stage_tests(no_smoke)

    print()
    if FAILS:
        print("CI 结果：失败")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("CI 结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
