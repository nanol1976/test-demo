"""停止标记模块（Batch29）：优雅停止的唯一实现。

- 全局只有一个停止标记文件，worker 轮询识别后不再领新活、等在飞任务收尾退出。
- 路径可通过环境变量 MWP_STOP_FILE 覆盖（验收脚本在临时目录跑，绝不写真实 data/）。
- 写入用 os.replace 原子落盘；worker 只读不写，清标记由 master:resume 做。
"""

import os
from datetime import datetime, timezone

STOP_FILE_NAME = "STOP"


def stop_file_path() -> str:
    """返回停止标记文件的绝对路径。

    优先读环境变量 MWP_STOP_FILE；没有则返回 data/STOP（相对当前工作目录）。
    """
    env_path = os.environ.get("MWP_STOP_FILE")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.abspath(os.path.join("data", STOP_FILE_NAME))


def request_stop(reason: str = "") -> str:
    """写入停止标记文件，返回写入的路径。

    内容：ISO 时间 + 空格 + reason（可为空）。
    用 os.replace 原子落盘，确保并发安全。
    """
    path = stop_file_path()
    # 确保父目录存在
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # 生成内容：ISO 时间 + 空格 + reason
    iso_time = datetime.now(timezone.utc).isoformat()
    content = f"{iso_time} {reason}\n" if reason else f"{iso_time}\n"

    # 原子写入：写临时文件再 replace
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, path)
    return path


def clear_stop() -> bool:
    """删除停止标记文件。

    删掉返回 True，本来不存在返回 False（幂等）。
    """
    path = stop_file_path()
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def is_stopped() -> bool:
    """检查停止标记文件是否存在。"""
    return os.path.exists(stop_file_path())
