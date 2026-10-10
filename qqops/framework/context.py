"""插件上下文 —— 插件能拿到的一切资源都从这里取(对齐 AstrBot 的 `Context`/`StarTools`)。

好处: 插件不需要 `import qqops.core` 这种内部路径, 依赖注入让测试可以替换实现,
将来把 core.py 拆成 core/ 包时插件也不用改一行。

按角色提供不同能力:
  * `ingest`  进程(采集器): cfg/store/state/log/bus/http/napcat
  * `tasks`   进程(调度器): 上述 + tasks
  * `console` 进程(控制台): 上述 + router/pages/plugin 管理
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import errors
from .log import BROKER
from .registry import REGISTRY
from .web import ROUTER

__all__ = ["Context"]


class Context:
    """服务定位器: 插件通过它访问框架与业务能力。"""

    def __init__(self, app=None, cfg=None, store=None, state=None, bus=None,
                 pipeline=None, plugins=None, adapter=None, role: str = "all"):
        self.app = app
        self._cfg = cfg
        self._store = store
        self._state = state
        self.bus = bus
        self.pipeline = pipeline
        self.plugins = plugins
        self.adapter = adapter                     # 消息平台适配器(OneBot/NapCat)
        self.role = role
        self.registry = REGISTRY
        self.router = ROUTER
        self._cfg_cache = {}

    # ---------------------------------------------------------------- 基础资源
    @property
    def cfg(self):
        if self._cfg is None:
            from .. import core
            self._cfg = core.Config()
        return self._cfg

    @property
    def data(self) -> dict:
        try:
            return self.cfg.data
        except Exception:
            return {}

    @property
    def store(self):
        if self._store is None:
            from .. import core
            self._store = core.Store()
        return self._store

    @property
    def state(self):
        if self._state is None:
            from .. import core
            self._state = core
        return self._state

    def state_get(self, key: str, default=None):
        try:
            return self.state.state_get(key, default)
        except Exception:
            return default

    def state_set(self, key: str, value) -> None:
        try:
            self.state.state_set(key, value)
        except Exception as e:
            errors.record("context.state", exc=e)

    # ---------------------------------------------------------------- 日志/异常
    def log(self, module: str, level: str, msg: str) -> None:
        try:
            from .. import core
            core.log(module, level, msg)
        except Exception:
            BROKER.publish(module, level, msg)

    def record(self, component: str, exc=None, kind: str = "error",
               message: str = "", extra: dict = None) -> dict:
        return errors.record(component, exc=exc, kind=kind, message=message, extra=extra)

    def crashes(self, limit: int = 50) -> list:
        return errors.recent(limit)

    # ---------------------------------------------------------------- 事件
    def emit(self, kind: str, payload: dict = None, session: str = "", source: str = "plugin"):
        from .event import Event
        evt = Event(kind, payload=payload or {}, source=source, session=session)
        if self.bus:
            self.bus.publish(evt)
        else:
            self.record("context.emit", kind="no_bus",
                        message="总线未启动, 事件 %s 被丢弃" % kind)
        return evt

    # ---------------------------------------------------------------- 插件配置
    def plugin_cfg(self, plugin_id: str) -> dict:
        """插件自己的配置(带缓存; 保存后由管理器清缓存)。"""
        if plugin_id in self._cfg_cache:
            return self._cfg_cache[plugin_id]
        if self.plugins:
            cfg = self.plugins.config(plugin_id)
        else:
            cfg = {}
        self._cfg_cache[plugin_id] = cfg
        return cfg

    def invalidate_plugin_cfg(self, plugin_id: str = None) -> None:
        if plugin_id is None:
            self._cfg_cache.clear()
        else:
            self._cfg_cache.pop(plugin_id, None)

    def plugin_data_dir(self, plugin_id: str) -> str:
        from .errors import data_dir
        path = os.path.join(data_dir(), "plugin_data", plugin_id)
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path

    # ---------------------------------------------------------------- 出站 HTTP
    def http_get(self, url: str, headers: dict = None, timeout: float = 8.0) -> bytes:
        """带超时与记账的出站 GET(插件不要自己裸用 urllib)。"""
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "QQOps"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            errors.record("context.http", exc=e, kind="http_error",
                          extra={"url": url[:200]})
            raise

    def http_json(self, url: str, headers: dict = None, timeout: float = 8.0, data=None):
        body = None
        hdr = dict(headers or {})
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            hdr.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except Exception as e:
            errors.record("context.http", exc=e, kind="http_error",
                          extra={"url": url[:200]})
            raise

    # ---------------------------------------------------------------- 对外发消息
    def _napcat(self):
        if self.adapter is not None:
            return self.adapter
        from .. import core
        self.adapter = core.napcat_from_config(self.cfg)
        return self.adapter

    def send_group(self, group_id, text: str) -> dict:
        """往群里发一条消息(失败记账并返回错误, 不抛出)。"""
        try:
            return self._napcat().send_group(int(group_id), str(text))
        except Exception as e:                           # noqa: BLE001
            errors.record("context.send", exc=e, kind="send_group_failed",
                          extra={"group": group_id})
            return {"ok": False, "error": str(e)}

    def send_private(self, user_id, text: str) -> dict:
        try:
            return self._napcat().send_private(int(user_id), str(text))
        except Exception as e:                           # noqa: BLE001
            errors.record("context.send", exc=e, kind="send_private_failed",
                          extra={"user": user_id})
            return {"ok": False, "error": str(e)}

    def notify(self, text: str, session: str = "") -> dict:
        """按会话发消息: `group:123` → 发群; `private:456` → 发私聊; 空 → 发主人。"""
        s = str(session or "")
        if s.startswith("group:"):
            return self.send_group(s.split(":", 1)[1], text)
        if s.startswith("private:"):
            return self.send_private(s.split(":", 1)[1], text)
        try:
            owner = int((self.data.get("push") or {}).get("owner_id") or 0)
        except Exception:
            owner = 0
        return self.send_private(owner, text) if owner else {"ok": False, "error": "没有可用的推送目标"}

    # ---------------------------------------------------------------- 概览
    def describe(self) -> dict:
        return {
            "role": self.role,
            "has_bus": bool(self.bus),
            "has_pipeline": bool(self.pipeline),
            "has_adapter": bool(self.adapter),
            "plugins": self.plugins.status() if self.plugins else [],
            "handlers": self.registry.counts(),
            "routes": len(self.router.routes()),
        }
