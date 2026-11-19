"""真 Worker 接入端（Batch9）：轮询平台拿活 → 执行 → 报活 → 交活。

设计原则：
1. **只走协议**。Worker 不 import `app.*`、不连数据库、不读台账，所有交互只有两条 HTTP：
   - `GET  /api/tasks?target_worker=<我>&status=执行中`（接活）
   - `POST /api/messages`（发 task_heartbeat / task_return / task_reject）
   这样平台随便改库结构，Worker 不受影响；Worker 也越不过平台的台账与状态机。
2. **执行有边界**。payload.exec 只接受"白名单目录内的 .py 文件 + 参数"，
   拒绝 `..` 越界、拒绝任意 shell 命令；每个任务有硬超时，超时即报 reject。
3. **只碰自己的活**。筛选用 target_worker=自己，且校验任务归属；别人的任务一律跳过。

执行规格（payload.exec，见 docs/base/09-Worker契约）：
    {
      "script": "scripts/demo_task.py",     # 必填，允许根目录内的 .py
      "args": ["--x", "1"],                  # 可选
      "timeout_seconds": 30,                 # 可选，另有全局上限
      "artifacts": ["out/hello.txt"]         # 可选，跑完回传这些相对路径
    }

用法：
    python worker/agent_worker.py --worker-id workbuddy --base-url http://127.0.0.1:8000
    python worker/agent_worker.py --once        # 处理一轮后退出（验收脚本用）
"""
import argparse
import concurrent.futures
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

ALLOWED_SENDERS = ("master", "dsh", "workbuddy", "codex", "user")
ACTIVE_STATUS = "执行中"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(event: str, **fields) -> None:
    """一行一条 JSON，方便监理侧直接 tail 与 grep。"""
    print(json.dumps({"ts": now_iso(), "event": event, **fields}, ensure_ascii=False), flush=True)


class PlatformError(RuntimeError):
    pass


class PlatformClient:
    """平台的最小客户端。只有 GET / POST，无 DELETE，无直连库。"""

    def __init__(self, base_url: str, token: str = "", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload=None):
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-API-Token"] = self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = {"raw": raw}
            return exc.code, parsed

    def list_active_tasks(self, worker_id: str, size: int = 50):
        qs = urllib.parse.urlencode(
            {"status": ACTIVE_STATUS, "target_worker": worker_id, "page": 1, "size": size}
        )
        code, body = self._request("GET", "/api/tasks?" + qs)
        if code != 200:
            raise PlatformError(f"拉取任务失败：HTTP {code} {body}")
        return (body or {}).get("items", [])

    def post_message(self, envelope: dict):
        return self._request("POST", "/api/messages", envelope)

    def report_worker(self, beat: dict):
        """Batch13：执行方注册表上报（忙闲都发，空转也能被看见）。"""
        return self._request("POST", "/api/workers", beat)


def envelope(trace_id: str, task_id: str, msg_type: str, sender: str, payload: dict) -> dict:
    """协议 v1.0 通用 6 字段 + payload。timestamp 必须 aware。"""
    return {
        "trace_id": trace_id,
        "task_id": task_id,
        "msg_type": msg_type,
        "sender": sender,
        "timestamp": now_iso(),
        "version": "v1.0",
        "payload": payload,
    }


class ExecSpecError(Exception):
    """执行规格不合法（越界 / 类型不符 / 文件不存在）。"""


def resolve_script(script: str, allow_roots) -> str:
    """把 exec.script 解析成一个绝对路径，且必须落在白名单目录内。"""
    if not isinstance(script, str) or not script.strip():
        raise ExecSpecError("exec.script 缺失或不是字符串")
    if os.path.isabs(script):
        candidate = os.path.abspath(script)
    else:
        candidate = os.path.abspath(os.path.join(ROOT, script))
    if not candidate.endswith(".py"):
        raise ExecSpecError(f"只允许执行 .py 文件：{script}")
    inside = False
    for root in allow_roots:
        root_abs = os.path.abspath(root)
        if os.path.commonpath([root_abs, candidate]) == root_abs:
            inside = True
            break
    if not inside:
        raise ExecSpecError(f"脚本越界（不在允许目录内）：{script}")
    if not os.path.isfile(candidate):
        raise ExecSpecError(f"脚本不存在：{script}")
    return candidate


