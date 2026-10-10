"""QQOps 框架层 —— 事件总线 / 阶段化管线 / 插件体系 / 容错内核 / 日志广播。

设计目标见 docs/ARCHITECTURE.md。这一层是"内核",不依赖 QQOps 的业务模块;
业务侧通过 `qqops.framework.api` 使用它(其余子模块不保证接口稳定)。
"""
from __future__ import annotations

__all__ = ["errors", "log"]
