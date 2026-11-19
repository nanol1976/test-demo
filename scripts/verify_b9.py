"""Batch9 验收脚本：真 Worker 接平台跑一条端到端真流水线。

与前面几批的区别：这里**不用 TestClient 造数据**，而是
1. 真起一个 uvicorn 进程（干净库 + 干净归档目录）；
2. 真起一个 Worker 子进程（worker/agent_worker.py），它自己轮询、自己执行、自己回执；
3. 断言全部从平台 HTTP 侧读回来，外加核对 Worker 真写出来的产物文件。

覆盖：
A 治理面：接口仍 9 个、方法只有 GET/POST（Worker 接入不得新增接口）
B 派发：payload.target_worker 生效、载荷原文可回读
C 真执行：Worker 跑脚本 → 产出文件真实存在 → task_return → 待验收
D 心跳：执行期间真有 task_heartbeat 落归档（不是补发的）
E 只碰自己的活：派给 codex 的任务，workbuddy 一轮后仍是「执行中」
F 规整校验：exec 越界 → task_reject（不执行）
G 失败链路：脚本非 0 退出 → task_reject → 验收驳回
H 幂等：同一 dispatch 重投 → 同 archive_id，状态不回退
I 看板/导出：列表能按执行方筛到、能导出 json/csv、首页能开
"""
import json
import os
import shutil
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

DB = "./data/verify_b9.db"
ARCHIVE = "./archive/_verify_b9"
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
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype and body:
                return resp.status, json.loads(body)
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body


def get(base, path, timeout=15):
    return request("GET", base + path, timeout=timeout)


def post(base, path, payload):
    return request("POST", base + path, payload)


