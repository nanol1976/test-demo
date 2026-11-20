"""Batch15 验收脚本：实例级并发配额下发（desired_limit）。

动机：弹性并发的上限（--concurrency）过去只能靠改命令行重启进程来收放。
Batch15 让平台把期望上限写进注册表，Worker 在下一次上报的响应里读到并当场生效，
在飞任务不被打断；看板可以直接给某个实例下发。

覆盖：
A 迁移 v007：v6 库加列，老行一行不丢、desired_limit 默认 0；幂等
B 指令契约：master:worker-limit 写入成功 / 缺参数 422 / 未注册实例 404 / 未知指令仍 400
C 配额合法性：0~64 之外 422；limit=0 表示撤销
D 真 Worker：上报→下发 3→下一轮上报 concurrency=3→下发 0 恢复本地配置；
   且下发比当前在飞任务数更小的上限时，在飞任务不被杀
E 看板与接口面：筛选控件、配额列在；路径仍 10 个
"""
import json
import os
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

DB = "./data/verify_b15.db"
ARCHIVE = "./archive/_verify_b15"
TOKEN = "verify-token"
FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def request(method, url, payload=None, timeout=15, raw=False):
    data = None
    headers = {"Accept": "application/json", "X-API-Token": TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (body if raw else (json.loads(body) if body else None))
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", "replace")
        if raw:
            return exc.code, raw_body
        try:
            return exc.code, json.loads(raw_body)
        except ValueError:
            return exc.code, {"raw": raw_body}


def get(base, path):
    return request("GET", base + path)


def post(base, path, payload):
    return request("POST", base + path, payload)


def port_free() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def build_v6_db(path):
    """造一个 Batch14 时期的库：版本 6，worker_registry 是 v6 形态（无 desired_limit）。"""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY, applied_at DATETIME, note TEXT)")
    for v in range(1, 7):
        conn.execute("INSERT INTO schema_version (version, applied_at, note) VALUES (?, datetime('now'), 'shim')", (v,))
    conn.execute("""CREATE TABLE worker_registry (
        worker_id VARCHAR(64) NOT NULL,
        instance_id VARCHAR(64) NOT NULL DEFAULT 'default',
        status VARCHAR(32) NOT NULL DEFAULT 'running',
        last_seen DATETIME NOT NULL,
        first_seen DATETIME NOT NULL,
        concurrency INTEGER NOT NULL DEFAULT 1,
        concurrency_limit INTEGER NOT NULL DEFAULT 1,
        in_flight INTEGER NOT NULL DEFAULT 0,
        handled INTEGER NOT NULL DEFAULT 0,
        note TEXT, meta TEXT,
        PRIMARY KEY (worker_id, instance_id))""")
    now = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""INSERT INTO worker_registry
        (worker_id, instance_id, status, last_seen, first_seen, concurrency,
         concurrency_limit, in_flight, handled, note)
        VALUES ('workbuddy','desk-1','running',?,?,2,4,1,7,'老数据')""", (now, now))
    conn.execute("""INSERT INTO worker_registry
        (worker_id, instance_id, status, last_seen, first_seen, concurrency,
         concurrency_limit, in_flight, handled, note)
        VALUES ('codex','mac-1','running',?,?,3,3,0,1,NULL)""", (now, now))
    conn.commit()
    conn.close()


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="b15_")

    print("=== A 迁移 v007：加列、老数据一行不丢 ===")
    url = f"sqlite:///{DB}"
    build_v6_db(DB)
    proc = run_migrate([], url)
    check("v6 → v7 升级成功", proc.returncode == 0, proc.stdout.strip().splitlines()[-1:])
    check("应用了 v007", "应用 v007" in proc.stdout, proc.stdout.strip().splitlines()[-3:-1])
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT worker_id, instance_id, concurrency, desired_limit, handled, note FROM worker_registry ORDER BY worker_id, instance_id"
    ).fetchall()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(worker_registry)").fetchall()]
    conn.close()
    check("desired_limit 列存在", "desired_limit" in cols, str(cols))
    check("两行老数据都在，desired_limit 默认 0",
          rows == [("codex", "mac-1", 3, 0, 1, None), ("workbuddy", "desk-1", 2, 0, 7, "老数据")], str(rows))
    check("重复升级幂等", "无待应用迁移" in run_migrate([], url).stdout)
    check("--check 通过", run_migrate(["--check"], url).returncode == 0)

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = url
    env["ARCHIVE_ROOT"] = ARCHIVE
    env["API_TOKEN"] = TOKEN
    env["SCAN_INTERVAL_SECONDS"] = "3600"

    port = port_free()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    workers = []
    try:
        check("真起服务", wait_up(base))
        if "真起服务" in FAILS:
            print((server.stdout.read() if server.stdout else "")[-1500:])
            return 1

        print("=== B 指令契约 ===")
        code, r = post(base, "/api/commands", {"command": "master:worker-limit",
                                              "worker_id": "workbuddy", "instance_id": "desk-1", "limit": 4})
        check("下发成功（200）", code == 200, f"{code} {r}")
        check("回显 desired_limit=4", (r or {}).get("desired_limit") == 4, str(r))
        code, lst = get(base, "/api/workers?worker_id=workbuddy")
        row = [w for w in (lst or []) if w["instance_id"] == "desk-1"]
        check("GET /api/workers 能看到 desired_limit", row and row[0].get("desired_limit") == 4,
              str(row[:1]))
        code, r = post(base, "/api/commands", {"command": "master:worker-limit",
                                              "worker_id": "nobody", "instance_id": "x", "limit": 2})
        check("未注册实例 → 404（不给凭空建行）", code == 404, f"{code} {r}")
        code, r = post(base, "/api/commands", {"command": "master:worker-limit", "limit": 2})
        check("缺 worker_id → 422", code == 422, str(code))
        code, r = post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy"})
        check("缺 limit → 422", code == 422, str(code))
        code, r = post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                              "instance_id": "desk-1", "limit": 65})
        check("limit 超上限 → 422", code == 422, str(code))
        code, r = post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                              "instance_id": "desk-1", "limit": -1})
        check("limit 负数 → 422", code == 422, str(code))
        code, r = post(base, "/api/commands", {"command": "master:乱写", "trace_id": "whatever"})
        check("未知指令仍 400", code == 400, str(code))
        code, r = post(base, "/api/commands", {"command": "master:pause"})
        check("流水线指令缺 trace_id → 422", code == 422, str(code))
        post(base, "/api/commands", {"command": "master:worker-limit",
                                    "worker_id": "workbuddy", "instance_id": "desk-1", "limit": 0})
        code, lst = get(base, "/api/workers?online=true")
        row = [w for w in (lst or []) if w["worker_id"] == "workbuddy" and w["instance_id"] == "desk-1"]
        check("limit=0 = 撤销下发", row and row[0].get("desired_limit") == 0, str(row[:1]))

        print("=== C 真 Worker：下发后动生效，撤销后回本地配置 ===")
        status_file = os.path.join(tmpdir, "status.json")
        worker = subprocess.Popen(
            [PY, os.path.join(ROOT, "worker", "agent_worker.py"), "--base-url", base,
             "--worker-id", "workbuddy", "--instance-id", "b15-live", "--token", TOKEN,
             "--allow-root", tmpdir, "--allow-root", ROOT, "--work-dir", tmpdir,
             "--registry-interval", "2", "--concurrency", "4", "--min-concurrency", "1",
             "--status-file", status_file, "--poll-interval", "1"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        workers.append(worker)

        def worker_row():
            _c, lst = get(base, "/api/workers?worker_id=workbuddy")
            hit = [w for w in (lst or []) if w["instance_id"] == "b15-live"]
            return hit[0] if hit else None

        deadline = time.time() + 20
        row0 = None
        while time.time() < deadline:
            row0 = worker_row()
            if row0:
                break
            time.sleep(0.5)
        check("真 Worker 已注册", bool(row0), str(row0))
        check("初始 concurrency = 本地 --concurrency 4", row0 and row0.get("concurrency") == 4, str(row0))

        code, r = post(base, "/api/commands", {"command": "master:worker-limit",
                                              "worker_id": "workbuddy", "instance_id": "b15-live", "limit": 2})
        check("下发 2 成功", code == 200, f"{code} {r}")
        applied = None
        deadline = time.time() + 15
        while time.time() < deadline:
            row = worker_row()
            if row and row.get("concurrency") == 2:
                applied = row
                break
            time.sleep(0.5)
        check("Worker 在下一轮上报里已按 2 跑", bool(applied), str(worker_row()))
        check("desired_limit 仍记录为 2", applied and applied.get("desired_limit") == 2, str(applied))

        code, r = post(base, "/api/commands", {"command": "master:worker-limit",
                                              "worker_id": "workbuddy", "instance_id": "b15-live", "limit": 0})
        recovered = None
        deadline = time.time() + 15
        while time.time() < deadline:
            row = worker_row()
            if row and row.get("concurrency") == 4:
                recovered = row
                break
            time.sleep(0.5)
        check("撤销后回到本地配置 4", bool(recovered), str(worker_row()))

        # 下发比在飞任务数更小的上限：不能杀在飞任务，已跑的要正常回执
        slow = os.path.join(tmpdir, "slow.py")
        with open(slow, "w", encoding="utf-8") as fh:
            fh.write("import time\ntime.sleep(6)\nprint('done')\n")
        dispatched = []
        for i in range(2):
            trace = f"t-b15-slow-{i}"
            code, body = post(base, "/api/messages", {
                "trace_id": trace, "task_id": f"{trace}-task", "msg_type": "task_dispatch",
                "sender": "master", "timestamp": datetime.now(timezone.utc).isoformat(),
                "version": "v1.0",
                "payload": {"target_worker": "workbuddy", "task_goal": "配额下调期间照常跑完",
                            "exec": {"script": slow, "timeout_seconds": 20}},
            })
            dispatched.append(code)
        check("派两个慢任务（202）", dispatched == [202, 202], str(dispatched))
        time.sleep(3)
        row = worker_row()
        check("两个任务确实在飞", row and (row.get("in_flight") or 0) >= 1, str(row))
        code, r = post(base, "/api/commands", {"command": "master:worker-limit",
                                              "worker_id": "workbuddy", "instance_id": "b15-live", "limit": 1})
        check("下调到 1 成功", code == 200, str(code))
        check("下调配额不会杀进程", worker.poll() is None, str(worker.poll()))
        time.sleep(8)
        done = []
        for i in range(2):
            _c, task = get(base, f"/api/tasks/t-b15-slow-{i}-task")
            done.append((task or {}).get("status"))
        check("在飞任务照常跑完（待验收），没被配额变化打断",
              all(st in ("待验收", "验收通过") for st in done), str(done))

        for w in workers:
            w.terminate()
        for w in workers:
            try:
                w.wait(timeout=20)
            except subprocess.TimeoutExpired:
                w.kill()
        workers.clear()

        print("=== D 看板与接口面 ===")
        code, spec = get(base, "/openapi.json")
        paths = sorted((spec or {})["paths"].keys())
        check("路径 11 个（配额没再加接口，11 = 10 + Batch18 审计）", len(paths) == 11, str(len(paths)))
        methods = {m for p in (spec or {})["paths"].values() for m in p}
        check("方法只有 GET/POST", methods <= {"get", "post"}, str(methods))
        code, html = request("GET", base + "/", raw=True)
        check("看板有执行方筛选控件", 'id="workerFilter"' in html and 'id="workerOnlineOnly"' in html)
        check("看板有实例名搜索", 'id="workerInstanceSearch"' in html)
        js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
        check("看板有「期望上限」列与下发按钮",
              '"期望上限"' in js and '"配额下发"' in js and "master:worker-limit" in js)
        check("筛选是客户端过滤（不打额外请求）", "function filteredWorkers" in js)
    finally:
        for w in workers:
            if w.poll() is None:
                w.kill()
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
