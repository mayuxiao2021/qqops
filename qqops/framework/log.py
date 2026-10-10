"""日志广播 —— 让控制台能看到"正在发生什么"。

背景: QQOps 原来只往 `data/logs/<模块>.log` 写文件, 控制台只能读文件尾部,
既慢又拿不到实时性。这里加一层内存环形缓冲 + 游标拉取(≈ AstrBot 的 `LogBroker`):

    python:  core.log("collector", "ERROR", "...")   # 原有写法不变
    http:    GET /api/ops/logs?since=<seq>&module=collector&level=WARN
    js:      定时带上次 seq 拉增量 → 真正的实时日志流

只依赖标准库, 线程安全; 前端用游标拉增量, 不用 WebSocket, 手机上更省电。
"""
from __future__ import annotations

import collections
import threading
import time

__all__ = ["LogBroker", "BROKER", "publish", "since", "tail", "modules", "clear", "levels"]

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
_RANK = {l: i for i, l in enumerate(LEVELS)}
# 兼容常见别名, 免得上游写法不同就丢日志
_ALIAS = {"WARNING": "WARN", "ERR": "ERROR", "CRITICAL": "ERROR", "FATAL": "ERROR",
          "TRACE": "DEBUG", "NOTICE": "INFO"}


def norm_level(level: str) -> str:
    lv = str(level or "INFO").upper()
    lv = _ALIAS.get(lv, lv)
    return lv if lv in _RANK else "INFO"


class LogBroker:
    """有界环形日志缓冲, 支持按序号游标拉取增量。"""

    def __init__(self, size: int = 2000):
        self.size = int(size)
        self._buf = collections.deque(maxlen=self.size)
        self._lock = threading.RLock()
        self._seq = 0
        self._counts = collections.Counter()

    # ---------------------------------------------------------------- 写
    def publish(self, module: str, level: str, message: str) -> dict:
        lv = norm_level(level)
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": time.time(),
                "time": time.strftime("%m-%d %H:%M:%S"),
                "module": str(module or "qqops")[:40],
                "level": lv,
                "msg": str(message or "")[:1000],
            }
            self._buf.append(entry)
            self._counts[lv] += 1
        return entry

    # ---------------------------------------------------------------- 读
    def since(self, seq: int = 0, module: str = "", level: str = "", limit: int = 500) -> dict:
        """拉取 seq 之后的日志。返回 {entries, next, dropped}。"""
        seq = int(seq or 0)
        min_rank = _RANK.get(norm_level(level), 0) if level else 0
        with self._lock:
            buf = list(self._buf)
            first = buf[0]["seq"] if buf else self._seq + 1
            out = [e for e in buf
                   if e["seq"] > seq
                   and (not module or e["module"] == module)
                   and _RANK.get(e["level"], 1) >= min_rank]
        dropped = max(0, first - max(0, seq) - 1) if first > max(0, seq) + 1 else 0
        rows = out[:max(1, int(limit))]
        return {"entries": rows, "next": rows[-1]["seq"] if rows else seq, "dropped": dropped}

    def tail(self, n: int = 100, module: str = "", level: str = "") -> list:
        min_rank = _RANK.get(norm_level(level), 0) if level else 0
        with self._lock:
            rows = [e for e in self._buf
                    if (not module or e["module"] == module)
                    and _RANK.get(e["level"], 1) >= min_rank]
        return rows[-max(1, int(n)):]

    def modules(self) -> list:
        with self._lock:
            mods = {}
            for e in self._buf:
                mods[e["module"]] = mods.get(e["module"], 0) + 1
        return [{"module": k, "count": v} for k, v in sorted(mods.items(), key=lambda kv: -kv[1])]

    def counts(self) -> dict:
        with self._lock:
            return dict(self._counts)

    def clear(self) -> int:
        with self._lock:
            n = len(self._buf)
            self._buf.clear()
            self._counts.clear()
            return n

    def state(self) -> dict:
        with self._lock:
            return {"seq": self._seq, "size": len(self._buf), "cap": self.size,
                    "counts": dict(self._counts)}


BROKER = LogBroker()


def publish(module: str, level: str, message: str) -> dict:
    return BROKER.publish(module, level, message)


def since(seq: int = 0, module: str = "", level: str = "", limit: int = 500) -> dict:
    return BROKER.since(seq, module, level, limit)


def tail(n: int = 100, module: str = "", level: str = "") -> list:
    return BROKER.tail(n, module, level)


def modules() -> list:
    return BROKER.modules()


def clear() -> int:
    return BROKER.clear()
