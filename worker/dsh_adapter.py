"""DSH 适配层（Batch33，Batch33-1 加按单模型）。

把 `dsh headless` 当成一个执行器接给 Worker，**不改 DSH 自己的配置**：

* NVIDIA 路由：用仓库内的 `worker/dsh/nvidia-patch.yml` 通过 `--patch` 临时叠加。
  两台机器的 GUI profile 里都有 NVIDIA 段（Windows 在 desktop、Mac 在 web），
  但 `headless` profile 是空的 `[]`，直接跑会退回 DeepSeek 默认路由。
* 按单指定模型（Batch33-1）：`exec.model` 或环境变量 `MWP_DSH_MODEL` 要跟默认模型不一样时，
  拿仓库 patch 当模板渲染一份临时 patch（放 run/tmp_dsh 复用缓存），**仓库里的 patch 不动**。
* 思考档（Batch33-2）：`exec.reasoning_effort`（minimal/low/medium/high）写进渲染 patch 的
  `agent-default-model.config.reasoningEffort`；不指定就不写这一行，用 dsh 自己的默认。
* 产物留档（Batch33-2）：每次执行把 stdout/stderr 全量写进 `run/artifacts/<task_id>.log`，
  回执里带相对路径、字节数、sha256 前 12 位——平台侧看板据此可查、可核完整。
* 凭证：`NVIDIA_API_KEY` 从 `~/.dsh/.credentials.yaml` 的 `refs` 读出来，
  只塞进子进程的 env。**不写进仓库、不写日志、不打印**。
* 可执行文件：Windows 走 `%APPDATA%\\npm\\dsh.cmd`，Mac 走 `~/.dsh/profiles/node_modules/.../bin.js`。
  可用 `MWP_DSH_CMD` 覆盖（验收用桩程序）。

一次执行 = 一个子进程、一次退出码、一段 stdout；stdout 就是给平台的 logs。
"""
import hashlib
import os
import re
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH_PATH = os.path.join(ROOT, "worker", "dsh", "nvidia-patch.yml")
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_NAME = "nemotron-3.5-lightning-30b-a3b"
TMP_PATCH_ROOT = os.path.join(ROOT, "run", "tmp_dsh")
RUNTIME_HOME = os.path.join(ROOT, "run", "dsh_home")
ARTIFACT_ROOT = os.path.join(ROOT, "run", "artifacts")

# 模型 id 只允许这一套字符：slug、带一个斜杠的 owner/repo、带点/冒号/@ 的版本后缀。
# 拦住 ../、空格、换行这些——它是写进 yml 的，不是拼 shell，但也不能当成路径或注入。
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,80}$")

# 思考档只认这几个词（dsh / 各家 provider 共用的口径），别的值一律拒。
EFFORT_VALUES = ("minimal", "low", "medium", "high")

# 任务号会当文件名用，只允许这套字符，别的全部替换成下划线。
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 按模型缓存的 patch 只增不删会越攒越多；超过这些天没动过就顺手清一次（删不掉就算了）。
CACHE_DAYS = 7.0


class DshError(RuntimeError):
    pass


def dsh_home() -> str:
    """dsh 运行用的 home：默认放仓库 run/dsh_home，不碰用户自己的 ~/.dsh。

    dsh 每次启动都会重写 `profiles/<name>/cordis.yml`（compose 的锚点文件），
    指着用户的 home 就等于让 Worker 去改用户的配置目录；指到仓库里，
    既不改用户的，也不会被"禁止写用户目录"的安全策略拦掉。
    """
    return os.environ.get("MWP_DSH_HOME") or os.environ.get("DSH_HOME") or RUNTIME_HOME


def user_dsh_home() -> str:
    return os.path.join(os.path.expanduser("~"), ".dsh")


def credentials_path() -> str:
    override = os.environ.get("MWP_DSH_CREDENTIALS")
    if override:
        return override
    return os.path.join(user_dsh_home(), ".credentials.yaml")


def ensure_profile(profile: str = "headless") -> str:
    """把用户 home 里那个 profile 的小文件（不含 node_modules）复制到运行 home。

    只复制、不回写；已经存在的就不动。bundle 列表在 package.json 里，照抄即可。
    """
    src = os.path.join(user_dsh_home(), "profiles", profile)
    dst = os.path.join(dsh_home(), "profiles", profile)
    os.makedirs(dst, exist_ok=True)
    if os.path.isdir(src):
        for name in ("cordis.yml", "cordis.patch.yml", "package.json", "pnpm-workspace.yaml"):
            s = os.path.join(src, name)
            d = os.path.join(dst, name)
            if os.path.isfile(s) and not os.path.isfile(d):
                with open(s, "r", encoding="utf-8") as fh:
                    text = fh.read()
                with open(d, "w", encoding="utf-8") as fh:
                    fh.write(text)
    return dst


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


