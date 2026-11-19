"""Batch8 验收脚本：查询增强（关键字/排序）+ 差量刷新 + 迁移接管(--baseline)。

A 流水线查询（q/sort/分页/组合）
B 任务查询（q/sort）
C 前端（搜索框、排序、差量渲染）
D 迁移 --baseline 接管
"""
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["DATABASE_URL"] = "sqlite:///./data/verify_b8.db"
os.environ["ARCHIVE_ROOT"] = "./archive/_verify_b8"
os.environ["API_TOKEN"] = "verify-token"
os.environ["BUS_CAPACITY"] = "200"
os.environ["BUS_WORKERS"] = "2"
os.environ["PYTHONPATH"] = ROOT

if os.path.exists("./data/verify_b8.db"):
    os.remove("./data/verify_b8.db")
if os.path.isdir("./archive/_verify_b8"):
    shutil.rmtree("./archive/_verify_b8")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import get_engine, get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.repositories.ledger import LedgerRepository  # noqa: E402

Base.metadata.create_all(get_engine())

H = {"X-API-Token": os.environ["API_TOKEN"]}
PY = sys.executable
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def seed():
    """三条流水线，起始时间刻意错开，用来验排序。"""
    db = get_session_factory()()
    try:
        repo = LedgerRepository(db)
        t0 = datetime.now(timezone.utc).replace(microsecond=0)
        rows = [
            ("alpha-trace", "报表汇总任务", t0 - timedelta(hours=2)),
            ("beta-trace", "数据清洗流程", t0 - timedelta(hours=1)),
            ("gamma-trace", "报表导出收尾", t0),
        ]
        for tid, demand, started in rows:
            p = repo.create_pipeline(tid, demand)
            p.start_time = started
            db.commit()
        # alpha 名下三个任务，id 有序，便于验证倒序
        for suffix in ("a01", "a02", "a03"):
            repo.create_task("alpha-trace", f"alpha-{suffix}", "dsh", status="待拆解")
        for suffix in ("b01", "b02"):
            repo.create_task("beta-trace", f"beta-{suffix}", "workbuddy", status="待拆解")
    finally:
        db.close()


def migrate_cli(args, db_url):
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url
    env["PYTHONPATH"] = ROOT
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


