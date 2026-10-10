"""框架对插件开放的**唯一稳定面**(对齐 AstrBot 的 `astrbot/api`)。

插件只应该 `from qqops.framework.api import ...`;其它 `qqops.*` 内部模块不保证兼容。
"""
from __future__ import annotations

from .context import Context
from .errors import CrashLedger, guard, record, retry, safe_call
from .event import Event
from .plugin import (Plugin, PluginMeta, command, on_event, on_stage, page,
                     register, route, task)
from .registry import REGISTRY, Ref
from .schema import defaults, validate
from .web import ROUTER, Request, Response

__all__ = [
    # 插件声明
    "Plugin", "PluginMeta", "register", "on_event", "on_stage", "task",
    "command", "route", "page",
    # 运行时
    "Context", "Event", "Request", "Response",
    # 注册表(只读用)
    "REGISTRY", "ROUTER", "Ref",
    # 容错
    "guard", "record", "retry", "safe_call", "CrashLedger",
    # 配置
    "defaults", "validate",
]
