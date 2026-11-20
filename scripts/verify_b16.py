"""Batch16 验收脚本：并发配额下发审计（append-only）。

Batch15 只留"当前期望值"，改过几次、从几改到几、谁改的、为什么都看不到。
Batch16 单独一张 append-only 审计表，每次 master:worker-limit 落一行。

覆盖：
A 迁移 v008：v7 库升级后建表成功、原有注册表数据不受影响；幂等
B 审计内容：三条下发 = 三行；prev_limit 链条正确（0→2→5→0）；reason 落库
C 铁律：审计只追加 —— 平台不提供删改入口；重复下发不覆盖历史
D CLI：scripts/quota_audit.py 能读出来（文本 / JSON / 过滤）
E 结构：路径仍 10 个；注册表只留最新值（与审计分开）
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

DB = "./data/verify_b16.db"
ARCHIVE = "./archive/_verify_b16"
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


def run_migrate(args, db_url, extra_env=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = db_url
    if extra_env:
        env.update(extra_env)
    return subprocess.run([PY, os.path.join(ROOT, "deploy", "migrate.py")] + args,
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def build_v7_db(path):
    """造一个 Batch15 时期的库：版本 7，注册表带 desired_limit，没有审计表。"""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY, applied_at DATETIME, note TEXT)")
    for v in range(1, 8):
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
        desired_limit INTEGER NOT NULL DEFAULT 0,
        note TEXT, meta TEXT,
        PRIMARY KEY (worker_id, instance_id))""")
    now = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""INSERT INTO worker_registry
        (worker_id, instance_id, status, last_seen, first_seen, concurrency,
         concurrency_limit, in_flight, handled, desired_limit, note)
        VALUES ('workbuddy','desk-1','running',?,?,2,4,1,7,3,'老数据')""", (now, now))
    conn.commit()
    conn.close()


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)

    print("=== A 迁移 v008：建审计表，不动注册表 ===")
    url = f"sqlite:///{DB}"
    build_v7_db(DB)
    proc = run_migrate([], url)
    check("v7 → v8 升级成功", proc.returncode == 0, proc.stdout.strip().splitlines()[-1:])
    check("应用了 v008", "应用 v008" in proc.stdout, proc.stdout.strip().splitlines()[-3:-1])
    conn = sqlite3.connect(DB)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    reg = conn.execute("SELECT worker_id, instance_id, desired_limit, handled, note FROM worker_registry").fetchall()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(worker_quota_audit)").fetchall()]
    conn.close()
    check("worker_quota_audit 表已建", "worker_quota_audit" in tables, str(sorted(tables)))
    check("注册表老数据原样（含 Batch15 的 desired_limit=3）",
          reg == [("workbuddy", "desk-1", 3, 7, "老数据")], str(reg))
    check("审计表列齐（含 prev_limit / reason）",
          {"prev_limit", "reason", "created_at", "desired_limit"} <= set(cols), str(cols))
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
    try:
        check("真起服务", wait_up(base))
        if "真起服务" in FAILS:
            print((server.stdout.read() if server.stdout else "")[-1500:])
            return 1

        print("=== B 审计内容：每次下发一行，prev 链条对 ===")
        post(base, "/api/workers", {"worker_id": "workbuddy", "instance_id": "desk-1",
                                   "status": "running"})
        post(base, "/api/workers", {"worker_id": "codex", "instance_id": "mac-1",
                                   "status": "running"})
        seq = [(2, "拉高夜间批处理"), (5, "临时加压"), (0, "撤销，回本地配置")]
        for limit, reason in seq:
            code, r = post(base, "/api/commands", {"command": "master:worker-limit",
                                                  "worker_id": "workbuddy",
                                                  "instance_id": "desk-1",
                                                  "limit": limit, "reason": reason})
            check(f"下发 {limit} 成功", code == 200, str(code))
        time.sleep(0.2)

        conn = sqlite3.connect(DB)
        rows = conn.execute("""SELECT desired_limit, prev_limit, reason FROM worker_quota_audit
                               WHERE worker_id='workbuddy' AND instance_id='desk-1'
                               ORDER BY id ASC""").fetchall()
        all_rows = conn.execute("SELECT COUNT(*) FROM worker_quota_audit").fetchone()[0]
        conn.close()
        check("三次下发 = 三行审计", len(rows) == 3, str(rows))
        check("prev 链条 3→2→5→0",
              [(r[1], r[0]) for r in rows] == [(3, 2), (2, 5), (5, 0)], str(rows))
        check("reason 落库", [r[2] for r in rows] == [s[1] for s in seq], str([r[2] for r in rows]))
        check("其他实例没有多出审计行", all_rows == 3, f"{all_rows} 行")

        print("=== C 只追加：再下发不覆盖历史 ===")
        post(base, "/api/commands", {"command": "master:worker-limit", "worker_id": "workbuddy",
                                    "instance_id": "desk-1", "limit": 2, "reason": "回滚测试"})
        conn = sqlite3.connect(DB)
        n = conn.execute("SELECT COUNT(*) FROM worker_quota_audit").fetchone()[0]
        first = conn.execute("SELECT desired_limit, prev_limit, reason FROM worker_quota_audit ORDER BY id ASC LIMIT 1").fetchone()
        conn.close()
        check("历史行没被改写（4 行）", n == 4, f"{n} 行")
        check("最早一行仍是原始值", first == (2, 3, "拉高夜间批处理"), str(first))
        code, spec = get(base, "/openapi.json")
        paths = sorted((spec or {})["paths"].keys())
        # 审计只读不写：平台侧不应出现任何删改审计的接口
        audit_paths = [p for p in paths if "audit" in p or "quota" in p]
        check("没为审计开新接口（路径仍 10 个）", len(paths) == 10 and not audit_paths,
              f"{len(paths)} 个 / 审计相关 {audit_paths}")

        print("=== D CLI 读审计 ===")
        cli = subprocess.run([PY, os.path.join(ROOT, "scripts", "quota_audit.py"), "--limit", "10"],
                             cwd=ROOT, env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        check("quota_audit.py 正常退出", cli.returncode == 0, (cli.stderr or "")[-160:])
        check("文本输出含目标与链条", "workbuddy/desk-1" in cli.stdout and "2→5" in cli.stdout,
              cli.stdout.strip().splitlines()[:3])
        cli_json = subprocess.run([PY, os.path.join(ROOT, "scripts", "quota_audit.py"), "--json",
                                  "--worker", "workbuddy"],
                                  cwd=ROOT, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        try:
            payload = json.loads(cli_json.stdout)
        except ValueError:
            payload = {}
        check("--json 可解析且条数正确", payload.get("total_shown") == 4, str(payload.get("total_shown")))
        check("JSON 只含指定执行方",
              all(i["worker_id"] == "workbuddy" for i in payload.get("items", [])),
              str({i["worker_id"] for i in payload.get("items", [])}))

        print("=== E 注册表与审计各管一段 ===")
        code, lst = get(base, "/api/workers?worker_id=workbuddy")
        cur = [w for w in (lst or []) if w["instance_id"] == "desk-1"]
        check("注册表只留最新值 2", cur and cur[0]["desired_limit"] == 2, str(cur[:1]))
        js = open(os.path.join(ROOT, "app", "api", "routes_commands.py"), encoding="utf-8").read()
        check("审计写入与注册表更新在同一事务", "db.add(WorkerQuotaAudit" in js and "db.commit()" in js)
        check("代码里没有删除审计表的语句", "delete(WorkerQuotaAudit" not in js and "DELETE FROM worker_quota_audit" not in js)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
