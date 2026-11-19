"""可注入时钟（Batch4）。

超时/心跳判定必须依赖可注入时钟，否则验收只能靠 sleep 赌时序。
生产用 SystemClock，验收用 FakeClock 手动推进。
"""
import threading
from datetime import datetime, timedelta, timezone


def ensure_utc(value: datetime):
    """SQLite 取回的 DateTime 常是 naive，统一按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock(Clock):
    """验收用：时间由测试显式推进，不依赖真实流逝。"""

    def __init__(self, start: datetime = None) -> None:
        self._now = start or datetime(2026, 11, 19, 8, 0, 0, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> datetime:
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds)
            return self._now

    def set(self, value: datetime) -> None:
        with self._lock:
            self._now = ensure_utc(value)


_clock: Clock = SystemClock()
_lock = threading.Lock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> None:
    global _clock
    with _lock:
        _clock = clock
