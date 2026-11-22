"""产物留档的"本机视角"（Batch33-2）。

Worker 把每次 dsh 执行写到 `run/artifacts/<task_id>.log`，但那是**执行方那台机器**上的文件。
平台这边只做一件事：把本机能看到的产物做成一张索引表，挂到台账返回体上（`TaskView.artifact*`），
让看板显示「这单有产物、多大、在哪」——**不新增接口路径**（接口面仍是 11 条），
也不假装能读到别的机器上的内容。

索引按目录 mtime 缓存：目录没动就不重扫，避免每刷一次台板就把整个目录 stat 一遍。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ARTIFACT_DIR = os.path.join(ROOT, "run", "artifacts")

_cache = {"key": None, "data": {}}


def _dir_key():
    try:
        stat = os.stat(ARTIFACT_DIR)
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def scan() -> dict:
    """返回 {task_id: {"path": rel, "bytes": n}}；目录不存在返回空表。"""
    key = _dir_key()
    if key is None:
        _cache["key"] = None
        _cache["data"] = {}
        return {}
    if _cache["key"] == key:
        return _cache["data"]
    data = {}
    try:
        names = os.listdir(ARTIFACT_DIR)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".log"):
            continue
        task_id = name[:-4]
        if not task_id:
            continue
        try:
            data[task_id] = {
                "path": f"run/artifacts/{name}",
                "bytes": os.path.getsize(os.path.join(ARTIFACT_DIR, name)),
            }
        except OSError:
            continue
    _cache["key"] = key
    _cache["data"] = data
    return data


def lookup(task_id: str) -> dict:
    return scan().get(str(task_id or ""), {})


_SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def save(task_id: str, text: str) -> dict:
    """跨机产物落本机（Batch35）。

    执行方在别的机器上时，产物只存在于它自己的磁盘，平台这边 `scan()` 自然看不到。
    回执里带了全文，这里就按本机口径补一份 `run/artifacts/<task_id>.log`，
    于是产物签、抽屉、`include_artifact` 三处都不用改，接口面也不新增路径。

    三条自律：任务号不合规（含 `..`）一律不落盘；本机已有同名产物就不覆盖
    （同机执行时 Worker 自己写的那份才是原始字节）；写不了返回空字典，不抛。
    """
    tid = str(task_id or "").strip()
    if not _SAFE_ID.fullmatch(tid) or ".." in tid or tid.startswith("."):
        return {}
    if not isinstance(text, str) or not text.strip():
        return {}
    if lookup(tid):
        return lookup(tid)
    try:
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        full = os.path.join(ARTIFACT_DIR, f"{tid}.log")
        if os.path.exists(full):
            return lookup(tid)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        return {}
    _cache["key"] = None
    _cache["data"] = {}
    return lookup(tid)



def read_text(task_id: str, max_chars: int = 8000) -> str:
    """读本机产物全文（超长截断并标注）；读不到返回空串，绝不抛给调用方。

    只读 `scan()` 认得的 task_id，别的任务号一律当没有——不按拼接路径去试探。
    """
    info = lookup(task_id)
    if not info:
        return ""
    full = os.path.join(ARTIFACT_DIR, os.path.basename(info["path"]))
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…（已截断，全文 {len(text)} 字符，见 {info['path']}）\n"
    return text
