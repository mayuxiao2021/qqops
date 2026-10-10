"""事件模型 —— 框架里流动的最小单位。

一次 QQ 消息、一次定时任务、一次服务状态变化, 都是 Event。
阶段与插件都围绕 Event 工作, 因此**任何**环节的异常都不会改变事件的形态,
只会在 `meta` 里留下痕迹, 便于控制台还原"这条消息经历了什么"。
"""
from __future__ import annotations

import itertools
import threading
import time

__all__ = ["Event", "new_id"]

_SEQ = itertools.count(1)
_LOCK = threading.Lock()


def new_id() -> str:
    with _LOCK:
        return "evt-%06d" % next(_SEQ)


class Event:
    """一个事件。字段刻意保持扁平常量, 便于日志与 JSON 序列化。"""

    __slots__ = ("kind", "payload", "source", "session", "ts", "id",
                 "dropped", "drop_reason", "results", "meta")

    def __init__(self, kind: str, payload: dict = None, source: str = "",
                 session: str = "", ts: float = None, id: str = None):
        self.kind = str(kind)
        self.payload = payload if payload is not None else {}
        self.source = str(source or "")
        self.session = str(session or "")
        self.ts = float(ts if ts is not None else time.time())
        self.id = id or new_id()
        self.dropped = False
        self.drop_reason = ""
        self.results = []
        self.meta = {"stages": []}          # 每阶段耗时/结果, 供可观测

    # ------------------------------------------------------------ 语义
    def drop(self, reason: str = "") -> "Event":
        """标记事件不再往下流(过滤掉/关键阶段失败)。"""
        self.dropped = True
        self.drop_reason = str(reason or "dropped")
        return self

    def add_result(self, value) -> None:
        self.results.append(value)

    def note(self, stage: str, **kv) -> None:
        row = {"stage": stage}
        row.update(kv)
        self.meta.setdefault("stages", []).append(row)

    # ------------------------------------------------------------ 构造快捷方式
    @classmethod
    def message(cls, one: dict, source: str = "onebot") -> "Event":
        """由 OneBot v11 上报体构造 `message.received` 事件。"""
        mtype = one.get("message_type") or "group"
        if mtype == "group":
            session = "group:%s" % one.get("group_id")
        else:
            session = "private:%s" % one.get("user_id")
        return cls("message.received", payload=one, source=source, session=session,
                   ts=float(one.get("time") or time.time()))

    # ------------------------------------------------------------ 输出
    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "source": self.source,
                "session": self.session, "ts": self.ts, "dropped": self.dropped,
                "drop_reason": self.drop_reason, "meta": self.meta,
                "payload_keys": sorted(self.payload.keys())[:20]}

    def __repr__(self) -> str:
        return ("<Event %s %s %s%s>" % (self.id, self.kind, self.session,
                                        " DROPPED" if self.dropped else ""))