def wait_up(base, timeout=30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = request("GET", base + "/api/health", timeout=2)
            if code == 200:
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return False


def envelope(trace_id, task_id, msg_type, sender, payload, ts):
    return {
        "trace_id": trace_id, "task_id": task_id, "msg_type": msg_type,
        "sender": sender, "timestamp": ts, "version": "v1.0", "payload": payload,
    }


def iso(dt):
    return dt.isoformat()


def archive_counts(trace_id):
    """读归档 JSONL，按 msg_type 计数。这是"心跳真的发过"的唯一硬证据。"""
    code, body = get(BASE, f"/api/archive/{trace_id}")
    if code != 200 or not isinstance(body, dict):
        return {}
    path = body.get("jsonl_path") or ""
    if not path:
        return {}
    full = path if os.path.isabs(path) else os.path.join(ROOT, path)
    counts = {}
    if not os.path.isfile(full):
        return counts
    with open(full, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                msg_type = json.loads(line).get("msg_type")
            except ValueError:
                continue
            counts[msg_type] = counts.get(msg_type, 0) + 1
    return counts


def write_task_scripts(tmpdir) -> dict:
    """任务脚本由验收自己生成：不进仓库，也不依赖仓库里的样例。"""
    slow = os.path.join(tmpdir, "slow_task.py")
    with open(slow, "w", encoding="utf-8") as fh:
        fh.write(
            "import json,os,sys,time\n"
            "out=os.path.join(os.getcwd(),'artifacts','slow.txt')\n"
            "os.makedirs(os.path.dirname(out),exist_ok=True)\n"
            "for i in range(6):\n"
            "    with open(out,'w',encoding='utf-8') as f:\n"
            "        json.dump({'step':i,'at':time.time()},f)\n"
            "    print('step',i,flush=True)\n"
            "    time.sleep(1)\n"
            "print('done')\n"
        )
    failing = os.path.join(tmpdir, "failing_task.py")
    with open(failing, "w", encoding="utf-8") as fh:
        fh.write(
            "import sys\n"
            "print('start')\n"
            "print('boom: 故意失败', file=sys.stderr)\n"
            "sys.exit(3)\n"
        )
    outside_dir = tempfile.mkdtemp(prefix="b9_outside_")
    outside = os.path.join(outside_dir, "escaping_task.py")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("print('should never run')\n")
    return {"slow": slow, "failing": failing, "outside": outside, "outside_dir": outside_dir}


def run_worker(base, tmpdir, extra_args=None, timeout=120):
    cmd = [
        PY, os.path.join(ROOT, "worker", "agent_worker.py"),
        "--base-url", base,
        "--worker-id", "workbuddy",
        "--token", TOKEN,
        "--allow-root", tmpdir,
        "--allow-root", ROOT,
        "--work-dir", tmpdir,
        "--heartbeat-interval", "2",
        "--max-task-seconds", "30",
        "--once",
    ]
    cmd += extra_args or []
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return proc


def main() -> int:
    global BASE
    if os.path.exists(DB):
        os.remove(DB)
    if os.path.isdir(ARCHIVE):
        shutil.rmtree(ARCHIVE)
    os.makedirs("./data", exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="b9_tasks_")
    scripts = write_task_scripts(tmpdir)

    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["DATABASE_URL"] = f"sqlite:///{DB}"
    env["ARCHIVE_ROOT"] = ARCHIVE
    env["API_TOKEN"] = TOKEN
    env["SCAN_INTERVAL_SECONDS"] = "3600"

    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        print("=== A 环境与治理面 ===")
        up = wait_up(BASE)
        check("真起服务（干净库）25s 内可用", up)
        if not up:
            print((server.stdout.read() if server.stdout else "")[-2000:])
            return 1
        code, spec = get(BASE, "/openapi.json")
        paths = list(spec["paths"].keys()) if code == 200 else []
        methods = {m.upper() for p in spec["paths"].values() for m in p} if code == 200 else set()
        check("接口数仍为 9（Worker 接入未加接口）", len(paths) == 9, f"{len(paths)} 个")
        check("方法只有 GET/POST", methods <= {"GET", "POST"}, str(sorted(methods)))

        print("=== B 派发：执行方归属与载荷落库 ===")
        t_ok = "t-b9-ok"
        ts_ok = "2026-11-19T15:00:00+00:00"
        ok_payload = {
            "target_worker": "workbuddy",
            "task_goal": "端到端：跑一个慢任务并产出文件",
            "exec": {"script": scripts["slow"], "timeout_seconds": 20,
                     "artifacts": ["artifacts/slow.txt"]},
        }
        code, body = post(BASE, "/api/messages",
                          envelope(t_ok, "t-b9-ok-1", "task_dispatch", "master", ok_payload, ts_ok))
        check("派发 ok 任务 202", code == 202, f"status={code} body={str(body)[:120]}")
        code, task = get(BASE, "/api/tasks/t-b9-ok-1")
        check("target_worker=payload 指定的 workbuddy（不是 sender=master）",
              code == 200 and task.get("target_worker") == "workbuddy",
              str(task.get("target_worker")) if code == 200 else f"HTTP {code}")
        check("载荷原样可回读（exec.script 在里面）",
              isinstance(task.get("payload"), dict)
              and (task["payload"].get("exec") or {}).get("script") == scripts["slow"],
              str((task.get("payload") or {}).get("exec"))[:120])

        # 越界脚本 / 别人的活，一起派下去，看 Worker 一轮里怎么处理
        post(BASE, "/api/messages",
             envelope("t-b9-bad", "t-b9-bad-1", "task_dispatch", "master",
                      {"target_worker": "workbuddy", "task_goal": "越界脚本",
                       "exec": {"script": scripts["outside"]}},
                      "2026-11-19T15:00:05+00:00"))
        post(BASE, "/api/messages",
             envelope("t-b9-foreign", "t-b9-foreign-1", "task_dispatch", "master",
                      {"target_worker": "codex", "task_goal": "不是给 workbuddy 的活"},
                      "2026-11-19T15:00:10+00:00"))

        print("=== C 真执行：起 Worker 子进程 ===")
        started = time.time()
        proc = run_worker(BASE, tmpdir)
        elapsed = round(time.time() - started, 1)
        worker_log = (proc.stdout or "") + (proc.stderr or "")
        check("Worker 进程正常退出", proc.returncode == 0, f"rc={proc.returncode} 用时 {elapsed}s")
        check("Worker 日志里有 pick_task（真领到活）", "pick_task" in worker_log)
        # 平台侧按 target_worker 过滤，别人的任务压根不会出现在它的队列里（比事后跳过更硬）
        check("别人的任务没进过 Worker 的处理队列",
              "t-b9-foreign-1" not in worker_log, "日志里出现了 foreign 任务 id")

        code, task = get(BASE, "/api/tasks/t-b9-ok-1")
        check("ok 任务收敛到「待验收」", task.get("status") == "待验收", str(task.get("status")))
        check("return_time 已落库", task.get("return_time") is not None, str(task.get("return_time")))
        artifact = os.path.join(tmpdir, "artifacts", "slow.txt")
        check("产物文件真实存在（不是演的）", os.path.isfile(artifact), artifact)
        if os.path.isfile(artifact):
            with open(artifact, encoding="utf-8") as fh:
                content = json.load(fh)
            check("产物内容是脚本最后一次写入（step=5）", content.get("step") == 5, str(content))

        counts = archive_counts(t_ok)
        check("归档里有 task_dispatch", counts.get("task_dispatch") == 1, str(counts))
        check("归档里有 task_heartbeat（执行期真发的）", counts.get("task_heartbeat", 0) >= 1, str(counts))
        check("归档里有 task_return", counts.get("task_return") == 1, str(counts))
        check("归档行数 ≥3（派发/心跳/回执）", sum(counts.values()) >= 3, str(sum(counts.values())))

        print("=== D 越权与规整 ===")
        code, foreign = get(BASE, "/api/tasks/t-b9-foreign-1")
        check("派给 codex 的任务 workbuddy 不碰，仍是「执行中」",
              foreign.get("status") == "执行中", str(foreign.get("status")))
        code, bad = get(BASE, "/api/tasks/t-b9-bad-1")
        check("越界脚本 → 验收驳回（拒绝执行）", bad.get("status") == "验收驳回", str(bad.get("status")))
        check("驳回原因写清「越界」", "越界" in (bad.get("reject_reason") or ""), str(bad.get("reject_reason")))

        print("=== E 幂等：同一派发重投 ===")
        code, again = post(BASE, "/api/messages",
                           envelope(t_ok, "t-b9-ok-1", "task_dispatch", "master", ok_payload, ts_ok))
        check("重投仍 202", code == 202, f"status={code} body={str(again)[:120]}")
        check("返回同一个 archive_id（幂等键生效）", (again or {}).get("archive_id") is not None,
              str(again))
        check("status_changed=false（状态不回退）", (again or {}).get("status_changed") is False,
              str(again))
        code, task = get(BASE, "/api/tasks/t-b9-ok-1")
        check("状态仍是「待验收」没被打回执行中", task.get("status") == "待验收", str(task.get("status")))

        print("=== F 失败链路：脚本非 0 退出 → task_reject ===")
        code, _ = post(BASE, "/api/messages",
                       envelope("t-b9-fail", "t-b9-fail-1", "task_dispatch", "master",
                                {"target_worker": "workbuddy", "task_goal": "脚本要失败",
                                 "exec": {"script": scripts["failing"]}},
                                "2026-11-19T15:01:00+00:00"))
        proc2 = run_worker(BASE, tmpdir)
        log2 = (proc2.stdout or "") + (proc2.stderr or "")
        check("第二轮 Worker 正常退出", proc2.returncode == 0, f"rc={proc2.returncode}")
        check("Worker 记录了 task_reject", "task_reject" in log2)
        code, fail_task = get(BASE, "/api/tasks/t-b9-fail-1")
        check("失败任务 → 验收驳回", fail_task.get("status") == "验收驳回", str(fail_task.get("status")))
        check("驳回原因带退出码", "退出码 3" in (fail_task.get("reject_reason") or ""),
              str(fail_task.get("reject_reason")))

        print("=== G 台账与看板可查 ===")
        code, page = get(BASE, "/api/tasks?target_worker=workbuddy&status=%E5%BE%98%E5%B1%9E%E6%89%9B")
        code2, page2 = get(BASE, "/api/tasks?" + urllib.parse.urlencode(
            {"target_worker": "workbuddy", "status": "待验收"}))
        check("列表按执行方+状态能筛到刚跑完的任务",
              code2 == 200 and page2["meta"]["total"] >= 1 and
              any(i["task_id"] == "t-b9-ok-1" for i in page2["items"]),
              str(page2.get("meta")) if code2 == 200 else f"HTTP {code2}")
        code, pipe = get(BASE, f"/api/pipelines/{t_ok}")
        check("流水线详情 200 且能列出任务", code == 200 and any(
            t["task_id"] == "t-b9-ok-1" for t in pipe.get("tasks", [])), f"HTTP {code}")
        code, exp = get(BASE, f"/api/export/{t_ok}?format=json")
        check("导出 json 200", code == 200 and isinstance(exp, dict), f"HTTP {code}")
        code, exp = get(BASE, f"/api/export/{t_ok}?format=csv")
        check("导出 csv 200", code == 200 and isinstance(exp, str) and "task_id" in exp, f"HTTP {code}")
        code, html = get(BASE, "/")
        check("看板首页可开", code == 200 and "台账看板" in str(html), f"HTTP {code}")

        print("=== H Worker 侧静态红线（不该有的依赖）===")
        worker_src = open(os.path.join(ROOT, "worker", "agent_worker.py"), encoding="utf-8").read()
        check("Worker 不 import app.*（只能走 HTTP）", "from app" not in worker_src and "import app" not in worker_src)
        check("Worker 不直连数据库（无 sqlalchemy）", "sqlalchemy" not in worker_src.lower())
        check("Worker 没有删除能力", ".delete(" not in worker_src and "DELETE FROM" not in worker_src)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(scripts["outside_dir"], ignore_errors=True)

    print(f"\n结果：{'全部通过' if not FAILS else '有失败项'}（{len(FAILS)} 项失败）")
    if FAILS:
        for f in FAILS:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