def main():
    seed()
    client = TestClient(app)

    print("=" * 60)
    print("A. 流水线查询：关键字 + 排序")
    print("=" * 60)

    r = client.get("/api/pipelines", params={"q": "alpha"}, headers=H)
    body = r.json()
    check("q 命中 trace_id 子串", r.status_code == 200 and body["meta"]["total"] == 1, str(body["meta"]))
    check("q 命中时返回的是那一条", body["items"][0]["trace_id"] == "alpha-trace")

    r = client.get("/api/pipelines", params={"q": "报表"}, headers=H)
    body = r.json()
    check("q 命中原始需求子串（2 条）", body["meta"]["total"] == 2, str(body["meta"]))

    r = client.get("/api/pipelines", params={"q": "根本不存在"}, headers=H)
    body = r.json()
    check("q 无命中 → 0 条且 total=0", body["meta"]["total"] == 0 and body["items"] == [], str(body["meta"]))

    r = client.get("/api/pipelines", params={"sort": "start_time_asc"}, headers=H)
    ids_asc = [x["trace_id"] for x in r.json()["items"]]
    check("sort=asc 最早在前", ids_asc[0] == "alpha-trace" and ids_asc[-1] == "gamma-trace", str(ids_asc))

    r = client.get("/api/pipelines", params={"sort": "start_time_desc"}, headers=H)
    ids_desc = [x["trace_id"] for x in r.json()["items"]]
    check("sort=desc 最新在前", ids_desc[0] == "gamma-trace" and ids_desc[-1] == "alpha-trace", str(ids_desc))

    r = client.get("/api/pipelines", headers=H)
    check("默认排序为最新在前", [x["trace_id"] for x in r.json()["items"]][0] == "gamma-trace")

    r = client.get("/api/pipelines", params={"sort": "id; DROP TABLE x"}, headers=H)
    check("非法 sort → 422（不落 SQL）", r.status_code == 422, f"{r.status_code}")

    r = client.get("/api/pipelines", params={"q": "x" * 200}, headers=H)
    check("q 超长 → 422", r.status_code == 422, f"{r.status_code}")

    r = client.get("/api/pipelines", params={"q": "报表", "size": 1, "page": 2}, headers=H)
    body = r.json()
    check("q + 分页：total 为过滤后总数", body["meta"]["total"] == 2 and body["meta"]["pages"] == 2, str(body["meta"]))
    check("q + 分页第 2 页剩 1 条", len(body["items"]) == 1, str([x["trace_id"] for x in body["items"]]))

    r = client.get("/api/pipelines", params={"q": "报表", "status": "pending"}, headers=H)
    check("q + 状态组合过滤", r.json()["meta"]["total"] == 2, str(r.json()["meta"]))

    r = client.get("/api/pipelines", params={"size": 100}, headers=H)
    body = r.json()
    check("count 与 list 一致（total == 返回条数）", body["meta"]["total"] == len(body["items"]), f"{body['meta']['total']} / {len(body['items'])}")

    print("=" * 60)
    print("B. 任务查询：关键字 + 排序")
    print("=" * 60)

    r = client.get("/api/tasks", params={"q": "alpha-a"}, headers=H)
    body = r.json()
    check("任务 q 命中 task_id 子串（3 条）", body["meta"]["total"] == 3, str(body["meta"]))

    r = client.get("/api/tasks", params={"q": "alpha-a", "sort": "task_id_desc"}, headers=H)
    ids = [x["task_id"] for x in r.json()["items"]]
    check("任务 sort=desc 倒序", ids[0] == "alpha-a03" and ids[-1] == "alpha-a01", str(ids))

    r = client.get("/api/tasks", params={"trace_id": "beta-trace"}, headers=H)
    check("任务按 trace 过滤仍可用（2 条）", r.json()["meta"]["total"] == 2, str(r.json()["meta"]))

    r = client.get("/api/tasks", params={"sort": "task_id_asc; --"}, headers=H)
    check("任务非法 sort → 422", r.status_code == 422, f"{r.status_code}")

    r = client.get("/api/export/alpha-trace", params={"format": "csv"}, headers=H)
    check("导出回归：CSV 200 且含表头", r.status_code == 200 and "task_id" in r.text, f"{r.status_code}")

    print("=" * 60)
    print("C. 前端：搜索 / 排序 / 差量渲染")
    print("=" * 60)

    html = open(os.path.join(ROOT, "app", "static", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()

    check("页面有搜索框", 'id="pipeSearch"' in html)
    check("页面有排序下拉", 'id="pipeSort"' in html)
    check("排序含最早在前选项", 'value="start_time_asc"' in html)
    check("查询会带上 q 与 sort 参数", 'query.set("q", state.q)' in js and 'query.set("sort", state.sort)' in js)
    check("搜索有防抖（setTimeout 400ms）", "searchTimer" in js and "}, 400)" in js)
    check("差量渲染：列表行复用", "listRows" in js and "buildPipelineRow" in js and "updatePipelineRow" in js)
    check("差量渲染：任务行复用", "taskRows" in js and "buildTaskRow" in js and "updateTaskRow" in js)
    check("差量渲染只改变化单元格（列表）", "rec.last.status !==" in js and "rec.last.demand !==" in js)
    check("差量渲染只改变化单元格（任务）", "rec.last.worker !==" in js and "rec.last.reason !==" in js)
    check("表格只在首次创建（复用 querySelector 的 table）", 'wrap.querySelector("table")' in js and "if (!table) {" in js)

    print("=" * 60)
    print("D. 迁移 --baseline 接管")
    print("=" * 60)

    base_path = os.path.join(ROOT, "data", "verify_b8_base.db")
    if os.path.exists(base_path):
        os.remove(base_path)
    url = "sqlite:///./data/verify_b8_base.db"
    eng = __import__("sqlalchemy").create_engine(url, future=True)
    Base.metadata.create_all(eng)  # 模拟"表已建好、但没版本记录"的既有库
    eng.dispose()

    from migrations import runner as _runner
    LATEST = _runner.latest_available()

    code, out = migrate_cli(["--baseline", str(LATEST)], url)
    check("baseline 接管成功", code == 0 and "接管" in out, f"exit={code}")
    code, out = migrate_cli(["--check"], url)
    check("接管后 --check 通过", code == 0, f"exit={code}")

    code, out = migrate_cli(["--baseline", str(LATEST)], url)
    check("已有版本再接管被拒绝", code == 1 and "拒绝接管" in out, f"exit={code}")

    empty_path = os.path.join(ROOT, "data", "verify_b8_empty.db")
    if os.path.exists(empty_path):
        os.remove(empty_path)
    code, out = migrate_cli(["--baseline", "1"], "sqlite:///./data/verify_b8_empty.db")
    check("缺表时接管被拒绝", code == 1 and "缺表" in out, f"exit={code}")

    code, out = migrate_cli(["--baseline", "9"], url)
    check("超范围版本被拒绝", code == 1, f"exit={code}")

    # 接管后，后续新版本仍能继续追加（用 runner 直接验证 pending 集合）
    from migrations import runner

    eng = __import__("sqlalchemy").create_engine(url, future=True)
    check("接管后无待应用迁移", runner.pending(eng) == [], str([m.version for m in runner.pending(eng)]))
    check("接管记录写进 schema_version", runner.current_version(eng) == LATEST,
          str(runner.current_version(eng)))
    eng.dispose()

    print("=" * 60)
    print("E. CI 静态门禁回归")
    print("=" * 60)

    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///./data/verify_b8_ci.db"
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run(
        [PY, os.path.join(ROOT, "scripts", "ci.py"), "--static-only"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    check("ci.py --static-only 仍通过", proc.returncode == 0, f"exit={proc.returncode}")
    check("CI 含 JS 语法门禁（有 node 时）", "JS 语法" in out, ";".join([ln.strip() for ln in out.splitlines() if "JS" in ln][:1]))

    # 清理：Windows 上 SQLite 文件可能仍被连接占着，失败不影响验收结论
    def try_remove(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    try:
        get_engine().dispose()
    except Exception:
        pass
    for p in (base_path, empty_path, os.path.join(ROOT, "data", "verify_b8_ci.db")):
        try_remove(p)
    try_remove("./data/verify_b8.db")
    if os.path.isdir("./archive/_verify_b8"):
        shutil.rmtree("./archive/_verify_b8", ignore_errors=True)

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
