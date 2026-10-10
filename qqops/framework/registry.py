"""处理器注册表 —— 插件与框架注册的一切"钩子"都登记在这里。

对齐 AstrBot 的 `star_handler` 注册表: 用装饰器声明, 运行时收集, 按优先级排序,
支持按插件整体注销(热重载/停用的基础)。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

__all__ = ["Ref", "Registry", "REGISTRY", "KINDS"]

KINDS = ("stage", "event", "task", "command", "route", "page", "hook")


@dataclass
class Ref:
    """一个已注册的处理器。"""
    kind: str                      # stage/event/task/command/route/page/hook
    name: str                      # 事件名 / 阶段名 / 任务名 / 命令名 / 路由路径
    fn: object                     # 可调用对象
    plugin_id: str = ""            # 归属插件(""=框架内置)
    priority: int = 100            # 越小越先执行(阶段=order)
    meta: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.kind, self.name, self.plugin_id, getattr(self.fn, "__name__", "?"))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "plugin": self.plugin_id,
                "priority": self.priority,
                "handler": getattr(self.fn, "__name__", str(self.fn)),
                "meta": self.meta}


class Registry:
    """线程安全的处理器注册表。"""

    def __init__(self):
        self._refs = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 写
    def add(self, ref: Ref) -> Ref:
        with self._lock:
            k = ref.key()
            self._refs = [r for r in self._refs if r.key() != k]   # 幂等: 同名覆盖
            self._refs.append(ref)
            self._refs.sort(key=lambda r: (r.kind, r.priority, r.name))
        return ref

    def remove_plugin(self, plugin_id: str) -> int:
        with self._lock:
            n = len(self._refs)
            self._refs = [r for r in self._refs if r.plugin_id != plugin_id]
            return n - len(self._refs)

    def clear(self) -> None:
        with self._lock:
            self._refs = []

    # ------------------------------------------------------------ 读
    def handlers(self, kind: str, name: str = None) -> list:
        with self._lock:
            rows = [r for r in self._refs if r.kind == kind
                    and (name is None or r.name == name)]
        return sorted(rows, key=lambda r: (r.priority, r.name, r.plugin_id))

    def names(self, kind: str) -> list:
        with self._lock:
            return sorted({r.name for r in self._refs if r.kind == kind})

    def all(self) -> list:
        with self._lock:
            return list(self._refs)

    def by_plugin(self, plugin_id: str) -> list:
        with self._lock:
            return [r for r in self._refs if r.plugin_id == plugin_id]

    def plugins(self) -> list:
        with self._lock:
            return sorted({r.plugin_id for r in self._refs if r.plugin_id})

    def counts(self) -> dict:
        out = {k: 0 for k in KINDS}
        with self._lock:
            for r in self._refs:
                out[r.kind] = out.get(r.kind, 0) + 1
        out["total"] = sum(out.values())
        return out

    def summary(self) -> list:
        return [r.to_dict() for r in self.all()]


REGISTRY = Registry()