class Heartbeat(threading.Thread):
    """执行期间按周期发心跳；主线程退出时 stop()。"""

    def __init__(self, client: PlatformClient, trace_id: str, task_id: str,
                 sender: str, interval: float, progress_text: str):
        super().__init__(daemon=True)
        self.client = client
        self.trace_id = trace_id
        self.task_id = task_id
        self.sender = sender
        self.interval = max(1.0, float(interval))
        self.progress_text = progress_text
        self._stop = threading.Event()
        self.sent = 0

    def run(self):
        while not self._stop.wait(self.interval):
            env = envelope(self.trace_id, self.task_id, "task_heartbeat", self.sender,
                           {"progress": 0.5, "message": self.progress_text})
            code, body = self.client.post_message(env)
            self.sent += 1
            if code != 202:
                log("heartbeat_failed", task_id=self.task_id, http=code, body=body)

    def stop(self):
        self._stop.set()


class Registry(threading.Thread):
    """周期上报"我还在 + 现在什么状态"（Batch13）。

    跟任务心跳的区别：心跳是"某个任务还在跑"，注册表是"这个进程还活着"。
    空转的 Worker 没有任务可心跳，但照样会注册——这就是 Batch12 在线视图的盲区。
    """

    def __init__(self, agent: "WorkerAgent", interval: float):
        super().__init__(daemon=True)
        self.agent = agent
        self.interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self.sent = 0

    def run(self):
        self.agent.report("running")
        self.sent += 1
        while not self._stop.wait(self.interval):
            self.agent.report("running")
            self.sent += 1

    def stop(self):
        self._stop.set()


