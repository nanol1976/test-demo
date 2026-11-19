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
    check("接口路径为 10 个（Batch13 加执行方注册表）", len(unique_paths) == 10,
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
    check("Worker 并发用线程池且有上限", "ThreadPoolExecutor" in worker_py and "max_workers=self.concurrency" in worker_py)
    templates = {
        "deploy/worker-systemd.service": ["Restart=always", "TimeoutStopSec=90", "agent_worker.py"],
        "deploy/worker-launchd.plist": ["KeepAlive", "ExitTimeOut", "agent_worker.py"],
        "deploy/worker-windows-task.ps1": ["Register-ScheduledTask", "agent_worker.py", "RestartCount"],
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
    check("Worker 支持 --registry-interval 与 --no-register",
          "--registry-interval" in worker_py and "--no-register" in worker_py)

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
