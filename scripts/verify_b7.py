"""Batch7 验收脚本：迁移框架 + 看板自动刷新 + CI 门禁。

三类断言：
A. 迁移框架：发现/校验/空库升级/幂等/旧库增量/--to/--status/失败回滚
B. 看板自动刷新：页面元素、定时器、页面隐藏暂停、不覆盖用户输入
C. CI：脚本存在、静态门禁可跑、workflow 就位
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["DATABASE_URL"] = "sqlite:///./data/verify_b7.db"
os.environ["PYTHONPATH"] = ROOT

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from migrations import runner  # noqa: E402

PY = sys.executable
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def run_cli(args=None, **env_extra):
    """跑 deploy/migrate.py，返回 (exit_code, stdout+stderr)。"""
    env = dict(os.environ)
    env.update(env_extra)
    proc = subprocess.run(
        [PY, os.path.join(ROOT, "deploy", "migrate.py")] + (args or []),
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def fresh_db(name):
    path = os.path.join(ROOT, "data", name)
    if os.path.exists(path):
        os.remove(path)
    return f"sqlite:///./data/{name}", path


def make_version(dir_path, fname, version, note="x", up_body="pass", extra=""):
    path = os.path.join(dir_path, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"VERSION = {version}\nNOTE = {note!r}\n\ndef up(conn):\n{up_body}\n{extra}")
    return path


def main():
    print("=" * 60)
    print("A. 迁移框架")
    print("=" * 60)

    found = runner.discover()
    check("发现迁移文件（>=2 个）", len(found) >= 2, f"{[m.version for m in found]}")
    check("版本号单调递增", all(found[i].version < found[i + 1].version for i in range(len(found) - 1)))
    check("v001 是基线建表", any(m.version == 1 and "基线" in m.note for m in found))
    check("v002 是增量（索引）", any(m.version == 2 and "索引" in m.note for m in found))

    # A2 校验：版本重复
    tmp = tempfile.mkdtemp(prefix="mwp_mig_")
    try:
        make_version(tmp, "v010_a.py", 10, up_body="    pass")
        make_version(tmp, "v010_b.py", 10, up_body="    pass")
        try:
            runner.discover(tmp)
            check("重复版本号被拒绝", False, "没有报错")
        except runner.MigrationError as e:
            check("重复版本号被拒绝", "重复" in str(e), str(e))

        shutil.rmtree(tmp)
        tmp = tempfile.mkdtemp(prefix="mwp_mig_")
        make_version(tmp, "v011_a.py", 12, up_body="    pass")
        try:
            runner.discover(tmp)
            check("VERSION 与文件名不符被拒绝", False, "没有报错")
        except runner.MigrationError as e:
            check("VERSION 与文件名不符被拒绝", "不一致" in str(e), str(e))

        shutil.rmtree(tmp)
        tmp = tempfile.mkdtemp(prefix="mwp_mig_")
        with open(os.path.join(tmp, "v012_bad.py"), "w", encoding="utf-8") as f:
            f.write("VERSION = 12\nNOTE = 'bad'\n")
        try:
            runner.discover(tmp)
            check("缺少 up() 被拒绝", False, "没有报错")
        except runner.MigrationError as e:
            check("缺少 up() 被拒绝", "up" in str(e), str(e))

        # A3 失败迁移：版本不落库
        shutil.rmtree(tmp)
        tmp = tempfile.mkdtemp(prefix="mwp_mig_")
        make_version(tmp, "v013_boom.py", 13, up_body="    raise RuntimeError('故意失败')")
        url, path = fresh_db("verify_b7_boom.db")
        eng = create_engine(url, future=True)
        before = runner.current_version(eng)
        try:
            runner.upgrade(eng, versions_dir=tmp)
            check("失败迁移会抛异常", False, "没有抛异常")
        except RuntimeError:
            check("失败迁移会抛异常", True)
        check("失败迁移不写 schema_version", runner.current_version(eng) == before, f"{before} -> {runner.current_version(eng)}")
        eng.dispose()
        if os.path.exists(path):
            os.remove(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # A4 空库全流程（CLI）
    url, path = fresh_db("verify_b7.db")
    code, out = run_cli(args=["--check"])
    check("空库 --check 不通过（exit 1）", code == 1, f"exit={code}")
    check("空库 --check 说明缺表", "缺表" in out)
    check("空库 --check 列出待应用迁移", "v001" in out and "v002" in out)

    code, out = run_cli(args=[])
    check("空库 upgrade 成功（exit 0）", code == 0, f"exit={code}")
    check("upgrade 打印应用 v001", "v001" in out)
    check("upgrade 打印应用 v002", "v002" in out)

    eng = create_engine(url, future=True)
    tables = set(inspect(eng).get_table_names())
    check(
        "三张台账表已建",
        {"pipeline_ledger", "task_ledger", "message_archive"} <= tables,
        str(sorted(tables)),
    )
    idx = {i["name"] for i in inspect(eng).get_indexes("message_archive")}
    check("v002 建的索引存在", "ix_message_archive_msg_type" in idx, str(sorted(idx)))
    check("schema_version 到 2", runner.current_version(eng) == 2, str(runner.current_version(eng)))

    code, out = run_cli(args=["--check"])
    check("升级后 --check 通过（exit 0）", code == 0, f"exit={code}")

    code, out = run_cli(args=[])
    check("幂等：重复 upgrade 不重复应用", "无待应用迁移" in out, out.strip().splitlines()[-2:])
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(1) FROM schema_version")).scalar()
    check("schema_version 仍只有 2 行", rows == 2, f"rows={rows}")

    # A5 旧库（v1）增量
    url2, path2 = fresh_db("verify_b7_legacy.db")
    eng2 = create_engine(url2, future=True)
    from app.models import Base

    Base.metadata.create_all(eng2)
    with eng2.begin() as conn:
        conn.execute(text(runner.VERSION_TABLE_DDL))
        conn.execute(
            text("INSERT INTO schema_version (version, applied_at, note) VALUES (1, '2026-11-19T09:00:00+00:00', '初始建表（Batch6 固化）')")
        )
    old_at = None
    with eng2.connect() as conn:
        old_at = conn.execute(text("SELECT applied_at FROM schema_version WHERE version=1")).scalar()
    eng2.dispose()

    code, out = run_cli([], DATABASE_URL=url2)
    check("旧库 upgrade 成功", code == 0, f"exit={code}")
    check("旧库只应用增量 v002（不重跑 v001）", "→ 应用 v002" in out and "→ 应用 v001" not in out)
    eng2 = create_engine(url2, future=True)
    check("旧库版本升到 2", runner.current_version(eng2) == 2, str(runner.current_version(eng2)))
    with eng2.connect() as conn:
        keep = conn.execute(text("SELECT applied_at FROM schema_version WHERE version=1")).scalar()
        n = conn.execute(text("SELECT COUNT(1) FROM schema_version")).scalar()
    check("旧库 v001 的应用时间保持不变", keep == old_at, f"{old_at} / {keep}")
    check("旧库 schema_version 有 2 行", n == 2, f"rows={n}")

    # A6 --to / --status
    url3, path3 = fresh_db("verify_b7_to.db")
    code, out = run_cli(["--to", "1"], DATABASE_URL=url3)
    check("--to 1 只升到 v001", code == 0 and "→ 应用 v002" not in out, out.strip().splitlines()[-2:])
    eng3 = create_engine(url3, future=True)
    check("--to 1 后版本为 1", runner.current_version(eng3) == 1, str(runner.current_version(eng3)))
    code, out = run_cli(["--status"], DATABASE_URL=url3)
    check("--status 显示 v002 待应用", "待应用" in out and "v002" in out, out.strip().splitlines()[-3:])
    code, out = run_cli(["--check"], DATABASE_URL=url3)
    check("--to 1 后 --check 不通过（版本落后）", code == 1, f"exit={code}")
    code, out = run_cli([], DATABASE_URL=url3)
    check("继续 upgrade 补齐 v002", code == 0 and runner.current_version(eng3) == 2, out.strip().splitlines()[-2:])
    for e in (eng, eng2, eng3):
        e.dispose()
    for p in (path, path2, path3):
        if os.path.exists(p):
            os.remove(p)

    print("=" * 60)
    print("B. 看板自动刷新")
    print("=" * 60)

    html = open(os.path.join(ROOT, "app", "static", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()

    check("页面有自动刷新开关", 'id="autoRefresh"' in html)
    check("开关含关闭选项", 'value="0"' in html)
    check("页面有上次刷新时间位", 'id="lastRefresh"' in html)
    check("JS 有刷新间隔常量", "AUTO_KEY" in js and "autoRefresh" in js)
    check("JS 用 setTimeout 而非 setInterval（避免请求堆积）", "setTimeout(" in js and "setInterval(" not in js)
    check("页面隐藏时暂停刷新", "visibilitychange" in js and "document.hidden" in js)
    check("自动刷新走静默模式（不重复拉列表）", "silent" in js)
    check("自动刷新不覆盖用户正在编辑的报文", "loadDetail(state.trace, { silent: true })" in js)
    check("上次刷新时间会更新", "lastRefresh" in js and "toLocaleTimeString" in js)
    check("刷新间隔持久化到 localStorage", "localStorage.setItem" in js)
    external = any(
        token in html or token in js
        for token in ("http://", "https://", "cdn.", "unpkg", "jsdelivr", "googleapis")
    )
    check("看板零外部依赖（无 CDN/框架）", not external, "发现外部引用" if external else "")

    print("=" * 60)
    print("C. CI 门禁")
    print("=" * 60)

    ci_py = os.path.join(ROOT, "scripts", "ci.py")
    check("scripts/ci.py 存在", os.path.isfile(ci_py))
    wf = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    check(".github/workflows/ci.yml 存在", os.path.isfile(wf))
    if os.path.isfile(wf):
        wf_text = open(wf, encoding="utf-8").read()
        check("workflow 调 scripts/ci.py", "scripts/ci.py" in wf_text)
        check("workflow 指定 Python 3.11", "3.11" in wf_text)

    if os.path.isfile(ci_py):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        env["DATABASE_URL"] = "sqlite:///./data/verify_b7_ci.db"
        if os.path.exists(os.path.join(ROOT, "data", "verify_b7_ci.db")):
            os.remove(os.path.join(ROOT, "data", "verify_b7_ci.db"))
        proc = subprocess.run(
            [PY, ci_py, "--static-only"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        check("ci.py --static-only 可通过", proc.returncode == 0, f"exit={proc.returncode}")
        check("ci.py 输出结论行", "静态门禁通过" in out, out.strip().splitlines()[-1:] and out.strip().splitlines()[-1])

    print()
    if FAILS:
        print(f"结果：{len(FAILS)} 条失败")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
