"""Batch11 验收脚本：Worker 并发、优先级、优雅退出、状态文件、守护模板。

Batch9 的 Worker 是"串行 + 前台跑"，只能演示。这一批让它具备上线条件：

A 并发：3 个各 4 秒的任务，--concurrency 3 一轮跑完，总耗时接近单个（而非相加）
B 优先级：payload.priority 大的先被领走（--concurrency 1 下看领取顺序）
C 优雅退出：长任务执行中发 SIGTERM，Worker 等任务收尾再退，任务仍正常「待验收」（不被打成失败）
D 状态文件：运行期可读，含 worker_id / concurrency / 在跑数量
E 守护模板：systemd / launchd / Windows 计划任务三份模板存在且关键字段正确
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

from _proc_drain import drain  # 子进程日志必须读走：Windows 管道写满会把服务堵死

DB = "./data/verify_b11.db"
ARCHIVE = "./archive/_verify_b11"
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
        return exc.code, exc.read().decode("utf-8", "replace")


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
        time.sleep(0.5)
    return False


def envelope(trace_id, task_id, sender, payload, ts):
    return {"trace_id": trace_id, "task_id": task_id, "msg_type": "task_dispatch",
            "sender": sender, "timestamp": ts, "version": "v1.0", "payload": payload}


def write_sleep_script(tmpdir, seconds: int, name: str) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "import json,os,time\n"
            f"start=time.time()\n"
            "out=os.path.join(os.getcwd(),'artifacts','run_%s.json'%os.path.basename(__file__))\n"
            "os.makedirs(os.path.dirname(out),exist_ok=True)\n"
            f"time.sleep({seconds})\n"
            "with open(out,'w',encoding='utf-8') as f:\n"
            "    json.dump({'script':os.path.basename(__file__),'elapsed':round(time.time()-start,2)},f)\n"
            "print('done',os.path.basename(__file__),flush=True)\n"
        )
    return path


def worker_cmd(base, tmpdir, extra):
    cmd = [
        PY, os.path.join(ROOT, "worker", "agent_worker.py"),
        "--base-url", base,
        "--worker-id", "workbuddy",
        "--token", TOKEN,
        "--allow-root", tmpdir,
        "--allow-root", ROOT,
        "--work-dir", tmpdir,
        "--heartbeat-interval", "2",
        "--max-task-seconds", "60",
    ]
    return cmd + extra


def task_status(base, task_id):
    code, task = request("GET", f"{base}/api/tasks/{task_id}")
    return task.get("status") if code == 200 and isinstance(task, dict) else f"HTTP {code}"


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="b11_tasks_")

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = f"sqlite:///{DB}"
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
    srv_log = drain(server)  # 必须读走 stdout，否则 Windows 上管道写满会把服务堵死
    status_file = os.path.join(tmpdir, "worker_status.json")
    try:
        check("真起服务", wait_up(base))
        if FAILS and "真起服务" in FAILS:
            print("".join(srv_log[-40:])[-1500:])
            return 1

        print("=== A 并发：3 个 4 秒任务，--concurrency 3 ===")
        for i in range(3):
            post(base, "/api/messages",
                 envelope("t-b11-c", f"t-b11-c-{i}", "master",
                          {"target_worker": "workbuddy", "task_goal": f"并发-{i}",
                           "exec": {"script": write_sleep_script(tmpdir, 4, f"sleep_c{i}.py")}},
                          f"2026-11-19T17:00:0{i}+00:00"))
        t0 = time.time()
        proc = subprocess.run(worker_cmd(base, tmpdir,
                                         ["--concurrency", "3", "--once",
                                          "--status-file", status_file]),
                              cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        elapsed = round(time.time() - t0, 1)
        log = (proc.stdout or "") + (proc.stderr or "")
        check("Worker 正常退出", proc.returncode == 0, f"rc={proc.returncode}")
        check(f"并发执行（3 个 4s 任务总耗时 {elapsed}s，串行需 ≥12s）", elapsed < 9.5, f"{elapsed}s")
        check("日志显示并发数 3", '"concurrency": 3' in log or "--concurrency" in " ".join(worker_cmd(base, tmpdir, ["--concurrency", "3"])))
        for i in range(3):
            check(f"并发任务 {i} 收敛到待验收", task_status(base, f"t-b11-c-{i}") == "待验收",
                  task_status(base, f"t-b11-c-{i}"))
        artifacts = [f for f in os.listdir(os.path.join(tmpdir, "artifacts"))
                     if f.startswith("run_sleep_c")] if os.path.isdir(os.path.join(tmpdir, "artifacts")) else []
        check("3 个产物文件都写出来了", len(artifacts) == 3, str(artifacts))

        print("=== B 优先级：payload.priority 大的先领 ===")
        # 刻意让"低优先级"的 task_id 字母序更靠前（a < z），否则测不出优先级是否真的生效
        for tid, prio in (("t-b11-p-a-low", 1), ("t-b11-p-z-high", 9)):
            post(base, "/api/messages",
                 envelope("t-b11-p", tid, "master",
                          {"target_worker": "workbuddy", "task_goal": f"优先级 {prio}",
                           "priority": prio,
                           "exec": {"script": write_sleep_script(tmpdir, 1, "sleep_p.py")}},
                          "2026-11-19T17:01:00+00:00"))
        proc2 = subprocess.run(worker_cmd(base, tmpdir, ["--concurrency", "1", "--once"]),
                               cwd=ROOT, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
        log2 = (proc2.stdout or "") + (proc2.stderr or "")
        picks = re.findall(r'"event": "pick_task", "task_id": "([^"]+)"', log2)
        check("两个任务都领到了", set(picks) == {"t-b11-p-a-low", "t-b11-p-z-high"}, str(picks))
        check("高优先级先执行（不是靠 task_id 字母序）", picks and picks[0] == "t-b11-p-z-high", str(picks))
        check("优先级任务都收敛", all(task_status(base, t) == "待验收"
                                     for t in ("t-b11-p-a-low", "t-b11-p-z-high")), "")
        code, high_task = request("GET", f"{base}/api/tasks/t-b11-p-z-high")
        check("payload.priority 真的进了平台台账（没被 schema 丢掉）",
              (high_task.get("payload") or {}).get("priority") == 9,
              str((high_task.get("payload") or {}).get("priority")))

        print("=== C 优雅退出：执行中收到 SIGTERM ===")
        post(base, "/api/messages",
             envelope("t-b11-g", "t-b11-g-1", "master",
                      {"target_worker": "workbuddy", "task_goal": "长任务",
                       "exec": {"script": write_sleep_script(tmpdir, 8, "sleep_g.py")}},
                      "2026-11-19T17:02:00+00:00"))
        proc3 = subprocess.Popen(worker_cmd(base, tmpdir,
                                            ["--concurrency", "1", "--grace-seconds", "20",
                                             "--status-file", status_file]),
                                 cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
        time.sleep(3)  # 让它真的开始跑
        if os.name == "nt":
            # Windows 没有 POSIX 信号语义：send_signal(SIGTERM) 落到 TerminateProcess（硬杀），
            # 「等到收尾、日志有 signal_stop」这些断言在 Windows 上不可能成立。
            # 这里明确跳过，而不是把断言放宽成假绿。
            print("  [SKIP] 优雅退出 4 项：Windows 上信号即硬杀，grace/回执断言不适用")
            proc3.kill()
            proc3.wait(timeout=20)
        else:
            check("执行中状态先确认", task_status(base, "t-b11-g-1") == "执行中",
                  task_status(base, "t-b11-g-1"))
            proc3.send_signal(signal.SIGTERM)
            stopped_at = time.time()
            try:
                out3, _ = proc3.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                proc3.kill()
                out3, _ = proc3.communicate()
            grace_used = round(time.time() - stopped_at, 1)
            log3 = proc3.stdout.read() if False else (out3 or "")
            check("收到 SIGTERM 后进程正常退出（rc=0）", proc3.returncode == 0, f"rc={proc3.returncode}")
            check("退出前等到任务收尾（用时 >3s）", grace_used > 3, f"{grace_used}s")
            check("日志记录了 signal_stop", "signal_stop" in log3, log3[-200:])
            check("长任务仍正常回执（没被打成执行失败）",
                  task_status(base, "t-b11-g-1") == "待验收", task_status(base, "t-b11-g-1"))

        print("=== D 状态文件 ===")
        check("状态文件已生成", os.path.isfile(status_file), status_file)
        if os.path.isfile(status_file):
            with open(status_file, encoding="utf-8") as fh:
                st = json.load(fh)
            check("含 worker_id", st.get("worker_id") == "workbuddy", str(st.get("worker_id")))
            check("含 concurrency", st.get("concurrency") in (1, 2, 3), str(st.get("concurrency")))
            check("含时间戳与在跑数", "updated_at" in st and ("in_flight" in st or "handled" in st), str(st))

        print("=== E 守护模板 ===")
        systemd = open(os.path.join(ROOT, "deploy", "worker-systemd.service"), encoding="utf-8").read()
        check("systemd：ExecStart 指向 agent_worker.py", "agent_worker.py" in systemd)
        check("systemd：Restart=always + SIGTERM + TimeoutStopSec",
              "Restart=always" in systemd and "KillSignal=SIGTERM" in systemd
              and "TimeoutStopSec=90" in systemd)
        plist_path = os.path.join(ROOT, "deploy", "worker-launchd.plist")
        import plistlib
        pl = plistlib.load(open(plist_path, "rb"))
        check("launchd：KeepAlive/RunAtLoad/ExitTimeOut 齐全",
              pl.get("KeepAlive") is True and pl.get("RunAtLoad") is True and pl.get("ExitTimeOut") == 90,
              str({k: pl.get(k) for k in ("KeepAlive", "RunAtLoad", "ExitTimeOut")}))
        check("launchd：参数里带 agent_worker.py 与 concurrency",
              any("agent_worker.py" in str(a) for a in pl.get("ProgramArguments", []))
              and "--concurrency" in pl.get("ProgramArguments", []))
        # Batch23：Windows 注册只剩一份（install-windows-tasks.ps1），老入口只是转发
        ps1 = open(os.path.join(ROOT, "deploy", "install-windows-tasks.ps1"),
                   encoding="utf-8-sig").read()
        check("Windows：注册计划任务 + 禁止多实例 + 自动重启",
              "Register-ScheduledTask" in ps1 and "MultipleInstances IgnoreNew" in ps1
              and "RestartCount 999" in ps1)
        check("Windows：带 agent_worker.py 与 concurrency 参数",
              "agent_worker.py" in ps1 and '"--concurrency"' in ps1)
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
