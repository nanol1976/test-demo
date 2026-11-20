"""Batch14 验收脚本：执行方多实例标识（worker_id + instance_id）。

Batch13 的注册表拿 worker_id 当唯一键：同一 worker_id 的两台机器互相覆盖 last_seen，
谁后报谁算在线，另一台被顶成静默。

覆盖：
A 迁移 v006：老库（v5 结构 + 已有行）升级后**一行不丢**，补 instance_id='default'
B 同一 worker_id 两个实例：各自一行、各自 last_seen、互不影响
C 过滤：?worker_id= 只看某个执行方的实例；?online=true 只看活的
D 真 Worker：默认 instance_id = 主机名；--instance-id 自定义；两台同 worker_id 并存
E 接口面：路径仍 10 个；看板有「实例」列
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

DB = "./data/verify_b14.db"
ARCHIVE = "./archive/_verify_b14"
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


def build_v5_db(path, limit_col="concurrency_limit"):
    """手工造一个 Batch13 时期的库：版本记到 5，worker_registry 是 v5 形态（无 instance_id）。"""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY, applied_at DATETIME, note TEXT)")
    for v in range(1, 6):
        conn.execute("INSERT INTO schema_version (version, applied_at, note) VALUES (?, datetime('now'), 'shim')", (v,))
    quoted = f'"{limit_col}"' if limit_col == "limit" else limit_col
    conn.execute(f"""CREATE TABLE worker_registry (
        worker_id VARCHAR(64) NOT NULL PRIMARY KEY,
        status VARCHAR(32) NOT NULL DEFAULT 'running',
        last_seen DATETIME NOT NULL,
        first_seen DATETIME NOT NULL,
        concurrency INTEGER NOT NULL DEFAULT 1,
        {quoted} INTEGER NOT NULL DEFAULT 1,
        in_flight INTEGER NOT NULL DEFAULT 0,
        handled INTEGER NOT NULL DEFAULT 0,
        note TEXT, meta TEXT)""")
    now = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(f"""INSERT INTO worker_registry
        (worker_id, status, last_seen, first_seen, concurrency, {quoted}, in_flight, handled, note)
        VALUES ('workbuddy', 'running', ?, ?, 2, {1 if limit_col != "limit" else 3}, 0,
                {5 if limit_col != "limit" else 8}, '老数据')""", (now, now))
    conn.commit()
    conn.close()


def sleep_script(tmpdir, seconds, name):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"import time\ntime.sleep({seconds})\n")
    return path


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="b14_")

    print("=== A 迁移 v006：老数据一行不丢 ===")
    url = f"sqlite:///{DB}"
    build_v5_db(DB)
    proc = run_migrate([], url)
    check("v5 → v6 升级成功", proc.returncode == 0, proc.stdout.strip().splitlines()[-1:])
    check("应用了 v006", "应用 v006" in proc.stdout, proc.stdout.strip().splitlines()[-3:-1])
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT worker_id, instance_id, handled, note FROM worker_registry").fetchall()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(worker_registry)").fetchall()]
    pk_cols = [r[1] for r in conn.execute("PRAGMA table_info(worker_registry)").fetchall() if r[5]]
    conn.close()
    check("老行还在，且补了 instance_id='default'", rows == [("workbuddy", "default", 5, "老数据")], str(rows))
    check("instance_id 列存在", "instance_id" in cols)
    check("主键是复合键 (worker_id, instance_id)", sorted(pk_cols) == ["instance_id", "worker_id"], str(pk_cols))
    check("重复升级幂等", "无待应用迁移" in run_migrate([], url).stdout)
    check("--check 通过", run_migrate(["--check"], url).returncode == 0)

    # 老库物理列名是 "limit"（SQL 关键字）的形态，也要能升过来
    legacy = "./data/verify_b14_legacy.db"
    if os.path.exists(legacy):
        os.remove(legacy)
    build_v5_db(legacy, limit_col="limit")
    proc3 = run_migrate([], f"sqlite:///{legacy}")
    conn = sqlite3.connect(legacy)
    rows3 = conn.execute("SELECT instance_id, handled, concurrency_limit FROM worker_registry").fetchall()
    conn.close()
    check("旧的 limit 列也能升级（改名为 concurrency_limit）", proc3.returncode == 0, proc3.stdout.strip().splitlines()[-1:])
    check("旧列里的值被带过来", rows3 == [("default", 8, 3)], str(rows3))
    os.remove(legacy)

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

        print("=== B 同一 worker_id 两个实例 ===")
        code, a = post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "host-a",
                                             "status": "running", "handled": 11})
        code, b = post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "host-b",
                                             "status": "running", "handled": 3})
        check("两个实例都 202", code == 202, str(code))
        check("各自独立记录", (a or {}).get("instance_id") == "host-a" and (b or {}).get("instance_id") == "host-b",
              f"{(a or {}).get('instance_id')} / {(b or {}).get('instance_id')}")
        conn = sqlite3.connect(DB)
        cnt = conn.execute("SELECT COUNT(*) FROM worker_registry WHERE worker_id='workbuddy'").fetchone()[0]
        conn.close()
        check("同 worker_id 三行（老 default + host-a + host-b）", cnt == 3, f"{cnt} 行")

        # host-b 再报一次，不应顶掉 host-a 的时间
        post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "host-b",
                                   "status": "running", "handled": 4})
        code, lst = get(base, "/api/workers?worker_id=workbuddy")
        by_inst = {(w["worker_id"], w["instance_id"]): w for w in (lst or [])}
        check("host-a 仍在列表里（没被顶掉）", ("workbuddy", "host-a") in by_inst, str(sorted(by_inst.keys())))
        check("host-b 的 handled 更新到 4", by_inst.get(("workbuddy", "host-b"), {}).get("handled") == 4,
              str(by_inst.get(("workbuddy", "host-b"), {}).get("handled")))
        check("host-a 的 handled 保持 11（互不干扰）", by_inst.get(("workbuddy", "host-a"), {}).get("handled") == 11,
              str(by_inst.get(("workbuddy", "host-a"), {}).get("handled")))

        print("=== C 过滤 ===")
        post(base, "/api/workers", {"worker_id": "codex", "instance_id": "mac", "status": "running"})
        code, only = get(base, "/api/workers?worker_id=codex")
        check("worker_id 过滤只回该执行方", only and all(w["worker_id"] == "codex" for w in only), str(only))
        post(base, "/api/workers", {"worker_id": "codex", "instance_id": "mac", "status": "stopped"})
        code, on = get(base, "/api/workers?online=true")
        check("online=true 不含已停机实例", all(w["online"] for w in (on or [])),
              str([(w["worker_id"], w["instance_id"]) for w in (on or [])]))
        bad_code, _ = post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "x" * 80})
        check("instance_id 超长 → 422", bad_code == 422, str(bad_code))

        print("=== D 真 Worker：默认实例名与自定义 ===")
        st1 = os.path.join(tmpdir, "w1.json")
        st2 = os.path.join(tmpdir, "w2.json")
        common = [PY, os.path.join(ROOT, "worker", "agent_worker.py"), "--base-url", base,
                  "--worker-id", "workbuddy", "--token", TOKEN,
                  "--allow-root", tmpdir, "--allow-root", ROOT, "--work-dir", tmpdir,
                  "--registry-interval", "3", "--concurrency", "1"]
        workers.append(subprocess.Popen(common + ["--status-file", st1], cwd=ROOT, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace"))
        workers.append(subprocess.Popen(common + ["--status-file", st2, "--instance-id", "extra-1"],
                                        cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace"))
        time.sleep(4)  # 两个 Worker 都常驻，只等第一轮注册上报
        code, lst2 = get(base, "/api/workers?worker_id=workbuddy")
        insts = {w["instance_id"] for w in (lst2 or [])}
        check("默认实例名 = 主机名", socket.gethostname()[:64] in insts, str(sorted(insts)))
        check("自定义实例名也在（同机两实例并存）", "extra-1" in insts, str(sorted(insts)))
        online = [w for w in (lst2 or []) if w["online"]]
        check("同机两个实例同时在线", len([w for w in online if w["instance_id"] in
                                        (socket.gethostname()[:64], "extra-1")]) == 2,
              str([(w["instance_id"], w["online"]) for w in online]))
        for w in workers:
            w.terminate()
        for w in workers:
            try:
                w.wait(timeout=20)
            except subprocess.TimeoutExpired:
                w.kill()
        workers.clear()

        print("=== E 接口面与看板 ===")
        code, spec = get(base, "/openapi.json")
        paths = sorted((spec or {})["paths"].keys())
        check("路径 11 个（多实例没再加接口，11 = 10 + Batch18 审计）", len(paths) == 11, str(len(paths)))
        methods = {m for p in (spec or {})["paths"].values() for m in p}
        check("方法只有 GET/POST", methods <= {"get", "post"}, str(methods))
        code, html = request("GET", base + "/", raw=True)
        check("看板有执行方面板", "workerTableWrap" in html)
        js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
        check("看板有「实例」列", '"执行方", "实例"' in js)
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