def prune_cache(days: float = None) -> list:
    """清掉 `run/tmp_dsh` 下超过 N 天没动过的 patch 缓存，返回清掉的文件名。

    只动自己写的 `patch-*.yml`，别的文件一概不碰；删不掉（有的机器拦删除）就跳过，
    不抛异常——缓存是加速用的，清不动最多是多占几 KB，不该影响这一单。
    """
    limit = CACHE_DAYS if days is None else float(days)
    cutoff = time.time() - limit * 86400
    removed = []
    if not os.path.isdir(TMP_PATCH_ROOT):
        return removed
    for name in os.listdir(TMP_PATCH_ROOT):
        if not name.startswith("patch-") or not name.endswith(".yml"):
            continue
        full = os.path.join(TMP_PATCH_ROOT, name)
        try:
            if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                os.remove(full)
                removed.append(name)
        except OSError:
            continue
    return removed


def check_model(model) -> str:
    """校验模型名，返回去掉空白的值；不合法就抛 DshError（不静默降级）。"""
    value = str(model or "").strip()
    if not value:
        raise DshError("模型名是空的")
    if not MODEL_RE.match(value) or ".." in value:
        raise DshError(f"模型名不合法：{value!r}（只允许字母数字和 . _ : @ / -，不许出现 ..）")
    return value


def resolve_model(model: str = None) -> str:
    """按单 model > 环境变量 MWP_DSH_MODEL > 仓库 patch 里的默认模型。"""
    if model is not None and str(model).strip():
        return check_model(model)
    env_model = os.environ.get("MWP_DSH_MODEL")
    if env_model and env_model.strip():
        return check_model(env_model)
    return DEFAULT_MODEL


def check_effort(effort) -> str:
    """校验思考档，返回小写值；空值返回空串（表示"不写这一行"）。"""
    value = str(effort or "").strip().lower()
    if not value:
        return ""
    if value not in EFFORT_VALUES:
        raise DshError(f"reasoning_effort 不合法：{effort!r}（只允许 {' / '.join(EFFORT_VALUES)}）")
    return value


def resolve_effort(effort: str = None) -> str:
    """按单 reasoning_effort > 环境变量 MWP_DSH_EFFORT > 不指定（空串）。"""
    if effort is not None and str(effort).strip():
        return check_effort(effort)
    env_effort = os.environ.get("MWP_DSH_EFFORT")
    if env_effort and env_effort.strip():
        return check_effort(env_effort)
    return ""


