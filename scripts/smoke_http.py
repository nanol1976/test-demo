"""真实起服务冒烟：起一个 uvicorn 子进程，用 HTTP 打关键路径，再关掉。

为什么单独做：TestClient 会掩盖"启动路径"的缺陷（Batch5 的空库 500 就是这种）。
只依赖标准库 urllib，不引额外包。

用法：python scripts/smoke_http.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
DB = "./data/smoke.db"
ARCHIVE = "./archive/_smoke"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, timeout=5):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post(url, payload, timeout=10):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def wait_up(base, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = get(base + "/api/health", timeout=2)
            if code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


FAILS = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main() -> int:
    for p in (DB,):
        if os.path.exists(p):
            os.remove(p)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = f"sqlite:///{DB}"
    env["ARCHIVE_ROOT"] = ARCHIVE
    env["API_TOKEN"] = ""
    env["SCAN_INTERVAL_SECONDS"] = "3600"

    # 注意：不先建表 —— 就是要验「空库启动自检」能不能兜住
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        up = wait_up(base)
        check("服务在 25s 内起来（空库）", up)
        if not up:
            out = proc.stdout.read() if proc.stdout else ""
            print("  服务日志尾部：\n" + out[-2000:])
            return 1

        print("  --- 关键路径 ---")
        code, _ = get(base + "/api/health")
        check("/api/health 200", code == 200, f"status={code}")

        code, body = get(base + "/")
        check("/ 首页 200", code == 200, f"status={code}")
        check("首页含看板标题", "台账看板" in body)

        code, body = get(base + "/static/app.js")
        check("/static/app.js 200", code == 200, f"status={code}")

        code, body = get(base + "/static/style.css")
        check("/static/style.css 200", code == 200, f"status={code}")

        code, body = get(base + "/api/pipelines?page=1&size=5")
        check("空库查列表不 500", code == 200, f"status={code} body={body[:120]}")

        code, body = get(base + "/openapi.json")
        check("openapi 可读", code == 200, f"status={code}")
        paths = list(json.loads(body)["paths"].keys()) if code == 200 else []
        methods = {m.upper() for ms in json.loads(body)["paths"].values() for m in ms} if code == 200 else set()
        check("接口数 10", len(paths) == 10, f"paths={paths}")
        check("方法只有 GET/POST", methods == {"GET", "POST"}, str(sorted(methods)))

        print("  --- 投一条探针报文 ---")
        payload = {
            "trace_id": "smoke-trace", "task_id": "smoke-task",
            "msg_type": "task_dispatch", "sender": "master",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "version": "v1.0",
            "payload": {"target_worker": "workbuddy", "task_goal": "冒烟探针"},
        }
        code, body = post(base + "/api/messages", payload)
        check("派发 202", code == 202, f"status={code} body={body[:120]}")

        code, body = get(base + "/api/pipelines/smoke-trace")
        ok = code == 200 and json.loads(body)["tasks"][0]["status"] == "执行中"
        check("任务进入执行中", ok, f"status={code}")

        code, body = get(base + "/api/export/smoke-trace?format=csv")
        check("导出 CSV 200", code == 200, f"status={code}")

        code, _ = post(base + "/api/commands", {"command": "master:pause", "trace_id": "smoke-trace"})
        check("暂停指令 200", code == 200, f"status={code}")
        code, _ = post(base + "/api/commands", {"command": "master:resume", "trace_id": "smoke-trace"})
        check("恢复指令 200", code == 200, f"status={code}")

        code, _ = post(base + "/api/commands", {"command": "nope", "trace_id": "smoke-trace"})
        check("未知指令 400", code == 400, f"status={code}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if FAILS:
        print(f"结果：FAIL {len(FAILS)} 项 -> {FAILS}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
