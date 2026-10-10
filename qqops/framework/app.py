"""应用编排 —— 一个服务进程的"总装线"(对齐 AstrBot 的 `AstrBotCoreLifecycle`)。

QQOps 是"多进程 + 单进程都能跑"的形态, 因此 `QQOpsApp` 只负责装配, 不负责 fork:

    app = QQOpsApp(roles=("ingest",)).setup().start()
    ...
    app.publish(Event("message.received", onebot_body))
    ...
    app.stop()

`roles` 决定这个进程注册哪些插件处理器:
    ingest   采集进程: 阶段 + 事件处理器(收消息时做富化/提醒扫描)
    tasks    调度进程: 定时任务(总结/备份/提醒到点)
    console  控制台进程: 路由 + 页面
    all      单进程全跑(开发/小内存设备)
"""
from __future__ import annotations

import os
import time

from . import errors
from .bus import EventBus
from .context import Context
from .event import Event
from .manager import PluginManager
from .pipeline import Pipeline
from .registry import REGISTRY
from .web import ROUTER

__all__ = ["QQOpsApp", "VERSION"]

VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "VERSION")


def _version() -> str:
    try:
        from .. import __version__
        return __version__
    except Exception:
        try:
            with open(VERSION_FILE, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return "0.0.0"


VERSION = _version()


class QQOpsApp:
    """装配总线 / 管线 / 插件 / 上下文, 并把"装配失败"限制在可控范围内。"""

    def __init__(self, name: str = "qqops", roles=("all",), cfg=None, store=None,
                 adapter=None, plugin_dirs=None, workers: int = 1, queue_size: int = 2000):
        self.name = name
        self.roles = tuple(roles or ("all",))
        self._cfg, self._store, self.adapter = cfg, store, adapter
        self.registry = REGISTRY
        self.router = ROUTER
        self.bus = None
        self.pipeline = None
        self.plugins = None
        self.ctx = None
        self.ready = False
        self.error = ""
        self.started_at = 0.0
        self._plugin_dirs = plugin_dirs
        self._workers = workers
        self._queue_size = queue_size

    # ---------------------------------------------------------------- 装配
    def setup(self) -> "QQOpsApp":
        try:
            from .. import core
            core.ensure_dirs()
            core.log("app", "INFO", "%s 装配中(角色: %s)" % (self.name, ",".join(self.roles)))
        except Exception as e:
            errors.record("app.setup", exc=e, kind="setup_warn")
        # 1) 全局异常钩子 —— 漏网的异常也要进账本, 不能静默消失
        errors.install_global_hooks(component="%s.process" % self.name)
        # 2) 配置/存储(懒加载, 出错也要能起来: 服务本身要能报错)
        cfg = self._cfg
        store = self._store
        # 3) 管线 + 总线
        self.pipeline = Pipeline(registry=self.registry, cfg_getter=self._cfg_dict)
        self.bus = EventBus(registry=self.registry, pipeline=self.pipeline,
                            workers=self._workers, queue_size=self._queue_size,
                            cfg_getter=self._cfg_dict)
        # 4) 上下文
        self.ctx = Context(app=self, cfg=cfg, store=store, state=None, bus=self.bus,
                           pipeline=self.pipeline, adapter=self.adapter,
                           role=self.roles[0] if len(self.roles) == 1 else "all")
        self.bus.ctx = self.ctx
        # 5) 插件
        self.plugins = PluginManager(ctx=self.ctx, dirs=self._plugin_dirs,
                                     roles=self.roles, registry=self.registry,
                                     router=self.router)
        self.ctx.plugins = self.plugins
        try:
            loaded = self.plugins.load_all()
            bad = {k: v for k, v in loaded.items() if v == "failed"}
            if bad:
                self.ctx.log("app", "WARN", "有 %d 个插件加载失败: %s"
                             % (len(bad), ", ".join(bad)))
        except Exception as e:                            # noqa: BLE001 —— 插件全挂也要能起服务
            errors.record("app.plugins", exc=e, kind="plugin_setup_failed")
            self.ctx.log("app", "ERROR", "插件系统装配失败(服务继续运行): %s" % e)
        self.ready = True
        return self

    def _cfg_dict(self) -> dict:
        try:
            if self._cfg is not None:
                return self._cfg.data
            from .. import core
            return core.Config().data
        except Exception:
            return {}

    # ---------------------------------------------------------------- 运行
    def start(self) -> "QQOpsApp":
        if not self.ready:
            self.setup()
        self.started_at = time.time()
        try:
            self.bus.start()
        except Exception as e:                            # noqa: BLE001
            errors.record("app.bus", exc=e, kind="bus_start_failed")
        self.publish(Event("app.started", {"name": self.name, "roles": list(self.roles),
                                           "version": VERSION, "pid": os.getpid()},
                           source="app"))
        # 启动即跑的任务
        if self.plugins:
            for ref in self.registry.handlers("task"):
                if (ref.meta or {}).get("run_on_start") and \
                        not self.plugins._state.get(ref.plugin_id, {}).get("task_%s" % ref.name):
                    res = self.plugins.run_task(ref, self.ctx)
                    self.ctx.log("app", "INFO", "启动任务 %s: %s"
                                 % (ref.name, res.get("result") if res.get("ok") else res.get("error")))
        return self

    def stop(self) -> None:
        try:
            self.publish(Event("app.stopping", {"name": self.name}, source="app"))
        except Exception:
            pass
        if self.bus:
            self.bus.stop(timeout=2.0)
        for pid in list((self.plugins.plugins if self.plugins else {}).keys()):
            try:
                self.plugins.unload(pid)
            except Exception:
                pass

    # ---------------------------------------------------------------- 事件
    def publish(self, evt, wait: bool = True) -> bool:
        if not self.bus:
            return False
        return self.bus.publish(evt, wait=wait)

    def emit(self, kind: str, payload: dict = None, session: str = "", source: str = "") -> Event:
        evt = Event(kind, payload=payload or {}, session=session, source=source or self.name)
        self.publish(evt)
        return evt

    def dispatch_now(self, evt) -> Event:
        return self.bus.dispatch(evt)

    # ---------------------------------------------------------------- 定时任务(给调度服务用)
    def run_due_tasks(self, limit: int = 5) -> list:
        out = []
        if not self.plugins:
            return out
        try:
            for ref in self.plugins.due_tasks()[:max(1, int(limit))]:
                res = self.plugins.run_task(ref, self.ctx)
                out.append(res)
                if not res.get("ok"):
                    self.ctx.log("app", "WARN", "任务 %s 失败: %s" % (res.get("task"), res.get("error")))
        except Exception as e:                            # noqa: BLE001
            errors.record("app.tasks", exc=e, kind="task_loop_error")
        return out

    # ---------------------------------------------------------------- 观测
    def status(self) -> dict:
        st = {
            "name": self.name, "version": VERSION, "ready": self.ready,
            "error": self.error, "pid": os.getpid(),
            "roles": list(self.roles),
            "started_at": self.started_at,
            "uptime": max(0.0, time.time() - self.started_at) if self.started_at else 0.0,
            "plugins": self.plugins.state() if self.plugins else {},
            "handlers": self.registry.counts(),
            "routes": len(self.router.routes()),
            "bus": self.bus.state() if self.bus else {},
            "crashes": errors.stats(),
        }
        try:
            from .log import BROKER
            st["log"] = BROKER.state()
        except Exception:
            pass
        return st
