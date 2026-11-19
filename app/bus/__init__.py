"""进程内消息总线（Batch2）。

设计要点：
- 每个 topic（trace_id）一条有界队列，容量由配置注入；
- 队列满时抛 BusBackpressureError，绝不丢消息、绝不静默阻塞；
- publish 返回 Ticket，调用方可等待处理结果（带超时）；
- worker 线程共享消费所有 topic 队列，线程安全。
"""
import queue
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Callable, Dict, List, Optional

from app.core.logging import get_logger
from app.schemas import Envelope

logger = get_logger(__name__)


class BusBackpressureError(Exception):
    """队列已满。明确拒绝，不丢消息、不假装成功。"""


class BusTimeoutError(Exception):
    """等待处理结果超时（总线可能在处理中或未启动）。"""


class Ticket:
    """一次投递的回执，用于取回处理结果。"""

    __slots__ = ("_future",)

    def __init__(self) -> None:
        self._future: Future = Future()

    def set_result(self, value) -> None:
        if not self._future.done():
            self._future.set_result(value)

    def set_error(self, exc: BaseException) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    @property
    def done(self) -> bool:
        return self._future.done()

    def result(self, timeout: Optional[float] = None):
        try:
            return self._future.result(timeout=timeout)
        except FutureTimeoutError:
            raise BusTimeoutError("等待报文处理结果超时")


class MessageBus:
    """进程内总线：有界队列 + 多 worker 消费。"""

    def __init__(
        self,
        capacity: int = 1000,
        workers: int = 2,
        consumer: Optional[Callable[[Envelope], object]] = None,
        result_timeout: float = 5.0,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须大于 0")
        self.capacity = capacity
        self.workers = max(1, workers)
        self.result_timeout = result_timeout
        self._consumer = consumer
        self._queues: Dict[str, "queue.Queue"] = {}
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._running = False
        self.leftover_on_stop = 0

    # ---- 装配 ----
    def set_consumer(self, consumer: Callable[[Envelope], object]) -> None:
        self._consumer = consumer

    # ---- 队列 ----
    def _queue_for(self, topic: str) -> "queue.Queue":
        with self._lock:
            q = self._queues.get(topic)
            if q is None:
                q = queue.Queue(maxsize=self.capacity)
                self._queues[topic] = q
            return q

    def get_queue(self, topic: str) -> "queue.Queue":
        return self._queue_for(topic)

    def pending(self, topic: str) -> int:
        return self._queue_for(topic).qsize()

    def total_pending(self) -> int:
        with self._lock:
            queues = list(self._queues.values())
        return sum(q.qsize() for q in queues)

    # ---- 投递 ----
    def publish(self, topic: str, envelope: Envelope) -> Ticket:
        if not topic:
            raise ValueError("topic 不能为空")
        q = self._queue_for(topic)
        ticket = Ticket()
        try:
            q.put_nowait((envelope, ticket))
        except queue.Full:
            raise BusBackpressureError(
                f"队列已满（容量 {self.capacity}），报文明文未入队、未丢失"
            )
        return ticket

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._running:
            return
        if self._consumer is None:
            raise RuntimeError("总线未设置 consumer，不能启动")
        self._running = True
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, name=f"bus-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("总线已启动：capacity=%s workers=%s", self.capacity, self.workers)

    def stop(self, join_timeout: float = 3.0) -> int:
        self._running = False
        for t in self._threads:
            t.join(timeout=join_timeout)
        self._threads = []
        self.leftover_on_stop = self.total_pending()
        if self.leftover_on_stop:
            logger.warning("总线停止时仍有 %s 条报文未消费（已保留在队列中）", self.leftover_on_stop)
        else:
            logger.info("总线已停止，无残留报文")
        return self.leftover_on_stop

    def _loop(self) -> None:
        while self._running:
            handled = False
            with self._lock:
                queues = list(self._queues.values())
            for q in queues:
                try:
                    envelope, ticket = q.get_nowait()
                except queue.Empty:
                    continue
                handled = True
                try:
                    ticket.set_result(self._consumer(envelope))
                except BaseException as exc:  # noqa: BLE001 - 必须回传给调用方
                    logger.exception("总线消费失败：trace_id=%s", envelope.trace_id)
                    ticket.set_error(exc)
            if not handled:
                time.sleep(0.02)


__all__ = ["MessageBus", "Ticket", "BusBackpressureError", "BusTimeoutError"]