def resolve_patch(model: str = None, template: str = None, effort: str = None) -> tuple:
    """返回 (patch 路径, 是否临时生成)。

    模型跟模板里的默认一致、又没指定思考档 -> 直接用仓库那份，不写临时文件；
    否则拿模板渲染一份，按"模型 + 思考档"缓存到 run/tmp_dsh/（同一组合复用一个文件，
    内容每次重写，不删除——有些环境里删文件会被安全软件/沙箱拦掉，删不动反而留垃圾）。
    """
    tmpl = template or os.environ.get("MWP_DSH_PATCH") or PATCH_PATH
    if not os.path.isfile(tmpl):
        raise DshError(f"NVIDIA patch 不存在：{tmpl}")
    target = resolve_model(model)
    target_effort = check_effort(effort)
    if target == DEFAULT_MODEL and not target_effort:
        return tmpl, False
    with open(tmpl, "r", encoding="utf-8") as fh:
        text = fh.read()
    new_text = text.replace(DEFAULT_MODEL, target)
    short = target.split("/")[-1]
    new_text = new_text.replace(f"name: {DEFAULT_NAME}", f"name: {short}")
    if target not in new_text:
        raise DshError(f"模板里没找到默认模型 {DEFAULT_MODEL}，无法按单替换")
    if target_effort:
        # 思考档挂在 agent-default-model 的 config 下（跟 provider/model 平级），
        # 只在这一段里的 `model:` 行后面插一行，不碰 llm-pi-ai 的 models 列表。
        anchor = f"    model: {target}\n"
        if anchor not in new_text:
            raise DshError(f"模板里没找到 agent-default-model 的 model 行，无法写入 reasoning_effort")
        new_text = new_text.replace(anchor, anchor + f"    reasoningEffort: {target_effort}\n", 1)
    os.makedirs(TMP_PATCH_ROOT, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", target)[:80]
    if target_effort:
        slug = f"{slug}-{target_effort}"
    path = os.path.join(TMP_PATCH_ROOT, f"patch-{slug}.yml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return path, True


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
    node_bin = os.path.join(user_dsh_home(), "profiles", "node_modules",
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


def artifact_path(task_id: str) -> str:
    """任务号 -> 产物文件路径；把不合规字符换成下划线，绝不让它跑出 run/artifacts。"""
    raw = str(task_id or "").strip()
    if not TASK_ID_RE.match(raw):
        raw = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:64] or "task"
    # 就算原样合规也再收一道：连续的点（含 ".."）一律换成下划线，开头不许是点，
    # 免得 Windows 上被判成上级目录或隐藏文件。
    raw = re.sub(r"\.{2,}", "_", raw).lstrip(".") or "task"
    return os.path.join(ARTIFACT_ROOT, f"{raw}.log")


def write_artifact(task_id: str, text: str) -> dict:
    """把这次执行的全文写进 run/artifacts，返回 {path, bytes, sha256}；写不了就返回错误说明。

    写失败不影响这一单的结论——产出已经在回执 logs 里了，磁盘只是留档；
    但要把失败原因带回去，别让上层以为"留档成功"。
    字节数与哈希按**落盘后的真实字节**算（Windows 会把换行写成 CRLF，按内存里的文本算就对不上）。
    """
    rel = os.path.join("run", "artifacts", os.path.basename(artifact_path(task_id)))
    full = artifact_path(task_id)
    try:
        os.makedirs(ARTIFACT_ROOT, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(full, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return {"artifact": None, "artifact_error": f"{type(exc).__name__}: {exc}"}
    return {
        "artifact": rel.replace(os.sep, "/"),
        "artifact_bytes": len(data),
        "artifact_sha256": hashlib.sha256(data).hexdigest()[:12],
        "artifact_error": "",
    }


def run(goal: str, timeout: float = 120.0, patch_path: str = None,
        profile: str = "headless", cwd: str = None, model: str = None,
        effort: str = None, task_id: str = None) -> dict:
    """跑一次 headless。返回 {exit_code, stdout, stderr, duration_ms, model, effort, key_used, artifact...}。"""
    key = read_nvidia_key()
    if not key:
        raise DshError("NVIDIA_API_KEY 没找到：~/.dsh/.credentials.yaml 的 refs 里没有，"
                       "或可用 MWP_DSH_CREDENTIALS 指到别处")
    target_model = resolve_model(model)
    target_effort = resolve_effort(effort)
    prune_cache()
    patch, _ = resolve_patch(target_model, template=patch_path, effort=target_effort)
    home = dsh_home()
    ensure_profile(profile)
    env = dict(os.environ)
    env["NVIDIA_API_KEY"] = key
    env["DSH_HOME"] = home
    # 有的插件会往系统临时目录里建文件；把临时目录也收进仓库，免得被"禁止写用户目录"的策略拦住。
    tmpdir = os.path.join(TMP_PATCH_ROOT, "temp")
    os.makedirs(tmpdir, exist_ok=True)
    for name in ("TMP", "TEMP", "TMPDIR"):
        env[name] = tmpdir
    started = time.time()
    cmd = build_command(goal, patch_path=patch, profile=profile)
    try:
        proc = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise DshError(f"dsh 超时（> {timeout:.0f}s）")
    except OSError as exc:
        raise DshError(f"dsh 起不来：{exc}")
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    duration_ms = int((time.time() - started) * 1000)
    result = {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "model": target_model,
        "effort": target_effort,
        "key_used": True,
    }
    # 留档：一次执行一个文件，任务号命名，人工和脚本都能直接读。
    head = [
        f"# task_id: {task_id or '-'}",
        f"# model: {target_model}",
        f"# reasoning_effort: {target_effort or '-'}",
        f"# exit_code: {proc.returncode}",
        f"# duration_ms: {duration_ms}",
        f"# patch: {os.path.relpath(patch, ROOT).replace(os.sep, '/')}",
        "",
        "=== stdout ===",
    ]
    body = "\n".join(head) + "\n" + stdout + "\n\n=== stderr ===\n" + stderr + "\n"
    result.update(write_artifact(task_id or "task-unknown", body))
    return result
