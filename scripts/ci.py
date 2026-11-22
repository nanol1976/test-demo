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
import json
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
# Batch31：执行方口令也是鉴权开关，不清空同一批冒烟会从 401 变 403（方向被拒）
os.environ["API_TOKEN_WORKER"] = ""

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
    # detail 是「失败原因」的措辞，PASS 时打出来只会让人误读成问题，所以只在 FAIL 时显示
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
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

    # 1.5b2b Batch31：Mac 双岗位常驻 —— 一个岗位一个 plist，口令不进 plist。
    # 红线守两件事：① 安装脚本真的按岗位循环生成（不是手工写死一个）；
    # ② plist 生成处不许出现 token 字面量，口令只能从 ~/.mwp/worker.env 读。
    mac_install_py = read(os.path.join(ROOT, "deploy", "install-mac-workers.sh"))
    mac_wrapper_py = read(os.path.join(ROOT, "deploy", "run-mw-worker.sh"))
    check("Batch31 Mac 安装脚本按岗位循环生成 plist",
          "for inst in $INSTANCES" in mac_install_py and "launchctl bootstrap" in mac_install_py)
    check("Batch31 Mac plist 口令不进文件（只传岗位名）",
          "MWP_TOKEN_WORKER" not in mac_install_py.split("<plist", 1)[-1],
          "plist 生成段里出现了口令变量")
    check("Batch31 Mac 启动器口令从 env 文件读且必须非空",
          'MWP_TOKEN_WORKER:?' in mac_wrapper_py and ". \"$CONF\"" in mac_wrapper_py)
    check("Batch31 质检岗默认只碰 scripts（岗位即权限边界）",
          '"$ROLE" = "qa"' in mac_wrapper_py and "scripts" in mac_wrapper_py)

    # 1.5b4 实例级路由红线（Batch31-D）：派单能指定岗位、回执能追溯岗位
    # 注意：model_py / dispatch_py 的公共读取在本段之后，这里自己读，避免依赖顺序
    model_b31d = read(os.path.join(ROOT, "app", "models", "__init__.py"))
    dispatch_b31d = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    ledger_py = read(os.path.join(ROOT, "app", "repositories", "ledger.py"))
    worker_py = read(os.path.join(ROOT, "worker", "agent_worker.py"))
    check("台账有 target_instance / done_instance 两列",
          "target_instance" in model_b31d and "done_instance" in model_b31d, "缺列")
    check("迁移 v010 存在", os.path.exists(os.path.join(ROOT, "migrations", "versions",
                                                       "v010_task_instance.py")))
    check("派单载荷支持 target_instance",
          "target_instance" in read(os.path.join(ROOT, "app", "schemas", "__init__.py")))
    check("派给未注册岗位当场拦下（不建任务）", "在注册表里从未出现" in dispatch_b31d)
    check("说不清自己是谁的请求看不到指定岗位的活",
          "elif not include_all" in ledger_py and "target_instance.is_(None)" in ledger_py)
    check("Worker 拉活带 instance_id",
          'params["instance_id"]' in worker_py and "instance_id=self.instance_id" in worker_py)
    check("回执 metadata 带 instance_id（可追溯执行岗位）",
          '"instance_id": self.instance_id' in worker_py)
    check("重派同一 task_id 不会被本地 done 挡掉（按 dispatch_time 认新一轮）",
          "redispatch_accept" in worker_py and "dispatch_seen" in worker_py)

    # 1.5b5 Batch31-E：「累计完成」改台账口径 + 五业务方向验收探针
    # 口径红线守三件事：① 后端有 _completed_counts 且按「验收通过」统计（不是进程内 handled）；
    # ② 看板那一列绑 completed_total 而不是 handled；③ 五条业务线有可执行的探针，且门禁真跑一遍。
    workers_txt = read(os.path.join(ROOT, "app", "api", "routes_workers.py"))
    check("累计完成有台账口径实现（_completed_counts，按验收通过计）",
          "def _completed_counts" in workers_txt and 'TaskLedger.status == "验收通过"' in workers_txt
          and "done_instance.isnot(None)" in workers_txt)
    check("执行方视图暴露 completed_total（并保留 handled 说明进程内计数）",
          "completed_total: int = 0" in workers_txt and "handled: int" in workers_txt)
    js_b31e = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("看板「累计完成」列绑 completed_total 不再绑 handled",
          "w.completed_total" in js_b31e and "String(w.handled)" not in js_b31e)
    probes_path = os.path.join(ROOT, "scripts", "role_probes.py")
    check("五业务方向探针脚本在位", os.path.exists(probes_path))
    if os.path.exists(probes_path):
        ptxt = read(probes_path)
        roles = ["probe_stock", "probe_doc", "probe_media", "probe_social", "probe_dev"]
        check("探针覆盖五条业务线", all(f"def {r}(" in ptxt for r in roles),
              "缺 " + ",".join(r for r in roles if f"def {r}(" not in ptxt))
        rp = subprocess.run([PY, os.path.join("scripts", "role_probes.py"), "--role", "all", "--json"],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180)
        ok_roles, detail_roles = False, ""
        try:
            payload = json.loads(rp.stdout or "{}")
            ok_roles = bool(payload.get("ok")) and len(payload.get("results") or []) == 5
            detail_roles = " / ".join(f"{r['role']}:{r['verdict']}" for r in payload.get("results") or [])
        except ValueError:
            detail_roles = (rp.stdout or rp.stderr or "")[:160]
        check("五业务方向探针实跑全过（离线可判）", ok_roles, detail_roles)

    # 1.5b6 Batch31-E 遗留处置：滞留单的人工收尾工具必须默认演练、且有年龄闸门
    close_path = os.path.join(ROOT, "scripts", "close_stale.py")
    check("滞留流水线有人工收尾工具", os.path.exists(close_path))
    if os.path.exists(close_path):
        ct = read(close_path)
        check("收尾工具默认演练（无 --apply 不写库）",
              'add_argument("--apply"' in ct and "if not args.apply" in ct)
        check("收尾工具有 24h 年龄闸门（防手滑关掉在跑的单）",
              "min-age-hours" in ct and "min_age_hours" in ct)
        check("收尾工具只推进状态、不删行",
              "delete(" not in ct.lower() and "update_task_status" in ct)

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
          "offset: int = Query" in atxt
          and (".offset(offset)" in atxt or "entries[offset:offset + limit]" in atxt))
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
    check("计划任务由同一个脚本注册（5 条，不许再散落）",
          all(f'"{n}"' in inst for n in ("MW-Platform", "MW-Worker", "MW-Watchdog", "MW-Guard", "MW-Snapshot")))
    # Batch30：快照是"每天一次跑完就走"，不能用常驻任务的每分钟重复触发
    check("每日快照任务用 Daily 触发（不是每分钟常驻）",
          "-Daily -At $DailyAt" in inst and "launch-snapshot-hidden.vbs" in inst,
          "快照任务没进注册脚本或触发写错")
    snap_vbs = read(os.path.join(ROOT, "deploy", "launch-snapshot-hidden.vbs"))
    check("每日快照启动器隐藏执行并等待子进程（任务结果才是真的）",
          "sh.Run cmd, 0, True" in snap_vbs and "daily_snapshot.py" in snap_vbs,
          "快照启动器缺隐藏运行或不是等子进程")
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
    check("Batch28 三条写接口都挂了 require_write／执行方档（Batch31 起 1 全权 + 2 执行方）",
          write_routes.count("Depends(require_write)") == 1
          and write_routes.count("Depends(require_worker_write)") == 2,
          f"全权 {write_routes.count('Depends(require_write)')} 处 / 执行方 {write_routes.count('Depends(require_worker_write)')} 处")
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

    # 1.8b2 Batch31：执行方口令档——验收权与管理权必须是两把钥匙。
    # 红线守的是「方向」：执行方档能写回执，但一个管理报文也投不出去，一条指令也下不了。
    check("Batch31 执行方口令有配置项（api_token_worker）", "api_token_worker" in cfg_py)
    check("Batch31 执行方档参与 resolve_role（不只是配了变量）",
          "ROLE_WORKER" in sec_py and "worker and hmac.compare_digest" in sec_py)
    # 只看白名单定义块本身，不看注释：注释里为了解释「为什么不许派单」必然会提到 task_dispatch
    wmt_block = sec_py.split("WORKER_MSG_TYPES = frozenset({", 1)[-1].split("})", 1)[0] \
        if "WORKER_MSG_TYPES = frozenset({" in sec_py else ""
    check("Batch31 回写报文白名单只含 4 类 worker→master",
          all(t in wmt_block for t in ("task_heartbeat", "task_return", "task_reject", "task_abort"))
          and "task_dispatch" not in wmt_block
          and "human_confirm" not in wmt_block
          and "task_summary" not in wmt_block,
          f"白名单块：{wmt_block.strip()[:80]}")
    check("Batch31 报文入口按角色收敛方向（执行方档投不出派单）",
          "ROLE_WORKER and msg_type not in WORKER_MSG_TYPES" in
          read(os.path.join(ROOT, "app", "api", "routes_messages.py")))
    check("Batch31 管理指令仍只认全权（commands 没被降档）",
          read(os.path.join(ROOT, "app", "api", "routes_commands.py")).count("Depends(require_write)") == 1)
    check("Batch31 执行方口令进 .env 时独立成变量",
          "API_TOKEN_WORKER" in inst2 or "API_TOKEN_WORKER" in sec_py,
          "环境变量名没落地")
    # 1.8b3 Batch31-B：岗位一眼可辨。角色从 instance_id 推导，不进协议、不加接口字段。
    check("Batch31 看板按 instance_id 前缀推导岗位",
          "WORKER_ROLES" in app_js and "function workerRole" in app_js
          and "function workerInstanceCell" in app_js)
    check("Batch31 执行方表用岗位格子渲染（不是裸文本）",
          "workerInstanceCell(w)" in app_js)
    check("Batch31 岗位方章有样式（与状态章分开）", ".role-chip" in css2)
    check("Batch31 岗位映射覆盖六个业务岗 + 三管理岗",
          all(r in app_js for r in ("stock", "doc", "viz", "news", "dev", "qa", "ops", "mgmt")),
          "岗位集合不全")

    # 1.8b4 Batch31 试点：首单真派出去后暴露的三条，全部转成红线。
    # ① 验收路径原本是空的 —— 状态枚举里有「验收通过」，但没有任何报文能写它，
    #    六步 SOP 的第 5 步（验收）实际不成立。
    # ② Mac 上 launchd 直接 exec 一个非可执行脚本会报 EX_CONFIG 78 并进 penalty box 反复重试；
    #    plist 显式用 /bin/bash 调启动器，别指望 git 里保留可执行位。
    # ③ 脚本要能在 system python 3.9 下跑（这台 Mac 没有 homebrew python，交互 shell 里那个
    #    3.14 是跑工具自带的，launchd 环境看不见）。
    ledger_py31 = read(os.path.join(ROOT, "app", "repositories", "ledger.py"))
    dispatch_py31 = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    schemas_py31 = read(os.path.join(ROOT, "app", "schemas", "__init__.py"))
    snap_py31 = read(os.path.join(ROOT, "scripts", "daily_snapshot.py"))
    check("Batch31 验收通过有写入路径（台账 + 报文模型 + 处理器三处对齐）",
          "def accept_pending_tasks" in ledger_py31
          and "accept_tasks" in schemas_py31
          and "accept_pending_tasks" in dispatch_py31,
          "验收通过仍然没人能写")
    check("Batch31 plist 用 /bin/bash 调启动器（不依赖可执行位）",
          "<string>/bin/bash</string>" in mac_install_py,
          "plist 仍是直接 exec 脚本")
    check("Batch31 快照脚本能在 system python 3.9 下跑（延迟注解）",
          "from __future__ import annotations" in snap_py31,
          "缺 future import，3.9 会报 PEP604")

    # 1.8f Batch32-A：Windows 收编开关与设计稿在位。
    # 收编那台机器只改了安装脚本三个开关（岗位名、口令档、只重生成 bat），
    # 红线守的是「以后换一台机器也能照单执行」，别让这三个开关被删回去。
    check("Batch32 安装脚本支持 -InstanceId（岗位名可传）", '$InstanceId = ""' in inst2)
    check("Batch32 安装脚本支持 -UseWorkerToken（口令档可切）",
          '[switch]$UseWorkerToken' in inst2
          and '$TokenKey = if ($UseWorkerToken) { "API_TOKEN_WORKER" } else { "API_TOKEN" }' in inst2,
          "口令键没按开关切换")
    check("Batch32 安装脚本支持 -OnlyBat（只重生成 bat、不动计划任务）",
          "[switch]$OnlyBat" in inst2 and "if ($OnlyBat) {" in inst2)
    check("Batch32 岗位名非空时真的落进 worker 命令行",
          '$ArgList += @("--instance-id", $InstanceId)' in inst2)
    doc09 = read(os.path.join(ROOT, "docs", "base", "09-Worker契约.md"))
    doc15 = read(os.path.join(ROOT, "docs", "base", "15-岗位与权限.md"))
    check("Batch32 老 Worker 兼容策略写进 Worker 契约（有意隔离）",
          "岗位与实例级路由" in doc09 and "有意隔离" in doc09, "09 号文档缺第八节")
    check("Batch32 Windows 收编记录写进岗位文档（十一·四 与 ops-win 都在）",
          "十一·四" in doc15 and "ops-win" in doc15, "15 号文档没记录收编")
    check("Batch32 异构助手编队实测写进岗位文档（十一·六）",
          "十一·六" in doc15 and "headless" in doc15, "15 号文档缺编队实测")
    design16 = os.path.join(ROOT, "docs", "base", "16-异构复核与插件沉淀.md")
    check("Batch32 异构复核设计稿在位", os.path.exists(design16))
    if os.path.exists(design16):
        d16 = read(design16)
        check("Batch32 设计稿含复核三列（review_of / review_verdict / review_items）",
              all(k in d16 for k in ("review_of", "review_verdict", "review_items")), "三列没写全")
        check("Batch32 设计稿守住四条防作弊约束",
              all(k in d16 for k in ("与执行者不同", "不递归", "条数", "只写一次")),
              "约束缺项")
        check("Batch32 设计稿不改接口面（只加 TaskView 字段）",
              "不加路径" in d16, "设计稿没说接口面不动")
    idx00 = read(os.path.join(ROOT, "docs", "base", "00-索引目录.md"))
    check("Batch32 设计稿在索引里登记过", "16-异构复核与插件沉淀" in idx00)

    # 1.8h Batch32-1：异构复核落地的四条防作弊，一条都不许回退。
    # 红线守的是「方向」：复核由别人做、标准逐条答满、结论只写一次、unreviewed 不放行。
    migrate_dir = os.path.join(ROOT, "migrations", "versions")
    check("Batch32-1 迁移 v011 在位", os.path.exists(os.path.join(migrate_dir, "v011_task_review.py")))
    model32 = read(os.path.join(ROOT, "app", "models", "__init__.py"))
    check("Batch32-1 台账三列齐全（review_of / review_verdict / review_items）",
          all(k in model32 for k in ("review_of", "review_verdict", "review_items")), "缺列")
    dispatch32 = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    check("Batch32-1 回执后自动派生复核单",
          "def _spawn_review" in dispatch32 and "task.task_id}-rv" in dispatch32)
    check("Batch32-1 复核者 ≠ 执行者（同实例不建单、标 unreviewed）",
          "复核者与执行者同实例" in dispatch32 and '"unreviewed"' in dispatch32,
          "防作弊第 1 条没落地")
    check("Batch32-1 复核单不再派生复核单（review_of 非空即跳过）",
          "复核单不再派生复核单" in dispatch32
          and 'getattr(task, "review_of", None)' in dispatch32,
          "防作弊第 2 条没落地")
    check("Batch32-1 条数必须对齐（少一条即 fail，且点出缺失编号）",
          "def _evaluate" in dispatch32 and "缺失项" in dispatch32, "防作弊第 3 条没落地")
    check("Batch32-1 结论只写一次（二次回执 409）",
          "复核结论已写入" in dispatch32 and "不允许改写" in dispatch32, "防作弊第 4 条没落地")
    ledger32 = read(os.path.join(ROOT, "app", "repositories", "ledger.py"))
    check("Batch32-1 签收闸门（只推 pass/null，unreviewed 需 force_accept）",
          'verdict == "unreviewed" and not force_accept' in ledger32
          and 'verdict == "fail"' in ledger32, "闸门没加，签收会把没人审的活混过去")
    check("Batch32-1 复核结论只写一次的仓储层实现",
          "def set_review_verdict" in ledger32 and "return False" in ledger32,
          "缺「已定案不覆盖」分支")
    check("Batch32-1 挑复核者的排序口径写死（排除执行者 + 在线 + 最闲）",
          "def pick_reviewer" in ledger32 and "in_flight" in ledger32)
    review_py = os.path.join(ROOT, "scripts", "task_review.py")
    check("Batch32-1 规则复核脚本在位", os.path.exists(review_py))
    if os.path.exists(review_py):
        rt = read(review_py)
        check("Batch32-1 规则复核脚本纯标准库且离线可判",
              "import argparse" in rt and "--review-b64" in rt and "def judge" in rt
              and "import subprocess" not in rt, "脚本引了不该引的东西")
    http32 = read(os.path.join(ROOT, "app", "api", "schemas_http.py"))
    check("Batch32-1 TaskView 三字段（接口路径不变，只是返回体变厚）",
          all(k in http32 for k in ("review_of", "review_verdict", "review_items")))
    msg32 = read(os.path.join(ROOT, "app", "api", "routes_messages.py"))
    check("Batch32-1 TaskView 三个字段真的填值（不是只声明）",
          "review_items=_review_items(task)" in msg32)
    js32 = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("Batch32-1 看板有复核列（unreviewed 一眼可见）",
          "function renderReview" in js32 and '"review"' in js32, "前端没有复核列")
    css32 = read(os.path.join(ROOT, "app", "static", "style.css"))
    check("Batch32-1 复核章有样式（pass/fail/warn 三色分开）",
          ".rv-chip.rv-pass" in css32 and ".rv-chip.rv-fail" in css32 and ".rv-chip.rv-warn" in css32)
    run_all32 = read(os.path.join(ROOT, "scripts", "run_all.py"))
    check("Batch32-1 验收脚本进全量测试清单", "verify_b32.py" in run_all32)

    # 1.8i Batch32-2A：插件沉淀只读侧。红线守「不进审核就不许发」：
    # 元数据必填、版本不回退、新版本必须重审、授权与撤权不许同列一个人。
    plug_index = os.path.join(ROOT, "plugins", "index.json")
    check("Batch32-2 插件索引在位", os.path.exists(plug_index))
    if os.path.exists(plug_index):
        try:
            plug_data = json.loads(read(plug_index))
        except ValueError:
            plug_data = {}
        check("Batch32-2 索引结构合法（plugins 数组 + 每项五字段）",
              isinstance(plug_data.get("plugins"), list) and plug_data["plugins"]
              and all(k in plug_data["plugins"][0]
                      for k in ("name", "latest", "versions", "review_state", "granted_to")),
              "索引结构不对")
        check("Batch32-2 索引里的状态值域只有 draft/reviewed/revoked",
              all(p.get("review_state") in ("draft", "reviewed", "revoked")
                  for p in plug_data.get("plugins", [])))
    plug_dir = os.path.join(ROOT, "plugins", "deploy-smoke")
    check("Batch32-2 插件四件套齐全（PLUGIN/SKILL/CHANGELOG/probes）",
          all(os.path.exists(os.path.join(plug_dir, f))
              for f in ("PLUGIN.md", "SKILL.md", "CHANGELOG.md", "probes.py")))
    plug_md = read(os.path.join(plug_dir, "PLUGIN.md")) if os.path.exists(plug_index) else ""
    check("Batch32-2 PLUGIN.md 八项必填字段齐全",
          all(k + ":" in plug_md for k in
              ("name", "version", "owner", "applies_to", "inputs", "outputs",
               "criteria_template", "reviewer_hint")), "元数据缺字段")
    check("Batch32-2 PLUGIN.md 每条标准可拆（criteria 用分号分隔的多条）",
          len([x for x in plug_md.split("criteria_template:", 1)[-1].split("\n")[0]
               .replace("；", ";").split(";") if x.strip()]) >= 2,
          "验收标准拆不出两条以上")
    review32a = read(os.path.join(ROOT, "scripts", "plugin_review.py"))
    check("Batch32-2 校验器纯标准库（不引 requests/yaml 一类第三方）",
          "import subprocess" in review32a and "import json" in review32a
          and "yaml" not in review32a and "requests" not in review32a)
    check("Batch32-2 必填字段与 semver 在代码里硬校验",
          "PLUGIN.md 缺必填字段" in review32a and "不是三段 semver" in review32a)
    check("Batch32-2 版本回退与「新版本必须重审」写死",
          "版本回退" in review32a and "review_state 必须是 draft" in review32a,
          "版本口径没落地")
    check("Batch32-2 授权与撤权不许同列一个实例",
          "同一实例既在 granted_to 又在 revoked_instances" in review32a and "overlap" in review32a,
          "授权互斥没落地")
    check("Batch32-2 只有校验全过才写索引（--apply 不绕过红线）",
          "if ok and args.apply" in review32a and "def apply_to_index" in review32a)
    probes_py = os.path.join(plug_dir, "probes.py")
    check("Batch32-2 probes 探针三判据在位（健康/迁移/文档）",
          os.path.exists(probes_py)
          and all(k in read(probes_py) for k in ("_check_health", "_check_schema", "_check_docs")))
    if os.path.exists(probes_py):
        try:
            py_compile.compile(probes_py, cfile=os.path.join(tmpdir, "probe.pyc"), doraise=True)
            probes_ok, probes_msg = True, ""
        except py_compile.PyCompileError as exc:
            probes_ok, probes_msg = False, str(exc)[:80]
        check("Batch32-2 probes.py 单独编译通过（不进主扫描、但也不许是坏语法）",
              probes_ok, probes_msg)
        proc = subprocess.run([sys.executable, probes_py, ROOT],
                              capture_output=True, text=True, timeout=60)
        check("Batch32-2 探针在本仓库上真跑得通（exit 0）",
              proc.returncode == 0, (proc.stdout or proc.stderr or "")[-100:])
    check("Batch32-2 验收脚本进全量测试清单",
          "verify_b32_2a.py" in read(os.path.join(ROOT, "scripts", "run_all.py")))

    # 1.8j Batch32-2B：派单侧授权。红线守「没授权不许派、拒了必须留痕、留痕不进台账」。
    plug_svc = read(os.path.join(ROOT, "app", "services", "plugins.py"))
    check("Batch32-2 授权判定三级口径写死（撤权 > 实例 > 岗位 > 拒）",
          "revoked_instances" in plug_svc and "granted_to" in plug_svc
          and "granted_roles" in plug_svc and "撤权优先于一切授权" in plug_svc,
          "三级判定没落地")
    check("Batch32-2 岗位口径与看板一致（instance 前缀）",
          "def instance_role" in plug_svc and 'split("-", 1)' in plug_svc,
          "岗位推导与看板方章口径不一致")
    check("Batch32-2 只放 reviewed 的插件（draft/revoked 一律拒）",
          'state != "reviewed"' in plug_svc and "未过审核" in plug_svc)
    check("Batch32-2 索引热改即生效（按 mtime+size 缓存）",
          "st_mtime_ns" in plug_svc and "MWP_PLUGINS_DIR" in plug_svc)
    dispatch32b = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    check("Batch32-2 派单带 plugin 时先判授权再建流水线/任务",
          "def _gate_plugin" in dispatch32b and "def _resolve_plugin" in dispatch32b
          and dispatch32b.index("_gate_plugin(db, repo, envelope") <
          dispatch32b.index("repo.create_pipeline("),
          "授权判定排在建流水线之后，拒单会留假流水线")
    check("Batch32-2 带 plugin 必须指定岗位（否则拒）",
          "必须指定 target_instance" in dispatch32b)
    check("Batch32-2 拒单抛错前审计已落库（自己 commit，不回滚）",
          "自己 commit" in read(os.path.join(ROOT, "app", "repositories", "ledger.py"))
          and "record_plugin_audit" in read(os.path.join(ROOT, "app", "repositories", "ledger.py")))
    model32b = read(os.path.join(ROOT, "app", "models", "__init__.py"))
    check("Batch32-2 审计表 append-only 且 decision 两值",
          "class PluginAudit" in model32b and "ck_plugin_audit_decision" in model32b)
    check("Batch32-2 迁移 v012 在位",
          os.path.exists(os.path.join(migrate_dir, "v012_plugin_audit.py")))
    schema32b = read(os.path.join(ROOT, "app", "schemas", "__init__.py"))
    check("Batch32-2 派发载荷有 plugin 字段（接口路径不变，只加字段）",
          "plugin: Optional[str]" in schema32b)
    check("Batch32-2 派单侧验收脚本进全量测试清单",
          "verify_b32_2b.py" in read(os.path.join(ROOT, "scripts", "run_all.py")))
    audit_cli = os.path.join(ROOT, "scripts", "plugin_audit.py")
    check("Batch32-2 审计有只读查询入口（不改接口面就能查被拒单）",
          os.path.exists(audit_cli)
          and "--only-deny" in read(audit_cli)
          and "import sqlite3" in read(audit_cli), "审计查不了等于没留痕")

    # 1.8k Batch32-3：探针进派发链 + 审计两源合并上板。红线守「授权不过不跑探针、
    # 探针不过必拒、一单只留一行审计、审计面板不许只看配额不看插件」。
    check("Batch32-3 一级规则复核真的起子进程跑 probes（带超时，不靠人肉）",
          "def run_probes" in plug_svc and "subprocess.run(" in plug_svc
          and "TimeoutExpired" in plug_svc and "probes.py" in plug_svc,
          "probes 没接到派发链上")
    # 只切 _gate_plugin 这一个函数体（Batch34 在它后面加了 _gate_dsh，别把两家的审计混着数）
    gate_body = dispatch32b.split("def _gate_plugin", 1)[-1].split("\ndef ", 1)[0]
    check("Batch32-3 探针闸门只在授权放行后跑（没资格就不动执行环境）",
          gate_body.index("judge_plugin(") < gate_body.index("run_probes(")
          and 'if decision == "allow":' in gate_body,
          "探针跑在授权判定之前")
    check("Batch32-3 探针不过改判 deny，理由写进审计",
          "一级规则复核未通过" in gate_body and "decision = \"deny\"" in gate_body,
          "探针失败没有落成拒单")
    gate_tail = gate_body[gate_body.index("verdict = judge_plugin("):]
    check("Batch32-3 一单一条审计（判定 + 探针只写最终结论一行）",
          gate_tail.count("record_plugin_audit(") == 1
          and gate_tail.index("record_plugin_audit(") > gate_tail.index("run_probes("),
          f"判定后写了 {gate_tail.count('record_plugin_audit(')} 行审计")
    routes_audit_py = read(os.path.join(ROOT, "app", "api", "routes_audit.py"))
    check("Batch32-3 审计查询把插件审计也并进来（接口路径不变）",
          "PluginAudit" in routes_audit_py and "WorkerQuotaAudit" in routes_audit_py
          and "entries[offset:offset + limit]" in routes_audit_py,
          "审计只读一边，插件拒单看不见")
    check("Batch32-3 插件行带 plugin/decision，且不冒充配额行",
          "class AuditItem" in routes_audit_py
          and "prev_limit: Optional[int]" in routes_audit_py
          and "plugin: Optional[str]" in routes_audit_py,
          "插件行还是套配额字段")
    appjs32 = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("Batch32-3 看板运维审计面板认得插件行（不再是空白/乱码）",
          "plugin:" in appjs32 and "已拒" in appjs32 and "已放行" in appjs32,
          "前端没渲染插件行")
    doc15 = read(os.path.join(ROOT, "docs", "base", "15-岗位与权限.md"))
    check("Batch32-3 岗位文档登记插件授权口径（撤权 > 实例 > 岗位 > 拒）",
          "插件授权口径" in doc15 and "撤权 > 实例 > 岗位 > 拒" in doc15,
          "授权口径只活在代码里，文档没登记")

    # 1.8l Batch32-4：看板时间不许差 8 小时。流水线时间必须带时区再出去。
    queries_py = read(os.path.join(ROOT, "app", "api", "routes_queries.py"))
    check("Batch32-4 流水线时间统一带时区（ensure_utc，不许 naive 直出）",
          "ensure_utc(pipeline.start_time).isoformat()" in queries_py
          and "ensure_utc(pipeline.end_time).isoformat()" in queries_py
          and "pipeline.start_time.isoformat()" not in queries_py,
          "start_time 还是 naive，看板滞留会多算 8 小时")
    check("Batch32-4 时间口径写进 07 号文档（哪些 UTC、哪些本地，标清楚）",
          "附3：时间字段的口径" in read(os.path.join(ROOT, "docs", "base", "07-台账两表字段.md")),
          "口径只活在代码里")

    # 1.8m Batch33：dsh 编队——外部 agent 走适配层，不动用户自己的 ~/.dsh。
    # 红线守的是「方向」：patch 在仓库里、key 只进子进程 env、adapter 分支排在建命令之前。
    dsh_adapter_py = read(os.path.join(ROOT, "worker", "dsh_adapter.py"))
    dsh_patch = read(os.path.join(ROOT, "worker", "dsh", "nvidia-patch.yml"))
    worker_py33 = read(os.path.join(ROOT, "worker", "agent_worker.py"))
    check("Batch33 nvidia patch 在仓库内且指向 NVIDIA 端点",
          "https://integrate.api.nvidia.com/v1" in dsh_patch
          and "apiKeyEnv: NVIDIA_API_KEY" in dsh_patch
          and "nemotron-3.5-lightning-30b-a3b" in dsh_patch,
          "patch 丢了：dsh headless 会退回 DeepSeek 路由")
    check("Batch33 命令固定叠 --profile headless + --patch",
          '"--profile"' in dsh_adapter_py and '"headless"' in dsh_adapter_py
          and '"--patch"' in dsh_adapter_py,
          "命令形状被改了，patch 就不生效")
    check("Batch33 key 只进子进程 env，不进仓库文件",
          'env["NVIDIA_API_KEY"] = key' in dsh_adapter_py
          and ("nvapi" + "-") not in dsh_adapter_py
          and ("nvapi" + "-") not in dsh_patch,
          "key 被硬编码或没塞进 env")
    check("Batch33 先判 adapter 再解析 script（顺序不许倒）",
          worker_py33.index('spec.get("adapter")') < worker_py33.index("resolve_script(spec.get"),
          "adapter 单会被当成 script 单拒掉")
    check("Batch33 只认 dsh，退出码说了算",
          "不认识的 adapter" in worker_py33 and 'f"dsh 退出码 {result' in worker_py33,
          "adapter 白名单或失败判据被放宽了")
    check("Batch33 exec.adapter 写进 09 号契约（载荷形状不靠口口相传）",
          "exec.adapter" in read(os.path.join(ROOT, "docs", "base", "09-Worker契约.md")),
          "契约文档没写 adapter 单的形状")

    # 1.8n Batch33-1：模型按单指定——仓库 patch 是模板，不就地改；临时 patch 按模型缓存。
    dsh_doc09 = read(os.path.join(ROOT, "docs", "base", "09-Worker契约.md"))
    check("Batch33-1 模型名有白名单校验（拒 .. 与空格）",
          "MODEL_RE" in dsh_adapter_py and "def check_model" in dsh_adapter_py
          and '".." in value' in dsh_adapter_py,
          "模型名没校验，会被拼进文件路径或 yml")
    check("Batch33-1 仓库 patch 只当模板，渲染结果落 run/tmp_dsh",
          "TMP_PATCH_ROOT" in dsh_adapter_py
          and '"tmp_dsh"' in dsh_adapter_py
          and "def resolve_patch" in dsh_adapter_py,
          "按单模型改写仓库 patch 就回滚不掉了")
    check("Batch33-1 优先级：按单 > MWP_DSH_MODEL > 默认",
          "MWP_DSH_MODEL" in dsh_adapter_py and "def resolve_model" in dsh_adapter_py,
          "环境变量没接进解析链")
    check("Batch33-1 Worker 把按单 model 传进适配层",
          'model=spec.get("model")' in worker_py33,
          "exec.model 被吞掉了")
    check("Batch33-1 exec.model 写进 09 号契约",
          "exec.model" in dsh_doc09 and "MWP_DSH_MODEL" in dsh_doc09,
          "契约没写 model 字段与优先级")
    check("Batch33-1 dsh 运行 home 收进仓库（不许让 Worker 改用户 ~/.dsh）",
          "RUNTIME_HOME" in dsh_adapter_py and '"dsh_home"' in dsh_adapter_py
          and 'env["DSH_HOME"] = home' in dsh_adapter_py,
          "DSH_HOME 还指着用户的 home，dsh 每次启动都会改写它")
    check("Batch33-1 凭证仍只读用户 ~/.dsh（运行 home 不接管凭证）",
          'os.path.join(user_dsh_home(), ".credentials.yaml")' in dsh_adapter_py,
          "凭证被搬到运行 home 就等于把 key 复制进仓库")
    check("Batch33-1 缓存只增不爆：按天清 patch-*.yml，删不掉也不抛",
          "def prune_cache" in dsh_adapter_py and "prune_cache()" in dsh_adapter_py
          and 'startswith("patch-")' in dsh_adapter_py
          and "except OSError" in dsh_adapter_py,
          "缓存没有清理口，或清理会把别的文件一起带走")

    # 1.8o Batch33-2：产物留档 + 思考档。产出不能只活在回执里；任务号不许把文件带出目录。
    artifacts_svc_py = read(os.path.join(ROOT, "app", "services", "artifacts.py"))
    app_js33_2 = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("Batch33-2 每次执行都落一份产物文件（run/artifacts/<task_id>.log）",
          "def write_artifact" in dsh_adapter_py and '"artifacts"' in dsh_adapter_py
          and "write_artifact(task_id" in dsh_adapter_py,
          "产物只在回执里，任务一结束就查不到")
    check("Batch33-2 产物字节与哈希按落盘后的真实字节算",
          'with open(full, "rb") as fh' in dsh_adapter_py
          and "artifact_sha256" in dsh_adapter_py,
          "按内存文本算哈希，Windows 换行一改就对不上")
    check("Batch33-2 任务号不许把产物带出目录（连点一律洗掉）",
          're.sub(r"\\.{2,}"' in dsh_adapter_py and "lstrip" in dsh_adapter_py,
          ".. 能穿过目录，产物会写到 run/artifacts 之外")
    check("Batch33-2 思考档有白名单（minimal/low/medium/high）",
          "EFFORT_VALUES" in dsh_adapter_py and "def check_effort" in dsh_adapter_py,
          "思考档不校验就会被当自由文本写进 yml")
    check("Batch33-2 思考档写在 agent-default-model 段（只一处）",
          "reasoningEffort:" in dsh_adapter_py and "anchor" in dsh_adapter_py,
          "写错段位会让 provider 配置也跟着变")
    check("Batch33-2 Worker 把 task_id 与 effort 都传下去",
          "task_id=task_id" in worker_py33 and 'effort=spec.get("reasoning_effort")' in worker_py33,
          "产物没有任务号就分不清是谁的，思考档也传不进去")
    check("Batch33-2 平台产物索引只认 run/artifacts 下的 .log（按目录 mtime 缓存）",
          "def scan" in artifacts_svc_py and 'endswith(".log")' in artifacts_svc_py
          and "_dir_key" in artifacts_svc_py,
          "每刷一次台板就全盘 stat，或把别的文件也当产物")
    check("Batch33-2 产物挂在 TaskView 上、不加接口路径（铁律仍 11 条）",
          "artifact_bytes" in read(os.path.join(ROOT, "app", "api", "schemas_http.py"))
          and not os.path.isfile(os.path.join(ROOT, "app", "api", "routes_artifacts.py")),
          "为产物开了新路径，就要先改接口面红线并征得同意")
    check("Batch33-2 看不到产物就不写路径（不编）",
          'art.get("path") or None' in read(os.path.join(ROOT, "app", "api", "routes_messages.py")),
          "查不到就编，等于把台账当小说写")
    check("Batch33-2 产物有按天清理（默认 30 天，只清 .log，删不掉不抛）",
          "def prune_artifacts" in dsh_adapter_py and "ARTIFACT_DAYS = 30.0" in dsh_adapter_py
          and "prune_artifacts()" in dsh_adapter_py and 'endswith(".log")' in dsh_adapter_py,
          "产物只增不删，迟早把磁盘塞满")
    check("Batch33-2 产物全文走已有路径 + 查询参数（不新增路径）",
          "include_artifact" in read(os.path.join(ROOT, "app", "api", "routes_messages.py"))
          and "include_artifact=true" in app_js33_2,
          "为了看全文又开了新路径，等于把接口面偷偷扩了")
    check("Batch33-2 看板有产物签，且不为它发请求",
          "renderArtifactChip" in app_js33_2 and "ROUTES.artifacts" not in app_js33_2,
          "看板显示产物要么走新接口，要么干脆没有")
    check("Batch33-2 exec.reasoning_effort 与产物字段写进 09 号契约",
          "reasoning_effort" in dsh_doc09 and "artifact_sha256" in dsh_doc09,
          "契约没写，下一个人只能靠猜")

    # 1.8p Batch33-4：按模型拦思考档——不支持档位的模型，在本地就拒，不发注定失败的单。
    check("Batch33-4 有一张实测名单（哪些模型不收 thinking 档）",
          "EFFORT_UNSUPPORTED_PREFIXES" in dsh_adapter_py and "minimaxai/" in dsh_adapter_py,
          "拦思考档却不知道拦谁，等于没拦")
    check("Batch33-4 模型与档位定稿前过 guard_effort（不是让上游去拒）",
          "def guard_effort" in dsh_adapter_py and "guard_effort(target, effort)" in dsh_adapter_py,
          "只在 run() 里拦一处，别的入口照样能把注定失败的单发出去")
    check("Batch33-4 拒单是 DshCapabilityError（跟起不来区分开）",
          "class DshCapabilityError(DshError)" in dsh_adapter_py
          and "isinstance(exc, dsh_adapter.DshCapabilityError)" in read(
              os.path.join(ROOT, "worker", "agent_worker.py")),
          "拒单和崩溃写同一句话，事后分不清是选错了还是坏了")
    check("Batch33-4 名单外模型不硬拦（默认放行，不做支持白名单）",
          "return not any(" in dsh_adapter_py and "SUPPORTED_MODELS" not in dsh_adapter_py,
          "做成支持白名单，新模型一上线就被误拒")
    check("Batch33-4 排障开关只认 MWP_DSH_EFFORT_FORCE（并写进契约）",
          "MWP_DSH_EFFORT_FORCE" in dsh_adapter_py and "MWP_DSH_EFFORT_FORCE" in dsh_doc09,
          "偷偷留后门，下次就没人知道它开着")

    # 1.8q Batch34：dsh 挂进真实岗位——岗位必填、单实例并发配额、审计留痕（都在派单闸里）。
    dispatch_py34 = read(os.path.join(ROOT, "app", "services", "dispatch.py"))
    ledger_py34 = read(os.path.join(ROOT, "app", "repositories", "ledger.py"))
    check("Batch34 dsh 单在派单前过闸（没岗位直接拒，不许落到默认实例）",
          "def _gate_dsh" in dispatch_py34 and "必须指定 target_instance" in dispatch_py34,
          "只写函数不调用，配额等于没设")
    check("Batch34 闸在建流水线之前（被拒的单不建流水线、不占任务行）",
          dispatch_py34.find("_gate_dsh(repo, envelope, payload, target_worker, target_instance)")
          < dispatch_py34.find("pipeline = repo.get_pipeline(envelope.trace_id)"),
          "先建流水线再拒单，拒一次就多一条孤儿流水线")
    check("Batch34 单实例 dsh 并发有上限（默认 1，MWP_DSH_MAX_INFLIGHT 可调，下限 1）",
          "DSH_MAX_INFLIGHT = 1" in dispatch_py34 and "max(1, value)" in dispatch_py34,
          "没有上限，外部 agent 单可以无限堆")
    check("Batch34 配额只数执行中 + adapter=dsh 的单（普通单不误伤）",
          "def count_inflight" in ledger_py34 and 'spec.get("adapter")' in ledger_py34
          and 'TaskLedger.status == "执行中"' in ledger_py34,
          "把脚本单也算进外部队配额，等于把正常活挡了")
    check("Batch34 派单判定写审计（allow 与 deny 都留痕）",
          'audit("allow", head)' in dispatch_py34 and 'audit("deny"' in dispatch_py34,
          "只记拒单，事后查不到放行依据")
    check("Batch34 配额/审计不新增接口路径（铁律仍 11 条）",
          not os.path.isfile(os.path.join(ROOT, "app", "api", "routes_quota.py")),
          "为了查配额又开一条路径，等于偷偷扩接口面")
    check("Batch34 Linux 上 PATH 里的 dsh 能自动找到（只有 Windows 认 APPDATA 等于漏了一半机器）",
          'shutil.which("dsh")' in dsh_adapter_py and "import shutil" in dsh_adapter_py,
          "云主机上 dsh 是全局 npm 装的，只认 ~/.dsh/profiles 会白报找不到")

    # 1.8b5 Batch35：产物随回执回传——执行方在别的机器上时，平台也要看得见产物。
    # 红线守「回传范围有边界、超长要截、平台只补不覆盖、只在回执里落，且接口面不动」。
    artifacts_py35 = read(os.path.join(ROOT, "app", "services", "artifacts.py"))
    worker_py35 = read(os.path.join(ROOT, "worker", "agent_worker.py"))
    check("Batch35 Worker 只回传 run/artifacts 下的产物（不把任意文件送上总线）",
          'startswith("run/artifacts/")' in worker_py35 and '".." in rel' in worker_py35,
          "回传范围没设边界，等于给 Worker 开了个读全盘的洞")
    check("Batch35 超长截到 8000 字并标明截断",
          "ARTIFACT_INLINE_MAX = 8000" in worker_py35 and "art_truncated" in worker_py35,
          "整份产物塞进报文，大产物会把总线撑爆")
    check("Batch35 平台补本机副本时不覆盖本机已有产物",
          "def save(" in artifacts_py35
          and "if lookup(tid):" in artifacts_py35
          and "if os.path.exists(full):" in artifacts_py35,
          "同机执行时 Worker 写的那份才是原始字节，不许被回传内容盖掉")
    check("Batch35 只在 task_return 里落盘（且这个函数不许被挂上幂等装饰器）",
          "_persist_remote_artifact(envelope.task_id, payload)" in dispatch_py34
          and "@_idempotent\ndef _persist_remote_artifact" not in dispatch_py34,
          "落盘挂在错的位置，会拿 handler 的 envelope 当 payload 用")

    # 1.8c Batch29：优雅停止——停止标记是整机级的，只允许一个实现，

    # 不许各处自己拼 "data/STOP"；worker 只读标记，不写、不删。
    routes_commands_py = read(os.path.join(ROOT, "app", "api", "routes_commands.py"))
    stopflag_py = read(os.path.join(ROOT, "app", "core", "stopflag.py"))
    worker_py29 = read(os.path.join(ROOT, "worker", "agent_worker.py"))
    guide11 = read(os.path.join(ROOT, "docs", "base", "11-启动自检清单.md"))
    check("Batch29 master:stop 在指令白名单",
          '"master:stop"' in routes_commands_py
          and "master:stop" in routes_commands_py.split("ALLOWED_COMMANDS", 1)[-1][:200],
          "白名单里没有 master:stop")
    check("Batch29 停止标记只有一个实现（stopflag 模块）",
          "def request_stop" in stopflag_py and "def clear_stop" in stopflag_py
          and "STOP_FILE_NAME" in stopflag_py,
          "stopflag.py 不完整")
    check("Batch29 标记路径可用环境变量覆盖（测试不许写真实 data/）",
          "MWP_STOP_FILE" in stopflag_py and "MWP_STOP_FILE" in worker_py29,
          "MWP_STOP_FILE 没接上")
    check("Batch29 worker 认停止标记（只读不写）",
          "def should_stop" in worker_py29 and "should_stop(" in worker_py29
          and "stop_flag_seen" in worker_py29
          and "os.remove(" not in worker_py29.split("def should_stop", 1)[-1][:200],
          "worker 没轮询停止标记")
    check("Batch29 优雅停止写进自检清单（N 节）",
          "## N." in guide11 and "master:stop" in guide11,
          "docs/base/11 没有 N 节")

    # 1.8d Batch30：看板上的「优雅停止」按钮——前端只是入口，服务端仍是唯一闸门；
    # 只读态必须一起置灰（别让人白点一次才发现被 403）。
    html30 = read(os.path.join(ROOT, "app", "static", "index.html"))
    css30 = read(os.path.join(ROOT, "app", "static", "style.css"))
    js30 = read(os.path.join(ROOT, "app", "static", "app.js"))
    check("Batch30 看板有「优雅停止」按钮",
          'id="btnStop"' in html30 and "优雅停止" in html30, "index.html 没按钮")
    check("Batch30 优雅停止按钮在只读态一起置灰",
          '"btnStop"' in js30.split("WRITE_BTNS =", 1)[-1][:120], "WRITE_BTNS 没带 btnStop")
    check("Batch30 点击走 master:stop（整机级，不带 trace_id）",
          "function stopMaster" in js30 and '"master:stop"' in js30
          and "btnStop" in js30.split("function bind()", 1)[-1][:4000],
          "stopMaster 或绑定缺失")
    check("Batch30 优雅停止按钮有告警色样式", "button.btn-warn" in css30, "style.css 缺 .btn-warn")

    # 1.8e Batch30-B：每日快照生成器——它要能被"另一台机器/无人值守"跑起来，
    # 所以必须零依赖（纯标准库）、必须自带 selftest、且取数口径必须对齐真实接口。
    snap_path = os.path.join(ROOT, "scripts", "daily_snapshot.py")
    snap_py = read(snap_path) if os.path.exists(snap_path) else ""
    check("Batch30 每日快照脚本存在", bool(snap_py), "缺 scripts/daily_snapshot.py")
    banned_imports = ("import requests", "import fastapi", "import sqlalchemy",
                      "from app", "from worker", "import subprocess")
    check("Batch30 每日快照只用标准库",
          snap_py and not any(b in snap_py for b in banned_imports),
          "daily_snapshot.py 引了非标准库/项目模块")
    check("Batch30 每日快照自带 --selftest",
          snap_py and '"--selftest"' in snap_py and "def run_selftest" in snap_py,
          "缺 --selftest")
    # 分页包裹是 {meta, items} 且 pipelines 的分页参数叫 size；写回 limit 会静默取空表
    check("Batch30 每日快照按真实分页结构取数",
          snap_py and '.get("items")' in snap_py and "/api/pipelines?size=" in snap_py,
          "取数没解 items 或 pipelines 用了 limit")
    # 真实接口时间带 +08:00 / offset，与 datetime.now() 相减会 TypeError
    check("Batch30 每日快照时间统一归一到本地",
          snap_py and "astimezone().replace(tzinfo=None)" in snap_py,
          "parse_iso 没做时区归一")
    guide14_path = os.path.join(ROOT, "docs", "base", "14-每日快照.md")
    check("Batch30 每日快照有配套文档",
          os.path.exists(guide14_path) and "daily_snapshot.py" in read(guide14_path),
          "缺 docs/base/14-每日快照.md")
    # 无人值守要能自己拿口令（计划任务不继承交互式环境变量）
    check("Batch30 每日快照能自己从 .env 取口令",
          snap_py and "def load_env_token" in snap_py and "load_env_token()" in snap_py,
          "没做 .env 回退，计划任务里会 401")
    # Batch28 起鉴权是两枚开关：冒烟只清一枚就会整片 401（Batch28 的老坑，这里锁死）
    smoke_py30 = read(os.path.join(ROOT, "scripts", "smoke_http.py"))
    check("Batch30 冒烟脚本清掉两枚鉴权开关（含 READONLY）",
          'env["API_TOKEN"] = ""' in smoke_py30 and 'env["API_TOKEN_READONLY"] = ""' in smoke_py30,
          "smoke_http 只清了一枚开关")

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
