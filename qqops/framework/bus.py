"""事件总线 —— 把事件从"产生方"送到"管线与插件", 且任何一环出错都不影响其它环。

对齐 AstrBot 的 `core/event_bus.py`, 但 QQOps 不用 asyncio: 用一个守护工作线程 +
有界队列(手机内存有限, 队列满了宁可记账丢弃也不无限堆积)。

语义:
    publish(evt)   入队, 立刻返回(采集器的快路径不能阻塞在插件上)
    dispatch(evt)  同步跑一遍: 管线 → 事件处理器; 每个处理器单独 guard
"""
from __future__ import annotations

import queue
import threading
import time

from . import errors
from .pipeline import Pipeline
from .registry import REGISTRY

__all__ = ["EventBus"]

_STOP = object()


class EventBus:
    def __init__(self, registry=None, pipeline: Pipeline = None, workers: int = 1,
                 queue_size: int = 2000, cfg_getter=None):
        self.registry = registry or REGISTRY
        self.pipeline = pipeline or Pipeline(registry=self.registry, cfg_getter=cfg_getter)
        self.queue = queue.Queue(maxsize=max(16, int(queue_size)))
        self.workers = max(1, int(workers))
        self._threads = []
        self._running = False
        self._lock = threading.RLock()
        self.stats = {"published": 0, "dispatched": 0, "dropped_queue_full": 0,
                      "handler_errors": 0, "last_ts": 0.0, "by_kind": {}}

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> "EventBus":
        with self._lock:
            if self._running:
                return self
            self._running = True
            for i in range(self.workers):
                t = threading.Thread(target=self._loop, name="qqops-bus-%d" % i, daemon=True)
                t.start()
                self._threads.append(t)
            return self

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            self._running = False
        for _ in self._threads:
            try:
                self.queue.put_nowait(_STOP)
            except Exception:
                pass
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    @property
    def running(self) -> bool:
        return self._running

    # ---------------------------------------------------------------- 入队
    def publish(self, evt, wait: bool = True, timeout: float = 2.0) -> bool:
        """把事件交给总线。队列满时记账并返回 False(绝不阻塞采集快路径)。"""
        with self._lock:
            self.stats["published"] += 1
            self.stats["by_kind"][evt.kind] = self.stats["by_kind"].get(evt.kind, 0) + 1
        try:
            if wait:
                self.queue.put(evt, timeout=timeout)
            else:
                self.queue.put_nowait(evt)
            return True
        except queue.Full:
            self.stats["dropped_queue_full"] += 1
            errors.record("bus.queue", kind="queue_full",
                          message="事件队列已满, 丢弃 %s(%s)" % (evt.kind, evt.session))
            return False
        except Exception as e:                          # noqa: BLE001
            errors.record("bus.publish", exc=e, extra={"kind": evt.kind})
            return False

    # ---------------------------------------------------------------- 工作线程
    def _loop(self) -> None:
        while True:
            try:
                item = self.queue.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    if not self._running:
                        return
                continue
            if item is _STOP:
                return
            try:
                self.dispatch(item)
            except Exception as e:                      # noqa: BLE001 —— 兜底, 线程不能死
                errors.record("bus.worker", exc=e, kind="worker_error")
            finally:
                try:
                    self.queue.task_done()
                except Exception:
                    pass

    # ---------------------------------------------------------------- 分发
    def dispatch(self, evt):
        """同步执行: 管线阶段 → 事件处理器。每个处理器独立隔离。"""
        t0 = time.time()
        with self._lock:
            self.stats["dispatched"] += 1
            self.stats["last_ts"] = t0
        try:
            self.pipeline.run(evt, ctx=getattr(self, "ctx", None))
        except Exception as e:                          # noqa: BLE001 —— 管线自身兜底
            errors.record("bus.pipeline", exc=e, extra={"kind": evt.kind})
        if not evt.dropped:
            for ref in self.registry.handlers("event", evt.kind) + \
                    self.registry.handlers("event", "*"):
                if not self._enabled(ref):
                    continue
                try:
                    out = ref.fn(evt, getattr(self, "ctx", None))
                    if out is not None:
                        evt.add_result(out)
                except Exception as e:                  # noqa: BLE001 —— 一个插件崩, 不影响其它
                    self.stats["handler_errors"] += 1
                    errors.record("plugin.%s" % (ref.plugin_id or "core"), exc=e,
                                  kind="handler_error",
                                  extra={"event": evt.kind, "handler":
                                         getattr(ref.fn, "__name__", "?")})
        evt.note("bus.total", ms=round((time.time() - t0) * 1000, 1))
        return evt

    def _enabled(self, ref) -> bool:
        if not self.pipeline.cfg_getter:
            return True
        try:
            cfg = self.pipeline.cfg_getter() or {}
            plug = (cfg.get("plugins") or {}).get(ref.plugin_id) or {}
            if isinstance(plug, dict) and plug.get("enabled") is False:
                return False
        except Exception:
            pass
        return True

    # ---------------------------------------------------------------- 观测
    def state(self) -> dict:
        with self._lock:
            st = dict(self.stats)
            st["by_kind"] = dict(st.get("by_kind") or {})
        st["queue"] = {"size": self.queue.qsize(), "max": self.queue.maxsize}
        st["running"] = self.running
        st["workers"] = self.workers
        st["stages"] = self.pipeline.describe()
        return st
