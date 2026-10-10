"""控制台路由注册表 —— 让插件与页面模块自己挂接口(对齐 AstrBot 的 `api/web.py`)。

原有的 `webapp.py` 只有一条巨大的 if/elif 路由; 新增能力就得改它。
这里加一层注册表: `@route("/api/x")` 声明, 启动时收集, 命中即调用。

约定:
  * 处理器签名 `(req, ctx) -> dict | Response | str | bytes | None`
  * 返回 dict/str 会被包装成 JSON; 返回 Response 则原样发送
  * 未命中注册表的路径由 webapp 的既有逻辑兜底(平滑迁移, 不破坏老接口)
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

__all__ = ["Request", "Response", "Router", "ROUTER", "route"]

_PARAM = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")


@dataclass
class Request:
    method: str = "GET"
    path: str = "/"
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    token: str = ""
    rid: str = ""
    client: str = ""
    params: dict = field(default_factory=dict)

    def q(self, key: str, default: str = "") -> str:
        v = self.query.get(key, default)
        return v[0] if isinstance(v, list) and v else (v if v is not None else default)

    def json(self) -> dict:
        import json
        try:
            return json.loads(self.body.decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    def to_dict(self) -> dict:
        return {"method": self.method, "path": self.path, "rid": self.rid,
                "client": self.client, "query": {k: v for k, v in self.query.items()},
                "body_bytes": len(self.body or b"")}


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    ctype: str = "application/json; charset=utf-8"
    headers: dict = field(default_factory=dict)

    @classmethod
    def json(cls, data, status: int = 200) -> "Response":
        import json
        return cls(status=status,
                   body=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    @classmethod
    def text(cls, text: str, status: int = 200) -> "Response":
        return cls(status=status, body=str(text).encode("utf-8"),
                   ctype="text/plain; charset=utf-8")

    @classmethod
    def html(cls, html: str, status: int = 200) -> "Response":
        return cls(status=status, body=str(html).encode("utf-8"),
                   ctype="text/html; charset=utf-8")


@dataclass
class _Route:
    path: str
    fn: object
    methods: tuple = ("GET",)
    auth: bool = True
    plugin_id: str = ""
    pattern: object = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"path": self.path, "methods": list(self.methods), "auth": self.auth,
                "plugin": self.plugin_id, "handler": getattr(self.fn, "__name__", "?"),
                "meta": self.meta}


class Router:
    """极简路由表: 精确匹配 + `<name>` 占位段 + 前缀匹配。"""

    def __init__(self):
        self._routes = []
        self._lock = threading.RLock()
        self.stats = {"hit": 0, "miss": 0, "error": 0}

    # ---------------------------------------------------------------- 写
    @staticmethod
    def _pattern(path: str):
        """把 `/api/x/<name>` 编译成正则(占位段捕获为具名组)。"""
        parts, i = [], 0
        for m in _PARAM.finditer(path):
            parts.append(re.escape(path[i:m.start()]))
            parts.append("(?P<%s>[^/]+)" % m.group(1))
            i = m.end()
        parts.append(re.escape(path[i:]))
        return re.compile("^" + "".join(parts) + "$")

    def add(self, path: str, fn, methods=("GET",), auth: bool = True,
            plugin_id: str = "", meta: dict = None) -> _Route:
        path = "/" + str(path).strip("/")
        r = _Route(path=path, fn=fn, methods=tuple(m.upper() for m in methods),
                   auth=bool(auth), plugin_id=plugin_id, pattern=self._pattern(path),
                   meta=dict(meta or {}))
        with self._lock:
            self._routes = [x for x in self._routes if not (x.path == r.path
                                                           and x.methods == r.methods)]
            self._routes.append(r)
            self._routes.sort(key=lambda x: (-len(x.path), x.path))
        # 同时登记到注册表: 控制台「运维→自检」能看到完整接口清单(插件与框架一视同仁)
        try:
            from .registry import REGISTRY, Ref
            REGISTRY.add(Ref(kind="route", name=path, fn=fn, plugin_id=plugin_id,
                             priority=int((meta or {}).get("priority", 100)),
                             meta={"methods": list(r.methods), "auth": r.auth,
                                   **(meta or {})}))
        except Exception:
            pass
        return r

    def remove_plugin(self, plugin_id: str) -> int:
        with self._lock:
            n = len(self._routes)
            self._routes = [r for r in self._routes if r.plugin_id != plugin_id]
            return n - len(self._routes)

    def clear(self) -> None:
        with self._lock:
            self._routes = []

    # ---------------------------------------------------------------- 读
    def routes(self, plugin_id: str = None) -> list:
        with self._lock:
            rows = [r for r in self._routes if plugin_id is None or r.plugin_id == plugin_id]
        return rows

    def match(self, method: str, path: str):
        """返回 `(route, params)`; 未命中返回 `(None, {})`。"""
        method = str(method or "GET").upper()
        path = "/" + str(path or "").strip("/")
        with self._lock:
            rows = list(self._routes)
        for r in rows:
            if method not in r.methods and "ALL" not in r.methods:
                continue
            m = r.pattern.match(path)
            if m:
                return r, {k: v for k, v in m.groupdict().items()}
        return None, {}

    def state(self) -> dict:
        return {"count": len(self.routes()), "stats": dict(self.stats),
                "routes": [r.to_dict() for r in self.routes()]}


ROUTER = Router()


def route(path: str, methods=("GET",), auth: bool = True, role: str = "console"):
    """装饰器: 把方法注册为控制台接口(由 PluginManager 在加载时收录)。"""
    from .plugin import route as _r
    return _r(path, methods=methods, auth=auth, role=role)
