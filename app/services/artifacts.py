"""产物留档的"本机视角"（Batch33-2）。

Worker 把每次 dsh 执行写到 `run/artifacts/<task_id>.log`，但那是**执行方那台机器**上的文件。
平台这边只做一件事：把本机能看到的产物做成一张索引表，挂到台账返回体上（`TaskView.artifact*`），
让看板显示「这单有产物、多大、在哪」——**不新增接口路径**（接口面仍是 11 条），
也不假装能读到别的机器上的内容。

索引按目录 mtime 缓存：目录没动就不重扫，避免每刷一次台板就把整个目录 stat 一遍。
"""
import os

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
