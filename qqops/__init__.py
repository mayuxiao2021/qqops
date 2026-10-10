"""QQOps —— 面向 Termux 的 QQ 群消息采集 / 总结 / 运维控制台。

纯标准库实现, 无需容器、无需 pip 依赖; NapCat 仍在 proot 中运行,
但与本程序共享 loopback, 因此可直接调用其 OneBot HTTP API。
"""

__version__ = "3.3.7"
__all__ = ["__version__"]