class WorkerAgent:
    def __init__(self, client: PlatformClient, worker_id: str, allow_roots,
                 work_dir: str, heartbeat_interval: float, max_task_seconds: float,
                 state_file: str = "", concurrency: int = 1, status_file: str = "",
                 min_concurrency: int = 1, registry_interval: float = 15.0,
                 register: bool = True, instance_id: str = ""):
        if worker_id not in ALLOWED_SENDERS:
            raise SystemExit(f"--worker-id 必须是 {ALLOWED_SENDERS} 之一（sender 就是执行方身份）")
        self.client = client
        self.worker_id = worker_id
        self.allow_roots = allow_roots
        self.work_dir = work_dir
        self.heartbeat_interval = heartbeat_interval
        self.max_task_seconds = max_task_seconds
        self.state_file = state_file
        self.status_file = status_file
        # 弹性并发：min 是下限（至少同时跑几个），concurrency 是上限
        self.concurrency = max(1, int(concurrency))
        self.min_concurrency = max(1, min(int(min_concurrency), self.concurrency))
        # Batch13：注册表上报
        self.registry_interval = max(1.0, float(registry_interval))
        # Batch14：多实例标识。默认取主机名（同一台机重启沿用同一行，不堆垃圾行）；
        # 一台机要多跑几个 Worker 就显式传 --instance-id。
        self.instance_id = (instance_id or socket.gethostname() or "default")[:64]
        self.register = bool(register)
        self._live = {"in_flight": 0, "limit": 1, "handled": 0}
        self._register_404_logged = False
        self.done = self._load_state()
        # 优雅退出：收到 SIGTERM/SIGINT 后不再拉新活，等在跑的任务收尾再退
        self._stop_requested = threading.Event()

    # ---- 本地去重（平台侧状态已收敛的任务不会再出现在「执行中」，这里只防同轮重复） ----
    def _load_state(self):
        if not self.state_file or not os.path.isfile(self.state_file):
            return set()
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                return set(json.load(fh).get("done", []))
        except (OSError, ValueError):
            return set()

    def _save_state(self):
        if not self.state_file:
            return
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"done": sorted(self.done), "updated_at": now_iso()}, fh, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    def report(self, status: str) -> None:
        """上报注册表。平台老版本没有 /api/workers（404）时只提醒一次，不中断干活。"""
        if not self.register:
            return
        beat = {
            "worker_id": self.worker_id,
            "instance_id": self.instance_id,
            "status": status,
            "concurrency": self.concurrency,
            "limit": int(self._live.get("limit", 1)),
            "in_flight": int(self._live.get("in_flight", 0)),
            "handled": int(self._live.get("handled", 0)),
            "meta": {"min_concurrency": self.min_concurrency, "pid": os.getpid(),
                     "host": socket.gethostname()},
        }
        try:
            code, body = self.client.report_worker(beat)
        except (urllib.error.URLError, OSError) as exc:
            log("registry_failed", status=status, error=str(exc))
            return
        if code == 404:
            if not self._register_404_logged:
                self._register_404_logged = True
                log("registry_unsupported", note="平台没有 /api/workers，跳过注册（不影响干活）")
            return
        if code not in (200, 202):
            log("registry_failed", status=status, http=code, body=body)

    def _write_status(self, **fields) -> None:
        """守护进程/运维要看的那点信息，落一个小 JSON 文件（不占接口、不进台账）。"""
        # Batch13：注册表上报要用同一份快照，先更新再决定要不要落盘
        for key in ("in_flight", "limit", "handled"):
            if key in fields:
                self._live[key] = fields[key]
        if not self.status_file:
            return
        try:
            payload = {
                "worker_id": self.worker_id,
                "updated_at": now_iso(),
                "concurrency": self.concurrency,
                "done_total": len(self.done),
                **fields,
            }
            tmp = self.status_file + ".tmp"
            os.makedirs(os.path.dirname(os.path.abspath(self.status_file)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.status_file)
        except OSError as exc:  # 状态文件写不了不该拖垮执行
            log("status_write_failed", error=str(exc))

    @staticmethod
    def _priority(task: dict) -> int:
        """任务优先级：payload.priority（数字，越大越先）。缺省 0。"""
        payload = task.get("payload") or {}
        try:
            return int(payload.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    def _sort_key(self, task: dict):
        # 先按优先级降序，再按派发时间升序（早派的先做），最后按 task_id 稳定兜底
        return (-self._priority(task), task.get("dispatch_time") or "", task.get("task_id") or "")

    # ---- 单个任务 ----
    def handle(self, task: dict) -> None:
        task_id = task.get("task_id", "")
        trace_id = task.get("trace_id", "")
        if task.get("target_worker") != self.worker_id:
            log("skip_foreign_task", task_id=task_id, target_worker=task.get("target_worker"))
            return

        payload = task.get("payload") or {}
        spec = payload.get("exec") if isinstance(payload, dict) else None
        log("pick_task", task_id=task_id, trace_id=trace_id, task_goal=payload.get("task_goal"))

        if not isinstance(spec, dict):
            self._reject(trace_id, task_id, "exec 规格缺失：payload.exec 不存在或不是对象，Worker 不知道该执行什么")
            return

        try:
            script_path = resolve_script(spec.get("script"), self.allow_roots)
        except ExecSpecError as exc:
            self._reject(trace_id, task_id, f"exec 规格不合法：{exc}")
            return

        args = spec.get("args") or []
        if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            self._reject(trace_id, task_id, "exec.args 必须是字符串数组")
            return

        timeout = float(spec.get("timeout_seconds") or self.max_task_seconds)
        timeout = min(timeout, self.max_task_seconds)

        cmd = [PY, script_path] + args
        log("exec_start", task_id=task_id, script=script_path, args=args, timeout_s=timeout)
        hb = Heartbeat(self.client, trace_id, task_id, self.worker_id,
                       self.heartbeat_interval, f"正在执行 {os.path.basename(script_path)}")
        hb.start()
        started = time.time()
        try:
            proc = subprocess.run(cmd, cwd=self.work_dir, capture_output=True,
                                  text=True, timeout=timeout)
            elapsed_ms = int((time.time() - started) * 1000)
        except subprocess.TimeoutExpired:
            hb.stop()
            self._reject(trace_id, task_id, f"执行超时：{timeout:.0f}s 内未完成，已主动放弃（避免被判执行失败）")
            return
        finally:
            hb.stop()

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        log("exec_end", task_id=task_id, exit_code=proc.returncode, duration_ms=elapsed_ms,
            heartbeats=hb.sent)

        if proc.returncode != 0:
            tail = (stderr or stdout)[-400:]
            self._reject(trace_id, task_id, f"执行失败（退出码 {proc.returncode}）：{tail}")
            return

        artifacts = self._collect_artifacts(spec.get("artifacts"))
        self._return(trace_id, task_id, {
            "output": {
                "artifacts": artifacts,
                "exit_code": proc.returncode,
                "duration_ms": elapsed_ms,
                "script": os.path.relpath(script_path, ROOT),
            },
            "logs": stdout[-2000:],
            "metadata": {
                "self_check": "脚本正常退出（退出码 0）；产出路径已核验存在" if artifacts else "脚本正常退出（退出码 0）",
                "known_issues": "" if artifacts else "未声明 artifacts，产出未做存在性核验",
                "heartbeats": hb.sent,
                "worker_id": self.worker_id,
                "finished_at": now_iso(),
            },
        })

    def _collect_artifacts(self, declared):
        if not isinstance(declared, list):
            return []
        out = []
        for item in declared:
            if not isinstance(item, str):
                continue
            path = os.path.abspath(os.path.join(self.work_dir, item))
            out.append({"path": path, "exists": os.path.isfile(path),
                        "size_bytes": os.path.getsize(path) if os.path.isfile(path) else None})
        return out

    def _return(self, trace_id: str, task_id: str, payload: dict) -> None:
        code, body = self.client.post_message(
            envelope(trace_id, task_id, "task_return", self.worker_id, payload)
        )
        log("task_return", task_id=task_id, http=code, body=body)
        if code == 202:
            self.done.add(task_id)
            self._save_state()
        else:
            log("task_return_rejected", task_id=task_id, http=code, body=body)

    def _reject(self, trace_id: str, task_id: str, reason: str) -> None:
        code, body = self.client.post_message(
            envelope(trace_id, task_id, "task_reject", self.worker_id,
                     {"reason": reason, "details": {"worker_id": self.worker_id}})
        )
        log("task_reject", task_id=task_id, http=code, reason=reason, body=body)
        if code == 202:
            self.done.add(task_id)
            self._save_state()

    # ---- 主循环 ----
    def stop(self) -> None:
        """外部请求优雅退出（信号处理器会调它）。"""
        self._stop_requested.set()

    def run(self, once: bool = False, poll_interval: float = 2.0,
            max_tasks: int = 0, idle_rounds_before_exit: int = 0,
            grace_seconds: float = 30.0) -> int:
        handled = 0
        idle_rounds = 0
        backoff = 0
        inflight = {}

        def _on_signal(signum, _frame):
            log("signal_stop", signal=signum)
            self.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):  # 非主线程/平台不支持时忽略
                pass

        registry = None
        if self.register:
            registry = Registry(self, self.registry_interval)
            registry.start()

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="mw-task"
        )

        def _drain(timeout: float) -> None:
            """等在跑的任务收尾；超时就放弃等待（任务仍在别的进程里跑完会自己回执）。"""
            deadline = time.time() + max(0.0, timeout)
            for fut in list(inflight):
                remain = max(0.0, deadline - time.time())
                try:
                    fut.result(timeout=remain)
                except concurrent.futures.TimeoutExpired:
                    log("drain_timeout", pending=len(inflight))
                    return
                except Exception as exc:  # 单个任务异常不影响其它任务
                    log("task_error", error=str(exc))

        try:
            while True:
                # 回收已完成的任务（腾出并发槽位）
                for fut in [f for f in inflight if f.done()]:
                    inflight.pop(fut, None)

                stopping = self._stop_requested.is_set()
                if stopping:
                    log("stopping", inflight=len(inflight))
                    if registry:
                        registry.stop()
                        self.report("stopping")
                    _drain(grace_seconds)
                    return 0

                # 到达并发上限就先不拉新活（下限/上限之间按积压自适应，见下方 slots）
                if len(inflight) >= self.concurrency:
                    self._write_status(poll="busy", in_flight=len(inflight), handled=handled,
                                       limit=self.concurrency)
                    time.sleep(min(poll_interval, 1.0))
                    continue

                try:
                    tasks = self.client.list_active_tasks(self.worker_id)
                    backoff = 0
                except (PlatformError, urllib.error.URLError, OSError) as exc:
                    backoff = min(backoff + 1, 6)
                    log("poll_failed", error=str(exc), retry_in_s=backoff)
                    self._write_status(poll="failed", error=str(exc), in_flight=len(inflight))
                    if once and idle_rounds_before_exit and idle_rounds >= idle_rounds_before_exit:
                        return 1
                    time.sleep(backoff)
                    continue

                seen = set(self.done) | {t.get("task_id") for t in self._inflight_tasks(inflight)}
                todo = [t for t in tasks if t.get("task_id") not in seen]
                todo.sort(key=self._sort_key)

                # 弹性并发：积压多就多开，积压少就少开（夹在 min..max 之间）
                backlog = len(todo) + len(inflight)
                limit = min(self.concurrency, max(self.min_concurrency, backlog))
                slots = max(0, limit - len(inflight))

                if not todo:
                    idle_rounds += 1
                    self._write_status(poll="idle", active=len(tasks), in_flight=len(inflight),
                                       handled=handled)
                    log("idle", round=idle_rounds, active=len(tasks), in_flight=len(inflight))
                    if once and not inflight:
                        return 0
                    if idle_rounds_before_exit and idle_rounds >= idle_rounds_before_exit and not inflight:
                        return 0
                    if inflight:
                        time.sleep(min(poll_interval, 0.5))
                        continue
                    time.sleep(poll_interval)
                    continue

                idle_rounds = 0
                for task in todo[:slots]:
                    if self._stop_requested.is_set():
                        break
                    fut = executor.submit(self.handle, task)
                    inflight[fut] = task
                    handled += 1
                    log("task_queued", task_id=task.get("task_id"),
                        priority=self._priority(task), in_flight=len(inflight))
                    if max_tasks and handled >= max_tasks:
                        log("max_tasks_reached", handled=handled)
                        _drain(grace_seconds)
                        return 0

                self._write_status(poll="ok", in_flight=len(inflight), handled=handled,
                                   limit=limit, backlog=len(todo))
                if once:
                    # 一轮里可能还有活（并发槽位受限时按批提交），继续循环，
                    # 由"没待办且没在跑"的分支负责退出。
                    time.sleep(min(poll_interval, 0.2))
                    continue
                time.sleep(min(poll_interval, 0.5))
        finally:
            if registry:
                registry.stop()
                self.report("stopped")
            executor.shutdown(wait=False)

    @staticmethod
    def _inflight_tasks(inflight: dict):
        return [task for task in inflight.values() if isinstance(task, dict)]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="真 Worker 接入端（轮询平台拿活、执行、回执）")
    p.add_argument("--base-url", default="http://127.0.0.1:8000", help="平台地址")
    p.add_argument("--worker-id", default="workbuddy", help=f"执行方身份，取值 {ALLOWED_SENDERS}")
    p.add_argument("--token", default=os.environ.get("MW_API_TOKEN", ""), help="X-API-Token（平台开了鉴权才需要）")
    p.add_argument("--allow-root", action="append", default=None,
                   help="允许执行脚本的目录，可重复；默认仓库根目录")
    p.add_argument("--work-dir", default=ROOT, help="脚本工作目录（cwd），默认仓库根")
    p.add_argument("--heartbeat-interval", type=float, default=5.0, help="心跳间隔秒（平台默认 120s 判超时）")
    p.add_argument("--poll-interval", type=float, default=2.0, help="空闲轮询间隔秒")
    p.add_argument("--max-task-seconds", type=float, default=300.0, help="单任务硬超时上限（秒）")
    p.add_argument("--max-tasks", type=int, default=0, help="处理够 N 个任务就退出，0=不限")
    p.add_argument("--idle-exit-rounds", type=int, default=0, help="连续 N 轮没活就退出，0=常驻")
    p.add_argument("--once", action="store_true", help="处理一轮（含并发的多个任务）后退出")
    p.add_argument("--state-file", default="", help="本地已完成任务记录，防止重启重复处理")
    p.add_argument("--concurrency", type=int, default=1, help="并发执行的任务数上限（默认 1=串行）")
    p.add_argument("--min-concurrency", type=int, default=1,
                   help="弹性并发的下限：至少同时跑几个（默认 1；积压多时会自动涨到 --concurrency 上限）")
    p.add_argument("--grace-seconds", type=float, default=30.0, help="收到停止信号后等在跑任务收尾的秒数")
    p.add_argument("--status-file", default="", help="运行时状态 JSON 落盘路径（守护与运维用）")
    p.add_argument("--registry-interval", type=float, default=15.0,
                   help="执行方注册表上报间隔秒（Batch13；忙闲都报，空转也能被看见）")
    p.add_argument("--instance-id", default="",
                   help="多实例标识（Batch14），默认取主机名；一台机跑多个 Worker 时各自指定")
    p.add_argument("--no-register", action="store_true",
                   help="不上报注册表（平台没有 /api/workers 或不想被记录时用）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    allow_roots = args.allow_root or [ROOT]
    agent = WorkerAgent(
        client=PlatformClient(args.base_url, args.token),
        worker_id=args.worker_id,
        allow_roots=allow_roots,
        work_dir=args.work_dir,
        heartbeat_interval=args.heartbeat_interval,
        max_task_seconds=args.max_task_seconds,
        state_file=args.state_file,
        concurrency=args.concurrency,
        status_file=args.status_file,
        min_concurrency=args.min_concurrency,
        registry_interval=args.registry_interval,
        register=not args.no_register,
        instance_id=args.instance_id,
    )
    log("worker_start", worker_id=args.worker_id, base_url=args.base_url,
        allow_roots=allow_roots, work_dir=args.work_dir)
    return agent.run(once=args.once, poll_interval=args.poll_interval,
                     max_tasks=args.max_tasks,
                     idle_rounds_before_exit=args.idle_exit_rounds,
                     grace_seconds=args.grace_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
