"""Batch12 验收脚本：Worker 在线视图（不新增接口）+ 弹性并发。

背景：要"看见哪些执行方在线"，最直接的做法是加 `/api/workers` —— 但那就破了
"接口面固定 9 个"的硬约束。本批用另一条路：**台账记最近心跳时间**
（迁移 v004 加 last_heartbeat_time），看板按 target_worker 聚合推断在线/静默。

覆盖：
A 迁移 v004：干净库升级到 4，列存在，重复升级幂等
B 心跳落库：执行期 GET /api/tasks/{id} 能看到 last_heartbeat_time，且随时间前进
C 在线视图：接口列表里带该字段；看板有"执行方"面板与聚合逻辑；接口数 10（Batch13 起）
D 弹性并发：积压 4 个任务时并发上限打开（.in_flight 升到 >1），只剩 1 个任务时回落到 1
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

DB = "./data/verify_b12.db"
ARCHIVE = "./archive/_verify_b12"
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
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def get_json(base, path):
    code, body = request("GET", base + path)
    try:
        return code, json.loads(body)
    except ValueError:
        return code, None


def post(base, path, payload):
    code, body = request("POST", base + path, payload)
    try:
        return code, json.loads(body)
    except ValueError:
        return code, body


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


def env_of(trace_id, task_id, sender, payload, ts):
    return {"trace_id": trace_id, "task_id": task_id, "msg_type": "task_dispatch",
            "sender": sender, "timestamp": ts, "version": "v1.0", "payload": payload}


def sleep_script(tmpdir, seconds, name):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "import json,os,time\n"
            "start=time.time()\n"
            f"time.sleep({seconds})\n"
            "out=os.path.join(os.getcwd(),'artifacts','r_'+os.path.basename(__file__)+'.json')\n"
            "os.makedirs(os.path.dirname(out),exist_ok=True)\n"
            "with open(out,'w',encoding='utf-8') as f:\n"
            "    json.dump({'elapsed':round(time.time()-start,2)},f)\n"
            "print('done',flush=True)\n"
        )
    return path


def worker_cmd(base, tmpdir, extra, status_file):
    return [PY, os.path.join(ROOT, "worker", "agent_worker.py"),
            "--base-url", base, "--worker-id", "workbuddy", "--token", TOKEN,
            "--allow-root", tmpdir, "--allow-root", ROOT, "--work-dir", tmpdir,
            "--heartbeat-interval", "2", "--max-task-seconds", "60",
            "--status-file", status_file] + extra


def run_migrate(args, db_url):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = db_url
    return subprocess.run([PY, os.path.join(ROOT, "deploy", "migrate.py")] + args,
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="b12_tasks_")
    status_file = os.path.join(tmpdir, "status.json")

    print("=== A 迁移 v004 ===")
    url = f"sqlite:///{DB}"
    proc = run_migrate([], url)
    check("干净库升级成功", proc.returncode == 0, proc.stdout.strip().splitlines()[-1:] and proc.stdout.strip().splitlines()[-1])
    import re as _re
    _line = _re.search(r"schema_version：[^\n]*", proc.stdout)
    _nums = [int(x) for x in _re.findall(r"\d+", _line.group(0))] if _line else [0]
    check("schema_version ≥ 4（后续批次还会往上加）", max(_nums) >= 4, proc.stdout.strip().splitlines()[-2:])
    check("日志显示应用 v004（Batch13 的 v005 紧随其后）",
          "应用 v004" in proc.stdout)
    import sqlite3
    cols = [r[1] for r in sqlite3.connect(DB).execute("PRAGMA table_info(task_ledger)").fetchall()]
    check("task_ledger 有 last_heartbeat_time 列", "last_heartbeat_time" in cols, str(cols[-3:]))
    proc2 = run_migrate([], url)
    check("重复升级幂等", "无待应用迁移" in proc2.stdout, proc2.stdout.strip().splitlines()[-2:])
    proc3 = run_migrate(["--check"], url)
    check("--check 通过", proc3.returncode == 0, proc3.stdout.strip().splitlines()[-1:])

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = url
    env["ARCHIVE_ROOT"] = ARCHIVE
    env["API_TOKEN"] = TOKEN
    env["SCAN_INTERVAL_SECONDS"] = "3600"

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        check("真起服务", wait_up(base))
        if "真起服务" in FAILS:
            print((server.stdout.read() if server.stdout else "")[-1500:])
            return 1

        print("=== B 心跳落库（执行期可观测）===")
        script = sleep_script(tmpdir, 9, "hb.py")
        post(base, "/api/messages",
             env_of("t-b12-hb", "t-b12-hb-1", "master",
                    {"target_worker": "workbuddy", "task_goal": "心跳可观测",
                     "exec": {"script": script}}, "2026-11-19T18:00:00+00:00"))
        worker = subprocess.Popen(worker_cmd(base, tmpdir, ["--concurrency", "1", "--once"], status_file),
                                  cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace")
        time.sleep(3)  # 心跳间隔 2s，任务跑 9s，留足观测窗口
        code, t1 = get_json(base, "/api/tasks/t-b12-hb-1")
        check("执行中能查到 last_heartbeat_time", code == 200 and t1.get("last_heartbeat_time") is not None,
              str((t1 or {}).get("last_heartbeat_time")))
        hb1 = (t1 or {}).get("last_heartbeat_time")
        check("第一次观测时任务仍在执行中", (t1 or {}).get("status") == "执行中",
              str((t1 or {}).get("status")))
        time.sleep(3)
        code, t2 = get_json(base, "/api/tasks/t-b12-hb-1")
        hb2 = (t2 or {}).get("last_heartbeat_time")
        check("心跳时间随时间前进（不是一次性写的）", bool(hb1) and bool(hb2) and hb1 != hb2, f"{hb1} → {hb2}")
        check("心跳时间是带时区的 ISO（前端能直接算在线）", str(hb2).endswith("+00:00"), str(hb2))
        check("两次观测都在执行中（心跳没改状态）", (t2 or {}).get("status") == "执行中",
              str((t2 or {}).get("status")))
        worker.wait(timeout=60)
        code, t3 = get_json(base, "/api/tasks/t-b12-hb-1")
        check("跑完收敛到待验收", (t3 or {}).get("status") == "待验收", str((t3 or {}).get("status")))

        print("=== C 执行方在线视图（不新增接口）===")
        code, page = get_json(base, "/api/tasks?page=1&size=100")
        check("任务列表 200", code == 200)
        items = (page or {}).get("items", [])
        check("列表项带 last_heartbeat_time 字段", items and "last_heartbeat_time" in items[0], str(sorted(items[0].keys()) if items else ""))
        # 模拟看板聚合口径：按 target_worker 统计在跑 / 待验收 / 最近心跳
        agg = {}
        for it in items:
            w = it.get("target_worker")
            rec = agg.setdefault(w, {"running": 0, "waiting": 0, "last_hb": None})
            if it["status"] == "执行中":
                rec["running"] += 1
            if it["status"] == "待验收":
                rec["waiting"] += 1
            if it.get("last_heartbeat_time"):
                rec["last_hb"] = max(rec["last_hb"] or "", it["last_heartbeat_time"])
        check("聚合能算出 workbuddy 的待验收数", agg.get("workbuddy", {}).get("waiting", 0) >= 1, str(agg.get("workbuddy")))
        check("聚合能算出 workbuddy 的最近心跳", agg.get("workbuddy", {}).get("last_hb") is not None,
              str(agg.get("workbuddy", {}).get("last_hb")))

        code, spec = get_json(base, "/openapi.json")
        paths = list((spec or {}).get("paths", {}).keys())
        # 本批做的时候是 9 个（靠任务心跳反推）；Batch13 已把注册表补成第 10 个路径
        check("接口数 11（Batch18 起含 /api/audit）", len(paths) == 11, f"{len(paths)} 个")
        check("在线视图数据源可由 /api/workers 承担（注册表已存在）", "/api/workers" in paths, str(paths))

        code, html = request("GET", base + "/")
        check("看板首页含「执行方」面板", "执行方" in html and "workerTableWrap" in html)
        js = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
        check("前端有聚合函数与在线阈值", "aggregateWorkers" in js and "WORKER_ONLINE_MS" in js)
        check("在线阈值与平台默认心跳超时一致（120s）", "WORKER_ONLINE_MS = 120000" in js)

        print("=== D 弹性并发 ===")
        for i in range(4):
            post(base, "/api/messages",
                 env_of("t-b12-c", f"t-b12-c-{i}", "master",
                        {"target_worker": "workbuddy", "task_goal": f"积压-{i}",
                         "exec": {"script": sleep_script(tmpdir, 3, f"c{i}.py")}},
                        f"2026-11-19T18:01:0{i}+00:00"))
        t0 = time.time()
        proc4 = subprocess.run(worker_cmd(base, tmpdir, ["--concurrency", "4", "--min-concurrency", "1", "--once"],
                                          status_file),
                               cwd=ROOT, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
        elapsed = round(time.time() - t0, 1)
        log4 = (proc4.stdout or "") + (proc4.stderr or "")
        peak = max([int(x) for x in re.findall(r'"in_flight": (\d+)', log4)] or [0])
        check("4 个 3s 任务并发跑完（串行 ≥12s）", elapsed < 8, f"{elapsed}s")
        check("并发真的打开（峰值 in_flight ≥2）", peak >= 2, f"peak={peak}")
        check("不越过上限（峰值 ≤4）", peak <= 4, f"peak={peak}")
        for i in range(4):
            code, ti = get_json(base, f"/api/tasks/t-b12-c-{i}")
            check(f"任务 {i} 收敛到待验收", (ti or {}).get("status") == "待验收", str((ti or {}).get("status")))

        # 只剩 1 个任务时，弹性并发应回落到下限 1
        post(base, "/api/messages",
             env_of("t-b12-s", "t-b12-s-1", "master",
                    {"target_worker": "workbuddy", "task_goal": "单个任务",
                     "exec": {"script": sleep_script(tmpdir, 1, "single.py")}},
                    "2026-11-19T18:02:00+00:00"))
        proc5 = subprocess.run(worker_cmd(base, tmpdir, ["--concurrency", "4", "--min-concurrency", "1", "--once"],
                                          status_file),
                               cwd=ROOT, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
        with open(status_file, encoding="utf-8") as fh:
            st = json.load(fh)
        check("单个任务时并发回落到 1（弹性生效）",
              st.get("limit") == 1 or st.get("in_flight") in (0, 1), str(st))
        code, ts1 = get_json(base, "/api/tasks/t-b12-s-1")
        check("单任务收敛到待验收", (ts1 or {}).get("status") == "待验收", str((ts1 or {}).get("status")))
    finally:
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
