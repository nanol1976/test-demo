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
import os
import py_compile
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

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
    targets = [os.path.join(ROOT, p) for p in ("app", "scripts", "deploy", "migrations")]
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

    # 1.2 业务词（只扫代码与迁移；docs/base 里出现业务词是刻意的——那是在写"平台不做什么"的边界示例）
    hits = []
    for path in list(walk_py([os.path.join(ROOT, "app"), os.path.join(ROOT, "migrations")])):
        text = read(path)
        for w in BUSINESS_WORDS:
            if w in text:
                hits.append(f"{os.path.relpath(path, ROOT)}:{w}")
    check("底座业务无关（代码/迁移业务词 0 命中）", not hits, ", ".join(hits[:5]))

    # 1.3 删除调用
    dels = []
    for path in walk_py([os.path.join(ROOT, "app")]):
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
        for m in re.finditer(r"@router\.(get|post|put|delete|patch)\(", text):
            methods.add(m.group(1))
            routes.append(f"{fn}:{m.group(1)}")
    check("接口方法只有 GET/POST", methods <= {"get", "post"}, ",".join(sorted(methods)))
    check("接口数仍为 9", len(routes) == 9, f"{len(routes)} 个：{routes}")

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
    db_path = os.path.join(ROOT, "data", db_name)
    if os.path.exists(db_path):
        os.remove(db_path)
    url = f"sqlite:///./data/{db_name}"

    code, out = migrate_cli(["--check"], url)
    check("空库 --check 必须不通过", code == 1, f"exit={code}")

    code, out = migrate_cli([], url)
    check("upgrade 必须成功", code == 0, f"exit={code}")

    code, out = migrate_cli(["--check"], url)
    check("升级后 --check 必须通过", code == 0, f"exit={code}")

    code, out = migrate_cli([], url)
    check("重复 upgrade 幂等", code == 0 and "无待应用迁移" in out)

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
