"""熔断登记表（Batch4）。

同一 trace 连续失败任务数达阈值 → 熔断，暂停接收该 trace 的新派发。
状态放内存（进程内），重启即复位；持久化的 pause_flag 在台账里。
"""
import threading
from typing import Dict, Set


class BreakerRegistry:
    def __init__(self) -> None:
        self._open: Dict[str, str] = {}
        self._lock = threading.Lock()

    def trip(self, trace_id: str, reason: str) -> bool:
        """熔断一条 trace，已熔断则返回 False（不重复记）。"""
        with self._lock:
            if trace_id in self._open:
                return False
            self._open[trace_id] = reason
            return True

    def is_open(self, trace_id: str) -> bool:
        with self._lock:
            return trace_id in self._open

    def reason(self, trace_id: str) -> str:
        with self._lock:
            return self._open.get(trace_id, "")

    def reset(self, trace_id: str) -> bool:
        with self._lock:
            return self._open.pop(trace_id, None) is not None

    def open_traces(self) -> Set[str]:
        with self._lock:
            return set(self._open)

    def clear(self) -> None:
        with self._lock:
            self._open.clear()


breaker = BreakerRegistry()
