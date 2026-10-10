"""插件基类与声明式装饰器(对齐 AstrBot 的 `@register` + `Star` + `filter`)。

一个插件就是一个目录:

    plugins/<id>/
      metadata.json       # 元信息(可选, 也可以只写 @register)
      _conf_schema.json   # 配置 schema → 控制台表单 + 校验
      __init__.py         # 导出 Plugin 子类

写法:

    from qqops.framework.api import Plugin, register, on_event, on_stage, task, route, page

    @register(id="reminders", name="截止提醒", version="1.0.0", desc="...")
    class Reminders(Plugin):
        def initialize(self, ctx):
            self.seen = 0

        @on_stage("reminders_scan", order=35)
        def scan(self, event, ctx):
            ...

        @task("reminders_tick", every=60)
        def tick(self, ctx):
            ...

装饰器只给函数打标记, 真正注册发生在 `PluginManager` 加载插件时 ——
这样插件类可以随便导入、随便测试, 不会因为导入就产生副作用。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = ["Plugin", "PluginMeta", "register", "on_event", "on_stage", "task",
           "command", "route", "page"]

_MARK = "_qqops_handlers"


def _mark(fn, kind: str, name: str, priority: int = 100, **meta):
    rows = getattr(fn, _MARK, None)
    if rows is None:
        rows = []
        try:
            setattr(fn, _MARK, rows)
        except Exception:
            return fn
    rows.append({"kind": kind, "name": name, "priority": int(priority), **meta})
    return fn


# ------------------------------------------------------------------ 装饰器
def register(id: str = None, name: str = None, version: str = "1.0.0", author: str = "",
             desc: str = "", repo: str = "", priority: int = 100,
             enabled_by_default: bool = True, depends: tuple = (), **extra):
    """类装饰器: 声明插件元信息(其余元信息也可写在 metadata.json 里)。"""
    def deco(cls):
        meta = {"id": id or cls.__name__.lower(), "name": name or (id or cls.__name__),
                "version": version, "author": author, "desc": desc, "repo": repo,
                "priority": int(priority), "enabled_by_default": bool(enabled_by_default),
                "depends": list(depends)}
        meta.update(extra)
        try:
            setattr(cls, "_qqops_meta", meta)
        except Exception:
            pass
        return cls
    return deco


def on_event(kind: str = "*", priority: int = 100, role: str = "ingest"):
    """订阅事件(`kind="*"` 表示全部)。"""
    return lambda fn: _mark(fn, "event", kind, priority, role=role)


def on_stage(name: str, order: int = 100, critical: bool = False,
             role: str = "ingest", on: str = None):
    """在管线上插一个阶段。`order` 越小越先跑; `critical=True` 时失败会丢弃事件。"""
    return lambda fn: _mark(fn, "stage", name, order, role=role, critical=bool(critical),
                            on=on)


def task(name: str, every: int = None, cron: str = None, role: str = "tasks",
         run_on_start: bool = False, enabled: bool = True):
    """注册定时任务: `every` 秒一次, 或 `cron="分 时 日 月 周"`。"""
    return lambda fn: _mark(fn, "task", name, 100, role=role, every=every, cron=cron,
                            run_on_start=bool(run_on_start), enabled=bool(enabled))


def command(name: str, help: str = "", role: str = "console"):
    """注册一个具名命令(控制台/CLI 可调用)。"""
    return lambda fn: _mark(fn, "command", name, 100, role=role, help=help)


def route(path: str, methods: tuple = ("GET",), auth: bool = True, role: str = "console"):
    """注册控制台 API 路由。处理器签名 `(req, ctx) -> dict|Response|str`。"""
    return lambda fn: _mark(fn, "route", path, 100, role=role,
                            methods=[m.upper() for m in methods], auth=bool(auth))


def page(meta: dict):
    """往控制台加一个页面。meta: {id, title, icon, order, api}"""
    return lambda fn: _mark(fn, "page", (meta or {}).get("id") or "page", 100,
                            role="console", page=dict(meta or {}))


# ------------------------------------------------------------------ 元信息
@dataclass
class PluginMeta:
    id: str
    name: str = ""
    version: str = "0.0.0"
    author: str = ""
    desc: str = ""
    repo: str = ""
    priority: int = 100
    enabled_by_default: bool = True
    depends: list = field(default_factory=list)
    dir: str = ""
    builtin: bool = False
    # 运行态
    status: str = "discovered"       # discovered/loaded/failed/disabled
    error: str = ""
    loaded_at: float = 0.0
    handlers: int = 0
    pages: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name or self.id, "version": self.version,
                "author": self.author, "desc": self.desc, "repo": self.repo,
                "priority": self.priority, "builtin": self.builtin, "dir": self.dir,
                "status": self.status, "error": self.error,
                "loaded_at": self.loaded_at, "handlers": self.handlers,
                "pages": self.pages, "depends": self.depends}


# ------------------------------------------------------------------ 基类
class Plugin:
    """插件基类: 只需要实现自己关心的方法。"""

    meta: PluginMeta = None
    ctx = None

    # ------------------------------------------------------------ 生命周期
    def initialize(self, ctx) -> None:
        """插件启用时调用(注册资源/建表/预热)。异常会被管理器记账, 不影响其它插件。"""

    def terminate(self) -> None:
        """插件停用/重载前调用(关闭线程、落盘状态)。"""

    # ------------------------------------------------------------ 便捷属性
    @property
    def id(self) -> str:
        return self.meta.id if self.meta else self.__class__.__name__.lower()

    @property
    def cfg(self) -> dict:
        """本插件的配置(schema 默认值 + 用户值, 已校验)。"""
        if self.ctx is None:
            return {}
        return self.ctx.plugin_cfg(self.id)

    @property
    def data_dir(self) -> str:
        return self.ctx.plugin_data_dir(self.id) if self.ctx else ""

    def log(self, level: str, msg: str) -> None:
        if self.ctx:
            self.ctx.log("plugin.%s" % self.id, level, msg)

    def info(self, msg: str) -> None:
        self.log("INFO", msg)

    def warn(self, msg: str) -> None:
        self.log("WARN", msg)

    def error(self, msg: str) -> None:
        self.log("ERROR", msg)

    # ------------------------------------------------------------ 状态
    def state_file(self) -> str:
        import os
        return os.path.join(self.data_dir, "state.json") if self.data_dir else ""

    def load_state(self) -> dict:
        import json
        path = self.state_file()
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def save_state(self, data: dict) -> None:
        import json
        import os
        path = self.state_file()
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:
            if self.ctx:
                self.ctx.record("plugin.%s" % self.id, exc=e, kind="state_save")


def now() -> float:
    return time.time()
