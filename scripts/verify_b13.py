"""Batch13 验收脚本：执行方注册表（精确在线列表）。

Batch12 的在线视图靠任务心跳反推，盲区是"空转但活着的 Worker 看不见"。
本批补注册表：Worker 忙闲都上报，平台自己维护 online 状态。

覆盖：
A 迁移 v005：worker_registry 建表、幂等、--check 通过
B POST /api/workers：注册 upsert、幂等（同一 worker 不新增行）、非法 worker_id/status → 409/422
C GET /api/workers：online 判定（running + 超时内）、stopping/stopped 不算在线、online_only 过滤
D 真 Worker：空转也注册（没任务时能查到）、跑完后 handled 增长、SIGTERM 后报 stopping/stopped
E 接口面：路径 10 个、方法只有 GET/POST、看板优先读注册表
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

DB = "./data/verify_b13.db"
ARCHIVE = "./archive/_verify_b13"
TOKEN = "verify-token"
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(method, url, payload=None, timeout=15):
    data = None
    headers = {"Accept": "application/json", "X-API-Token": TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


def get_text(base, path):
    code, _ = 0, None
    req = urllib.request.Request(base + path, headers={"Accept": "text/html", "X-API-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def get(base, path):
    return request("GET", base + path)


def post(base, path, payload):
    return request("POST", base + path, payload)


def wait_up(base, timeout=30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if request("GET", base + "/api/health", timeout=2)[0] == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


def run_migrate(args, db_url):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = db_url
    return subprocess.run([PY, os.path.join(ROOT, "deploy", "migrate.py")] + args,
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def sleep_script(tmpdir, seconds, name):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "import json,os,time\n"
            "start=time.time()\n"
            f"time.sleep({seconds})\n"
            "print('done',flush=True)\n"
        )
    return path


def main() -> int:
    for path in (DB,):
        if os.path.exists(path):
            os.remove(path)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="b13_")

    print("=== A 迁移 v005 ===")
    url = f"sqlite:///{DB}"
    proc = run_migrate([], url)
    check("干净库升级成功", proc.returncode == 0, proc.stdout.strip().splitlines()[-1:])
    check("schema_version 到 5", "schema_version：0 → 5" in proc.stdout or "schema_version：5" in proc.stdout,
          proc.stdout.strip().splitlines()[-2:])
    check("应用了 v005", "应用 v005" in proc.stdout)
    tables = {r[0] for r in sqlite3.connect(DB).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("worker_registry 表存在", "worker_registry" in tables, str(sorted(tables)))
    proc2 = run_migrate([], url)
    check("重复升级幂等", "无待应用迁移" in proc2.stdout, proc2.stdout.strip().splitlines()[-2:])
    check("--check 通过", run_migrate(["--check"], url).returncode == 0)

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = url
    env["ARCHIVE_ROOT"] = ARCHIVE
    env["API_TOKEN"] = TOKEN
    env["SCAN_INTERVAL_SECONDS"] = "3600"

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    worker = None
    try:
        check("真起服务", wait_up(base))
        if "真起服务" in FAILS:
            print((server.stdout.read() if server.stdout else "")[-1500:])
            return 1

        print("=== B POST /api/workers ===")
        code, w1 = post(base, "/api/workers", {"worker_id": "workbuddy", "status": "running",
                                              "concurrency": 4, "limit": 2, "in_flight": 2,
                                              "handled": 7, "meta": {"pid": 123}})
        check("注册 202", code == 202, str(code))
        check("返回在线=true", (w1 or {}).get("online") is True, str(w1))
        check("字段原样回显", (w1 or {}).get("limit") == 2 and (w1 or {}).get("handled") == 7, str(w1))
        check("meta 解析成对象", (w1 or {}).get("meta") == {"pid": 123}, str((w1 or {}).get("meta")))
        code, w2 = post(base, "/api/workers", {"worker_id": "workbuddy", "status": "running",
                                              "concurrency": 4, "limit": 4, "in_flight": 0,
                                              "handled": 9})
        check("重复上报 202（upsert）", code == 202 and (w2 or {}).get("handled") == 9, str(w2))
        rows = sqlite3.connect(DB).execute("SELECT COUNT(*) FROM worker_registry").fetchone()[0]
        check("同一 worker 只一行（不是每次新增）", rows == 1, f"{rows} 行")
        check("first_seen 保持不动（能看出从什么时候开始在线）",
              (w1 or {}).get("first_seen") == (w2 or {}).get("first_seen"),
              f"{(w1 or {}).get('first_seen')} vs {(w2 or {}).get('first_seen')}")
        code, _ = post(base, "/api/workers", {"worker_id": "hacker", "status": "running"})
        check("非法 worker_id → 409", code == 409, str(code))
        code, _ = post(base, "/api/workers", {"worker_id": "codex", "status": "explode"})
        check("非法 status → 422", code == 422, str(code))
        code, _ = post(base, "/api/workers", {"status": "running"})
        check("缺 worker_id → 422", code == 422, str(code))

        print("=== C GET /api/workers ===")
        code, lst = get(base, "/api/workers")
        check("列表 200", code == 200, str(code))
        check("至少一条（workbuddy）", any(w["worker_id"] == "workbuddy" for w in (lst or [])),
              str([w["worker_id"] for w in (lst or [])]))
        check("字段含 online / age_seconds",
              all(k in (lst or [{}])[0] for k in ("online", "age_seconds", "last_seen")),
              str(sorted((lst or [{}])[0].keys())))
        code, on = get(base, "/api/workers?online=true")
        check("online_only 过滤可用", all(w["online"] for w in (on or [])), str(on))

        # 把它改成停机 + 把 last_seen 推到 10 分钟前：应判"不在线"
        post(base, "/api/workers", {"worker_id": "codex", "status": "stopped"})
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE worker_registry SET last_seen=? WHERE worker_id='workbuddy'", (old,))
        conn.commit()
        conn.close()
        code, lst2 = get(base, "/api/workers")
        by_id = {w["worker_id"]: w for w in (lst2 or [])}
        check("超时未上报 → online=false", by_id.get("workbuddy", {}).get("online") is False,
              str(by_id.get("workbuddy", {}).get("age_seconds")))
        check("状态 stopping/stopped 一律不算在线", by_id.get("codex", {}).get("online") is False,
              str(by_id.get("codex", {}).get("status")))

        print("=== D 真 Worker：空转也注册 + 跑完报数 + 退出报停机 ===")
        st_file = os.path.join(tmpdir, "status.json")
        cmd = [PY, os.path.join(ROOT, "worker", "agent_worker.py"),
               "--base-url", base, "--worker-id", "workbuddy", "--token", TOKEN,
               "--allow-root", tmpdir, "--allow-root", ROOT, "--work-dir", tmpdir,
               "--heartbeat-interval", "2", "--registry-interval", "3", "--status-file", st_file,
               "--concurrency", "2", "--min-concurrency", "1"]
        worker = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace")
        time.sleep(5)  # 空转窗口：没有任何任务
        code, idle_list = get(base, "/api/workers")
        idle_row = {w["worker_id"]: w for w in (idle_list or [])}.get("workbuddy", {})
        check("空转（没有任务）也能在注册表里看到", bool(idle_row), str(idle_row))
        check("空转时 online=true", idle_row.get("online") is True, str(idle_row.get("online")))
        check("空转时 in_flight=0", idle_row.get("in_flight") == 0, str(idle_row.get("in_flight")))
        check("上报了并发上限", idle_row.get("concurrency") == 2, str(idle_row.get("concurrency")))

        # 派一个任务让它真干活
        post(base, "/api/messages", {
            "trace_id": "t-b13", "task_id": "t-b13-1", "msg_type": "task_dispatch",
            "sender": "master", "timestamp": datetime.now(timezone.utc).isoformat(), "version": "v1.0",
            "payload": {"target_worker": "workbuddy", "task_goal": "注册表验证",
                        "exec": {"script": sleep_script(tmpdir, 4, "one.py")}},
        })
        time.sleep(7)
        code, done_row = get(base, "/api/workers")
        row = {w["worker_id"]: w for w in (done_row or [])}.get("workbuddy", {})
        check("干完活后 handled ≥1", row.get("handled", 0) >= 1, str(row.get("handled")))
        code, task = get(base, "/api/tasks/t-b13-1")
        check("任务收敛到待验收", (task or {}).get("status") == "待验收", str((task or {}).get("status")))

        worker.terminate()
        worker.wait(timeout=30)
        time.sleep(2)
        code, final = get(base, "/api/workers")
        fin = {w["worker_id"]: w for w in (final or [])}.get("workbuddy", {})
        check("收到 SIGTERM 后状态变成 stopping/stopped", fin.get("status") in ("stopping", "stopped"),
              str(fin.get("status")))
        check("退出后 online=false", fin.get("online") is False, str(fin.get("online")))

        print("=== E 接口面与看板 ===")
        code, spec = get(base, "/openapi.json")
        paths = sorted((spec or {})["paths"].keys())
        check("路径 10 个（原 9 + /api/workers）", len(paths) == 10, str(paths))
        methods = {m for p in (spec or {})["paths"].values() for m in p}
        check("方法只有 GET/POST", methods <= {"get", "post"}, str(methods))
        code, html = get_text(base, "/")
        check("看板首页仍含执行方面板", "执行方" in html and "workerTableWrap" in html)
        js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
        check("前端优先读 /api/workers", 'workers: "/api/workers"' in js)
        check("前端保留任务聚合兜底（老平台不报错）", "aggregateWorkers" in js)
    finally:
        if worker and worker.poll() is None:
            worker.kill()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
