"""DSH 适配层（Batch33）。

把 `dsh headless` 当成一个执行器接给 Worker，**不改 DSH 自己的配置**：

* NVIDIA 路由：用仓库内的 `worker/dsh/nvidia-patch.yml` 通过 `--patch` 临时叠加。
  两台机器的 GUI profile 里都有 NVIDIA 段（Windows 在 desktop、Mac 在 web），
  但 `headless` profile 是空的 `[]`，直接跑会退回 DeepSeek 默认路由。
* 凭证：`NVIDIA_API_KEY` 从 `~/.dsh/.credentials.yaml` 的 `refs` 读出来，
  只塞进子进程的 env。**不写进仓库、不写日志、不打印**。
* 可执行文件：Windows 走 `%APPDATA%\\npm\\dsh.cmd`，Mac 走 `~/.dsh/profiles/node_modules/.../bin.js`。
  可用 `MWP_DSH_CMD` 覆盖（验收用桩程序）。

一次执行 = 一个子进程、一次退出码、一段 stdout；stdout 就是给平台的 logs。
"""
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH_PATH = os.path.join(ROOT, "worker", "dsh", "nvidia-patch.yml")
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"


class DshError(RuntimeError):
    pass


def dsh_home() -> str:
    return os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")


def credentials_path() -> str:
    override = os.environ.get("MWP_DSH_CREDENTIALS")
    if override:
        return override
    return os.path.join(dsh_home(), ".credentials.yaml")


def read_nvidia_key(path: str = None) -> str:
    """只认 `refs:` 段下的同名键；找不到返回 None（不猜、不从别处凑）。"""
    path = path or credentials_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    in_refs = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_refs = line.strip() == "refs:"
            continue
        if in_refs:
            match = re.match(r"\s+NVIDIA_API_KEY:\s*(\S+)\s*$", line)
            if match:
                value = match.group(1).strip("'\"")
                return value or None
    return None


def resolve_command() -> list:
    override = os.environ.get("MWP_DSH_CMD")
    if override:
        # 验收用桩程序时需要带参数（例如 "python C:\tmp\stub.py"），按 shell 规则切；
        # Windows 的 posix=False 会保留引号，这里再去掉一层，否则带空格的路径找不到。
        import shlex
        tokens = shlex.split(override, posix=(os.name != "nt"))
        return [t.strip('"').strip("'") for t in tokens]
    if os.name == "nt":
        cmd = os.path.join(os.environ.get("APPDATA", ""), "npm", "dsh.cmd")
        if not os.path.isfile(cmd):
            raise DshError(f"找不到 dsh：{cmd}（可设 MWP_DSH_CMD 指定）")
        return [cmd]
    node_bin = os.path.join(dsh_home(), "profiles", "node_modules",
                            "@deepseek-ai", "dsh", "lib", "bin.js")
    if not os.path.isfile(node_bin):
        raise DshError(f"找不到 dsh：{node_bin}（可设 MWP_DSH_CMD 指定）")
    return ["node", node_bin]


def build_command(goal: str, patch_path: str = None, profile: str = "headless") -> list:
    if not goal or not str(goal).strip():
        raise DshError("adapter=dsh 需要 payload.task_goal（不能为空）")
    patch = patch_path or os.environ.get("MWP_DSH_PATCH") or PATCH_PATH
    if not os.path.isfile(patch):
        raise DshError(f"NVIDIA patch 不存在：{patch}")
    return resolve_command() + [
        "--profile", profile,
        "--patch", patch,
        str(goal),
    ]


def run(goal: str, timeout: float = 120.0, patch_path: str = None,
        profile: str = "headless", cwd: str = None) -> dict:
    """跑一次 headless。返回 {exit_code, stdout, stderr, duration_ms, model, key_used}。"""
    key = read_nvidia_key()
    if not key:
        raise DshError("NVIDIA_API_KEY 没找到：~/.dsh/.credentials.yaml 的 refs 里没有，"
                       "或可用 MWP_DSH_CREDENTIALS / DSH_HOME 指到别处")
    cmd = build_command(goal, patch_path=patch_path, profile=profile)
    env = dict(os.environ)
    env["NVIDIA_API_KEY"] = key
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env)
        exit_code = proc.returncode
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        raise DshError(f"dsh 超时（> {timeout:.0f}s）")
    except OSError as exc:
        raise DshError(f"dsh 起不来：{exc}")
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": int((time.time() - started) * 1000),
        "model": os.environ.get("MWP_DSH_MODEL") or DEFAULT_MODEL,
        "key_used": True,
    }
