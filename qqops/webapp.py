"""QQOps Web Console —— 现代运维控制台。

形态: 单一 SPA 外壳 + JSON API(标准库实现, 零前端构建)。
安全: 默认只监听 loopback; token 鉴权; HttpOnly Cookie 会话; 密钥只回传掩码。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core
from .console import pages as console_pages
from .framework import errors
from .framework.app import QQOpsApp
from .framework.web import ROUTER, Request, Response

# 注意: 这里**故意不导入** summarizer/render/media/linkfetch —— 它们会连带拉起
# media/linkfetch/extract/fileread 等一大堆模块, 而控制台大多数请求根本用不到。
# 手机上每个进程的内存都是实打实的, 需要的地方在函数里再 import(见各调用点注释)。
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
APP = None                    # QQOpsApp(框架装配后的实例, 由 main() 赋值)


def _asset_ver() -> str:
    """静态资源版本 = 版本号 + 文件指纹。文件一变 URL 就变, 浏览器不可能拿到旧 JS/CSS。"""
    h = hashlib.sha1()
    for name in ("app.css", "app.js"):
        p = os.path.join(STATIC_DIR, name)
        try:
            st = os.stat(p)
            h.update(("%s:%d:%d" % (name, st.st_size, int(st.st_mtime))).encode())
        except Exception:
            pass
    return "%s.%s" % (core.VERSION, h.hexdigest()[:8])


ASSET_VER = _asset_ver()
SESSIONS: dict = {}          # token -> expire_ts
TASK = {"running": False, "kind": "", "started": 0, "result": None, "log": []}


# ---------------------------------------------------------------- 会话
def _new_session(hours: int) -> str:
    tk = secrets.token_urlsafe(24)
    SESSIONS[tk] = core.now() + max(1, int(hours)) * 3600
    for k in [k for k, v in SESSIONS.items() if v < core.now()]:
        SESSIONS.pop(k, None)
    return tk


def _session_ok(tk: str) -> bool:
    exp = SESSIONS.get(tk or "")
    if not exp:
        return False
    if exp < core.now():
        SESSIONS.pop(tk, None)
        return False
    return True


# ---------------------------------------------------------------- 页面外壳
SHELL = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>QQOps 控制台</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🐧</text></svg>">
<link rel="stylesheet" href="/static/app.css?v=%(assets)s">
</head><body>
<div id="app">
  <aside id="sidebar">
    <div class="brand"><span class="logo">🐧</span><span class="bt">QQOps<em>v%(ver)s</em></span></div>
    <nav id="nav"></nav>
    <div class="side-foot">
      <span class="dot" id="health-dot"></span><span id="health-text">连接中…</span>
    </div>
  </aside>
  <main>
    <header id="topbar">
      <button id="menu-btn" class="icon-btn" title="菜单">☰</button>
      <h1 id="page-title">仪表盘</h1>
      <div id="status-strip"></div>
      <div class="spacer"></div>
      <label class="toggle"><input type="checkbox" id="auto-refresh" checked><span>自动刷新</span></label>
      <button class="icon-btn" id="refresh-btn" title="立即刷新">⟳</button>
      <a class="icon-btn" id="logout-btn" href="/logout" title="退出">⎋</a>
    </header>
    <div id="banner" class="hidden"></div>
    <section id="view"></section>
  </main>
  <nav id="bottomnav"></nav>
</div>
<div id="toasts"></div>
<div id="modal" class="hidden"><div class="modal-box"><header><span id="modal-title"></span><button class="icon-btn" id="modal-close">✕</button></header><div id="modal-body"></div></div></div>
<script>window.QQOPS={version:"%(ver)s",refresh:%(refresh)d};</script>
<script src="/static/app.js?v=%(assets)s"></script>
</body></html>
"""

LOGIN = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>QQOps 控制台 · 登录</title>
<link rel="stylesheet" href="/static/app.css?v=%(assets)s"></head><body class="login-body">
<form class="login-card" method="post" action="/login">
  <div class="brand big"><span class="logo">🐧</span> QQOps 控制台</div>
  <p class="muted">请输入访问令牌(在服务器的 <code>data/config.json</code> 里, 键名 <code>web.token</code>;<br>
  或者用安装完成时打印的带令牌网址直接打开)</p>
  %(err)s
  <input name="token" type="password" placeholder="访问令牌" autofocus autocomplete="current-password">
  <button class="primary" type="submit">进入控制台</button>
</form></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "QQOps/" + core.VERSION

    # ------------------------------------------------------------ 基础
    def _cfg(self) -> core.Config:
        return core.Config()

    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie") or ""
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _token_from_req(self, qs: dict) -> str:
        return (qs.get("token", [""])[0] or self.headers.get("X-Token") or
                self._cookies().get("qqops_session", ""))

    def _authed(self, qs: dict) -> bool:
        cfg = self._cfg()
        tok = cfg.data["web"]["token"]
        given = self._token_from_req(qs)
        if given and given == tok:
            return True
        return _session_ok(given)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None, cache: str = "no-store") -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            if cache:
                # 关键: HTML 与接口一律禁缓存, 否则浏览器会启发式缓存旧页面(尤其是换版本后)
                self.send_header("Cache-Control", cache)
                self.send_header("Pragma", "no-cache")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            # 客户端提前断开(刷新页面/关标签/手机切后台)是常态, 不是错误, 别污染日志
            self.close_connection = True

    def json(self, obj, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def html(self, text: str, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8", extra)

    def download(self, data: bytes, filename: str, ctype: str) -> None:
        self._send(200, data, ctype,
                   {"Content-Disposition": 'attachment; filename="%s"' % filename.replace('"', "")})

    def _latest_report_md(self, store) -> str:
        try:
            files = sorted([f for f in os.listdir(core.REPORT_DIR) if f.endswith(".md")], reverse=True)
            if files:
                with open(os.path.join(core.REPORT_DIR, files[0]), encoding="utf-8", errors="replace") as f:
                    return f.read()
        except Exception:
            pass
        events = store.events(status="all", limit=500)
        return "# QQOps 导出\n\n" + "\n".join("- [%s] %s" % (e.get("status"), e.get("title")) for e in events)

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    # ------------------------------------------------------------ 请求级守卫
    def _route_plugin(self, method: str, path: str, qs: dict, body: bytes = b""):
        """先看框架路由表(插件与 console 模块注册的接口); 未命中返回 None。"""
        hit, params = ROUTER.match(method, path)
        if not hit:
            return None
        rid = errors.request_id()
        if hit.auth and not self._authed(qs):
            return self.json({"ok": False, "error": "unauthorized", "rid": rid}, 401)
        req = Request(method=method, path=path, query=qs, headers=dict(self.headers),
                      body=body or b"", token=qs.get("token", [""])[0], rid=rid,
                      client=self.client_address[0] if self.client_address else "",
                      params=params)
        try:
            out = hit.fn(req, APP.ctx if APP else None)
        except Exception as e:                       # noqa: BLE001 —— 插件接口异常只影响这个请求
            errors.record("web.plugin_route", exc=e, kind="route_error",
                          extra={"path": path, "plugin": hit.plugin_id, "rid": rid})
            return self.json({"ok": False, "error": "接口异常: %s" % e, "rid": rid}, 500)
        if isinstance(out, Response):
            return self._send(out.status, out.body, out.ctype, out.headers)
        if isinstance(out, bytes):
            return self._send(200, out, "application/octet-stream")
        if isinstance(out, (dict, list)):
            return self.json(out)
        return self.json({"ok": True, "data": out})

    def _guard_request(self, method: str, fn):
        """把一次请求整体包起来: 记录 request-id、异常进账本、慢请求留痕。"""
        rid = errors.request_id()
        t0 = time.time()
        try:
            return fn()
        except (BrokenPipeError, ConnectionResetError):
            return None                              # 浏览器提前断开, 不算错
        except Exception as e:                       # noqa: BLE001 —— 单请求异常不能拖垮控制台
            errors.record("web.request", exc=e, kind="request_error",
                          extra={"rid": rid, "method": method, "path": self.path})
            core.log("web", "ERROR", "[%s] %s %s 失败: %s" % (rid, method, self.path, e))
            try:
                return self.json({"ok": False, "error": "服务端异常: %s" % e, "rid": rid}, 500)
            except Exception:
                return None
        finally:
            dt = time.time() - t0
            if dt > 2.0:
                core.log("web", "WARN", "[%s] 慢请求 %.1fs %s %s" % (rid, dt, method, self.path))

    # ------------------------------------------------------------ HEAD
    def do_HEAD(self):
        """仅回头部(探活、缓存校验、curl -I 都会用到)。"""
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, b"", "application/json; charset=utf-8")
        if not self._authed(qs):
            if u.path.startswith("/api/"):
                return self.json({"error": "unauthorized"}, 401)
            return self._send(302, b"", "text/plain", {"Location": "/login"})
        if u.path.startswith("/static/"):
            f = os.path.join(STATIC_DIR, os.path.basename(u.path))
            ctype = (mimetypes.guess_type(f)[0] or "application/octet-stream") if os.path.isfile(f) else "text/plain"
            code = 200 if os.path.isfile(f) else 404
            return self._send(code, b"", ctype, cache="public, max-age=60" if code == 200 else "no-store")
        return self._send(200, b"", "text/html; charset=utf-8")

    # ------------------------------------------------------------ GET
    def do_GET(self):
        return self._guard_request("GET", self._do_GET)

    def _do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = u.path

        if path == "/health":
            return self.json({"ok": True, "version": core.VERSION, "ts": core.now()})
        if path == "/login":
            return self.html(LOGIN % {"ver": core.VERSION, "assets": ASSET_VER, "err": ""})
        if path == "/logout":
            return self._send(302, b"", "text/plain", {"Set-Cookie": "qqops_session=; Path=/; Max-Age=0",
                                                       "Location": "/login"})
        if path.startswith("/static/"):
            return self.serve_static(path)
        if not self._authed(qs):
            if path.startswith("/api/"):
                # 接口语义: 未鉴权返回 401 JSON(前端据此跳登录), 不做 302 —— 否则客户端会拿到登录页 HTML
                return self.json({"error": "unauthorized"}, 401)
            if qs.get("token"):
                return self.html(LOGIN % {"ver": core.VERSION, "assets": ASSET_VER,
                                          "err": "<p class=err>令牌无效</p>"}, 401)
            return self._send(302, b"", "text/plain", {"Location": "/login"})
        r = self._route_plugin("GET", path, qs)       # 框架/插件注册的接口优先
        if r is not None:
            return r
        if qs.get("token") == self._cfg().data["web"]["token"] and "qqops_session" not in self._cookies():
            sess = _new_session(self._cfg().data["web"]["session_hours"])
            return self.html(SHELL % {"ver": core.VERSION, "assets": ASSET_VER, "refresh": self._cfg().data["web"]["auto_refresh"]},
                             extra={"Set-Cookie": "qqops_session=%s; Path=/; HttpOnly; SameSite=Lax" % sess})
        if path in ("/", "/index.html"):
            return self.html(SHELL % {"ver": core.VERSION, "assets": ASSET_VER,
                                      "refresh": self._cfg().data["web"]["auto_refresh"]})
        if path.startswith("/r/"):
            name = os.path.basename(path[3:])
            f = os.path.join(core.REPORT_DIR, name)
            if not (name.endswith(".html") and os.path.isfile(f)):
                return self.html("<h1>报告不存在</h1><p><a href='/'>回到控制台</a></p>", 404)
            with open(f, encoding="utf-8", errors="replace") as fh:
                return self.html(fh.read())
        if path == "/api/media/raw":
            rec = core.Store().media_get(qs.get("key", [""])[0])
            p = (rec or {}).get("path") or ""
            if not p or not os.path.isfile(p):
                return self._send(404, b"not found", "text/plain")
            with open(p, "rb") as fh:
                data = fh.read()                     # 必须在 with 里读完: 出了 with 文件就关了
            from . import media as _media            # 懒加载: 只有取图才需要
            return self._send(200, data, _media.guess_mime(os.path.basename(p)),
                              cache="private, max-age=3600")
        try:
            return self.api_get(path, qs)
        except Exception as e:
            core.log("web", "ERROR", "GET %s 失败: %s" % (path, e))
            return self.json({"error": str(e)}, 500)

    def api_get(self, path: str, qs: dict):
        cfg = self._cfg()
        store = core.Store()
        if path == "/api/overview":
            hours = int(qs.get("hours", ["24"])[0])
            snap = core.status_snapshot(cfg)
            nc = core.napcat_from_config(cfg)
            nh = nc.health()
            try:
                gcount = len(nc.group_list())
            except Exception:
                gcount = 0
            snap.update({
                "stats": store.stats(hours),
                "napcat": dict(nh, groups=gcount),
                "runs": store.runs(10),
                "task": {"running": TASK["running"], "kind": TASK["kind"], "started": TASK["started"]},
                "ports": {"collector": core.port_open(cfg.data["collector"]["port"]),
                          "web": True,
                          "napcat": core.port_open(cfg.data["push"].get("napcat_api_port", 3000))},
                "disk": snap["storage"]["disk"],
                "db_size": snap["storage"]["db"],
                "data_size": snap["storage"]["data"],
            })
            return self.json(snap)
        if path == "/api/status":
            return self.json(core.status_snapshot(cfg))
        if path == "/api/schedule":
            return self.json(schedule_payload(cfg, store))
        # ---- 计划: 事件 / 待办 / 记忆 / 链接 / 媒体 ----
        if path == "/api/events":
            st = qs.get("status", ["open"])[0]
            return self.json({"events": store.events(
                status=st, kind=qs.get("kind", [""])[0], group=qs.get("group", [""])[0],
                q=qs.get("q", [""])[0], limit=int(qs.get("limit", ["300"])[0]),
                soon_only=qs.get("soon", ["0"])[0] == "1"), "stats": store.event_stats()})
        if path == "/api/memory":
            return self.json({"memories": store.memory_all(qs.get("prefix", [""])[0]),
                              "profile": core.profile_read()})
        if path == "/api/links":
            return self.json({"links": store.links(status=qs.get("status", [""])[0],
                                                   group=qs.get("group", [""])[0],
                                                   limit=int(qs.get("limit", ["100"])[0]))})
        if path == "/api/media":
            return self.json({"media": store.media_list(int(qs.get("limit", ["60"])[0]),
                                                        qs.get("kind", [""])[0])})
        if path == "/api/export.xlsx":
            from . import render as _render          # 懒加载: 只有导出才需要
            return self.download(
                _render.xlsx_multi({"事件待办": _render.events_rows(store.events(status="all", limit=2000)),
                                    "链接": _render.links_rows(store.links(limit=1000))}, "QQOps"),
                "qqops-events.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if path == "/api/export.docx":
            md = self._latest_report_md(store)
            from . import render as _render          # 懒加载: 只有导出才需要
            return self.download(_render.docx_bytes(_render.md_to_docx_blocks(md), "QQOps 整理"),
                                 "qqops-report.docx",
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if path == "/api/report.docx":
            name = os.path.basename(qs.get("name", [""])[0])
            p = os.path.join(core.REPORT_DIR, name if name.endswith(".md") else name + ".md")
            if not os.path.isfile(p):
                return self.json({"error": "报告不存在"}, 404)
            with open(p, encoding="utf-8", errors="replace") as f:
                md = f.read()
            from . import render as _render          # 懒加载
            return self.download(_render.docx_bytes(_render.md_to_docx_blocks(md), name),
                                 name.replace(".md", "") + ".docx",
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if path == "/api/report.xlsx":
            name = os.path.basename(qs.get("name", [""])[0])
            base = name.rsplit(".", 1)[0]
            return self.download(
                _render.xlsx_multi({"事件待办": _render.events_rows(store.events(status="all", limit=2000)),
                                    "链接": _render.links_rows(store.links(limit=1000))},
                                  base[:28] or "QQOps"),
                base + ".xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if path == "/api/messages":
            return self.json(store.messages(
                group=qs.get("group", [""])[0], kind=qs.get("kind", [""])[0],
                q=qs.get("q", [""])[0], hours=int(qs.get("hours", ["24"])[0]),
                page=int(qs.get("page", ["1"])[0]), size=int(qs.get("size", ["50"])[0])))
        if path == "/api/groups":
            return self.json(self.groups_payload(cfg, store, force=bool(qs.get("refresh"))))
        if path == "/api/runs":
            return self.json({"runs": store.runs(int(qs.get("limit", ["50"])[0]))})
        if path == "/api/reports":
            items = []
            if os.path.isdir(core.REPORT_DIR):
                for f in sorted(os.listdir(core.REPORT_DIR), reverse=True):
                    p = os.path.join(core.REPORT_DIR, f)
                    if os.path.isfile(p) and f.endswith(".md"):
                        items.append({"name": f, "size": os.path.getsize(p),
                                      "mtime": int(os.path.getmtime(p))})
            return self.json({"reports": items[:200]})
        if path == "/api/report":
            name = os.path.basename(qs.get("name", [""])[0])
            p = os.path.join(core.REPORT_DIR, name)
            if not name or not os.path.isfile(p):
                return self.json({"error": "报告不存在"}, 404)
            with open(p, encoding="utf-8", errors="replace") as f:
                return self.json({"name": name, "content": f.read()})
        if path == "/api/config":
            return self.json({"config": cfg.masked(), "errors": cfg.validate(),
                              "paths": {"data": core.DATA, "db": core.DB_PATH, "logs": core.LOG_DIR,
                                        "reports": core.REPORT_DIR, "backups": core.BACKUP_DIR,
                                        "config": core.CONF_PATH}})
        if path == "/api/logs":
            name = re.sub(r"[^A-Za-z0-9_.-]", "", qs.get("name", ["collector"])[0])
            lines = int(qs.get("lines", ["200"])[0])
            f = os.path.join(core.LOG_DIR, "%s.log" % name)
            avail = sorted([x[:-4] for x in os.listdir(core.LOG_DIR) if x.endswith(".log")]) \
                if os.path.isdir(core.LOG_DIR) else []
            return self.json({"name": name, "available": avail, "text": core.tail(f, lines)})
        if path == "/api/export.csv":
            data = store.export_csv(int(qs.get("hours", ["168"])[0]), qs.get("group", [""])[0])
            return self._send(200, data, "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="qqops-export.csv"'})
        if path == "/api/napcat":
            nc = core.napcat_from_config(cfg)
            return self.json({"health": nc.health()})
        return self.json({"error": "not found", "path": path}, 404)

    def groups_payload(self, cfg: core.Config, store: core.Store, force: bool = False) -> dict:
        live, err = [], ""
        try:
            live = core.napcat_from_config(cfg).group_list()
        except Exception as e:
            err = str(e)[:200]
        rows = store.group_rows()
        counts = store.group_counts(168)
        merged = {}
        for g in live:
            merged[int(g["id"])] = {"id": int(g["id"]), "name": g["name"], "in_napcat": True}
        for gid, r in rows.items():
            e = merged.setdefault(int(gid), {"id": int(gid), "name": r["name"], "in_napcat": False})
            if r["name"]:
                e["name"] = r["name"]
        for gid, e in merged.items():
            e["count"] = int(counts.get(gid, 0))
            e["enabled"] = bool(rows.get(gid, {}).get("enabled", 0))
        out = sorted(merged.values(), key=lambda x: (-x["count"], str(x["id"])))
        return {"groups": out, "napcat_count": len(live), "napcat_error": err,
                "enabled_count": sum(1 for g in out if g["enabled"])}

    # ------------------------------------------------------------ POST
    def do_POST(self):
        return self._guard_request("POST", self._do_POST)

    def _do_POST(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = u.path
        if path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            tok = (form.get("token", [""])[0] or "").strip()
            if tok and tok == self._cfg().data["web"]["token"]:
                sess = _new_session(self._cfg().data["web"]["session_hours"])
                return self._send(302, b"", "text/plain",
                                  {"Set-Cookie": "qqops_session=%s; Path=/; HttpOnly; SameSite=Lax" % sess,
                                   "Location": "/"})
            return self.html(LOGIN % {"ver": core.VERSION, "assets": ASSET_VER, "err": "<p class=err>令牌无效</p>"}, 401)
        # 客户端错误上报: 只写日志, 无需鉴权(本机 loopback), 便于远程排查前端问题
        if path == "/api/clientlog":
            body = self.body_json()
            core.log("client", "ERROR", "[%s] %s | %s | url=%s | ua=%s" % (
                str(body.get("kind"))[:20], str(body.get("msg"))[:400],
                str(body.get("stack") or "").replace("\n", " < ")[:1800],
                str(body.get("url"))[:160], str(body.get("ua"))[:100]))
            return self.json({"ok": True})
        if not self._authed(qs):
            return self.json({"error": "unauthorized"}, 401)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        r = self._route_plugin("POST", path, qs, raw)   # 框架/插件注册的接口优先
        if r is not None:
            return r
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}") if raw else {}
        except Exception:
            body = {}
        try:
            return self.api_post(path, body)
        except Exception as e:
            core.log("web", "ERROR", "POST %s 失败: %s" % (path, e))
            return self.json({"error": str(e)}, 500)

    def api_post(self, path: str, body: dict):
        cfg = self._cfg()
        store = core.Store()
        if path == "/api/groups":
            gid = int(body.get("id") or 0)
            if not gid:
                return self.json({"error": "缺少 id"}, 400)
            store.upsert_group(gid, str(body.get("name") or ""),
                               None if body.get("enabled") is None else int(bool(body["enabled"])))
            return self.json({"ok": True, "groups": self.groups_payload(cfg, store)["groups"]})
        if path == "/api/groups/bulk":
            ids = [int(x) for x in (body.get("ids") or [])]
            en = int(bool(body.get("enabled")))
            for gid in ids:
                store.upsert_group(gid, "", en)
            return self.json({"ok": True, "count": len(ids)})
        if path == "/api/config":
            cfg.set_values(body.get("config") or {})
            cfg = core.Config()
            return self.json({"ok": True, "errors": cfg.validate(), "config": cfg.masked()})
        if path == "/api/schedule":
            return self.json(schedule_apply(cfg, body))
        if path == "/api/summarize":
            if TASK["running"]:
                return self.json({"error": "已有任务在运行", "task": TASK}, 409)
            params = {"hours": body.get("hours"), "groups": body.get("groups") or None,
                      "dry_run": bool(body.get("dry_run")), "no_push": bool(body.get("no_push"))}
            threading.Thread(target=_run_task, args=("summarize", params), daemon=True).start()
            return self.json({"ok": True, "task": {"running": True, "kind": "summarize"}})
        if path == "/api/service":
            name = body.get("name") or ""
            action = body.get("action") or ""
            return self.json(service_action(cfg, name, action))
        if path == "/api/backup":
            p = store.backup(core.BACKUP_DIR)
            return self.json({"ok": True, "file": os.path.basename(p)})
        if path == "/api/events":
            eid = int(body.get("id") or 0)
            if body.get("action") == "delete" and eid:
                store.event_delete(eid)
                return self.json({"ok": True, "deleted": eid})
            if not eid:
                return self.json({"error": "缺少 id"}, 400)
            fields = {}
            for k in ("status", "priority", "notes", "title", "detail", "kind"):
                if body.get(k) is not None:
                    fields[k] = body[k]
            if body.get("due"):
                fields["due_at"] = core.parse_due(body["due"])
            if not store.event_update(eid, **fields):
                return self.json({"error": "没有可更新字段"}, 400)
            return self.json({"ok": True, "events": store.events(status=body.get("back_status") or "open")})
        if path == "/api/events/new":
            ev = {"kind": body.get("kind") or "event", "title": body.get("title") or "",
                  "detail": body.get("detail") or "", "who": body.get("who") or "",
                  "group_id": int(body.get("group_id") or 0),
                  "due_at": core.parse_due(body.get("due") or ""),
                  "priority": int(body.get("priority") or 2), "confidence": 1.0, "origin": "manual"}
            if not ev["title"]:
                return self.json({"error": "缺少标题"}, 400)
            eid, created = store.event_upsert(ev)
            return self.json({"ok": True, "id": eid, "created": created})
        if path == "/api/memory":
            scope = str(body.get("scope") or "").strip()
            if body.get("profile") is not None:
                core.profile_write(str(body["profile"]))
                return self.json({"ok": True, "profile": core.profile_read()})
            if not scope:
                return self.json({"error": "缺少 scope"}, 400)
            store.memory_set(scope, str(body.get("key") or "digest"), str(body.get("content") or ""))
            return self.json({"ok": True, "memory": store.memory_get(scope, str(body.get("key") or "digest"))})
        if path == "/api/memory/delete":
            scope = str(body.get("scope") or "")
            if not scope:
                return self.json({"error": "缺少 scope"}, 400)
            con = store.conn()
            try:
                con.execute("DELETE FROM memories WHERE scope=? AND key=?",
                            (scope, str(body.get("key") or "digest")))
                con.commit()
            finally:
                con.close()
            return self.json({"ok": True})
        if path == "/api/link":
            url = str(body.get("url") or "")
            if not url:
                return self.json({"error": "缺少 url"}, 400)
            from . import linkfetch as _linkfetch    # 懒加载
            r = _linkfetch.fetch(url, 12, 512_000, 3)
            store.link_update(url, status="ok" if r["ok"] else "failed",
                              title=(r.get("title") or "")[:200], excerpt=(r.get("text") or "")[:4000],
                              error=(r.get("error") or "")[:200], fetched_at=core.now())
            return self.json({"ok": r["ok"], "title": r.get("title"), "text": (r.get("text") or "")[:4000],
                              "error": r.get("error")})
        if path == "/api/reindex":
            n = store.rebuild_fts()
            return self.json({"ok": True, "rows": n, "fts": {"available": store.fts_ok, "rows": store.fts_count()}})
        if path == "/api/napcat/send":
            text = str(body.get("text") or "")
            if not text:
                return self.json({"error": "缺少 text"}, 400)
            nc = core.napcat_from_config(cfg)
            n = nc.send_text(text, user_id=int(body.get("user_id") or 0),
                             group_id=int(body.get("group_id") or 0))
            return self.json({"ok": True, "chunks": n})
        return self.json({"error": "not found", "path": path}, 404)

    # ------------------------------------------------------------ 静态资源
    def serve_static(self, path: str):
        name = os.path.basename(path)
        f = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(f):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        with open(f, "rb") as fh:
            data = fh.read()
        # 静态资源走版本化 URL(?v=<version>), 用短缓存; 注意不要与默认的 no-store 叠加成两个头
        return self._send(200, data, ctype, cache="public, max-age=60")

    def log_message(self, *a):
        pass


def schedule_payload(cfg: core.Config, store: core.Store) -> dict:
    sch = cfg.data.get("schedule", {})
    last = int(core.state_get("last_summary_ts", 0) or 0)
    return {"schedule": sch, "next": core.next_run(sch), "last_run": last,
            "new_since_last": store.count_since(last) if last else None,
            "preview": core.next_run(dict(sch, enabled=True))}


def schedule_apply(cfg: core.Config, body: dict) -> dict:
    """校验并保存定时设置(三种模式)。"""
    import re as _re
    sch = dict(cfg.data.get("schedule") or {})
    errs = []
    mode = str(body.get("mode") or sch.get("mode") or "daily").lower()
    if body.get("mode") is not None:
        if mode not in ("daily", "interval", "weekly"):
            errs.append("模式只能是 每天/每隔/每周")
        else:
            sch["mode"] = mode
    if body.get("enabled") is not None:
        sch["enabled"] = bool(body["enabled"])
    # 只校验**当前模式真正用到**的字段。
    # 前端会把三套模式的输入一起发过来(隐藏的那两个也在), 以前不看模式就校验, 于是
    # "每隔一段时间"模式报"至少填一个时间点"、"每天"模式报"每周模式至少要选一天" ——
    # 用户怎么点保存都失败(真踩过: 手机任务页的「保存定时」一直弹校验错误)。
    if body.get("times") is not None:
        ts = [str(x).strip() for x in (body["times"] if isinstance(body["times"], list)
                                       else str(body["times"]).split(",")) if str(x).strip()]
        bad = [x for x in ts if not _re.match(r"^\d{1,2}:\d{2}$", x)]
        if bad:
            errs.append("时间点格式应为 HH:MM: %s" % ", ".join(bad))
        elif ts:
            sch["times"] = ["%02d:%02d" % tuple(int(y) for y in x.split(":")) for x in ts]
        elif mode == "daily":
            errs.append("每天模式至少要填一个时间点")
    if body.get("every_minutes") is not None:
        try:
            v = int(body["every_minutes"])
        except Exception:
            v = None
        if v is None:
            errs.append("间隔必须是整数分钟")
        elif not (1 <= v <= 10080):
            if mode == "interval":              # 非"每隔"模式下这个输入框是隐藏的, 不该拦
                errs.append("间隔需在 1~10080 分钟之间")
        else:
            sch["every_minutes"] = v
    if body.get("weekdays") is not None:
        try:
            wd = sorted({int(x) for x in body["weekdays"] if 1 <= int(x) <= 7})
        except Exception:
            wd = None
        if wd is None:
            errs.append("星期取值应为 1~7")
        elif wd:
            sch["weekdays"] = wd
        elif mode == "weekly":
            errs.append("每周模式至少要选一天")
    if body.get("weekly_time") is not None:
        t = str(body["weekly_time"]).strip()
        if _re.match(r"^\d{1,2}:\d{2}$", t):
            sch["weekly_time"] = "%02d:%02d" % tuple(int(y) for y in t.split(":"))
        elif t or mode == "weekly":
            errs.append("每周时刻格式应为 HH:MM")
    if body.get("skip_if_no_new") is not None:
        sch["skip_if_no_new"] = bool(body["skip_if_no_new"])
    if body.get("min_new_messages") is not None:
        try:
            sch["min_new_messages"] = max(0, int(body["min_new_messages"]))
        except Exception:
            errs.append("最小新消息数必须是整数")
    if errs:
        return {"ok": False, "errors": errs, **schedule_payload(cfg, core.Store())}
    cfg.set_values({"schedule": sch})
    cfg = core.Config()
    store = core.Store()
    core.log("web", "INFO", "定时设置已更新: %s" % core.next_run(cfg.data["schedule"]).get("label"))
    return {"ok": True, "errors": [], **schedule_payload(cfg, store)}


def service_action(cfg: core.Config, name: str, action: str) -> dict:
    if name in core.EXTRAS:                      # 旁路服务: NapCat / AstrBot
        if action not in ("start", "stop", "restart"):
            return {"error": "未知动作: %s" % action}
        msg = core.extra_action(cfg, name, action)
        core.log("web", "INFO", "旁路服务 %s/%s: %s" % (name, action, msg))
        return {"ok": True, "message": msg, "extras": core.extra_status(cfg)}
    if name not in core.SERVICE_MODULES:
        return {"error": "未知服务: %s" % name}
    if action == "start":
        msg = core.start_service(name)
    elif action == "stop":
        msg = core.stop_service(name)
    elif action == "restart":
        core.stop_service(name)
        time.sleep(1)
        msg = core.start_service(name)
    else:
        return {"error": "未知动作: %s" % action}
    core.log("web", "INFO", "服务操作 %s/%s: %s" % (name, action, msg))
    time.sleep(1)
    return {"ok": True, "message": msg.strip(), "services": core.service_status(cfg)}


def _run_task(kind: str, params: dict) -> None:
    TASK.update({"running": True, "kind": kind, "started": core.now(), "result": None})
    try:
        from . import summarizer as _summarizer      # 懒加载: 只有真跑总结才需要
        res = _summarizer.run(hours=params.get("hours"), groups=params.get("groups"),
                             dry_run=params.get("dry_run", False), no_push=params.get("no_push", False),
                             job="web")
        TASK["result"] = {k: v for k, v in res.items()}
    except Exception as e:
        TASK["result"] = {"status": "failed", "errors": [str(e)]}
        core.log("web", "ERROR", "后台任务失败: %s" % e)
    finally:
        TASK["running"] = False


def build_server(cfg=None, app=None, host: str = None, port: int = None):
    """构造控制台 HTTP 服务(不启动)。`main()` 与单进程 runner 共用。

    返回 `(srv, url, app)`。`app` 为 None 时会现场装配一个 console 角色的框架实例。
    """
    global APP
    cfg = cfg or core.Config()
    core.apply_power_profile(cfg)
    if app is None:
        APP = QQOpsApp(name="console", roles=("console",), cfg=cfg,
                       store=core.Store()).setup()
        try:
            n = len(console_pages.register(APP))
            core.log("web", "INFO", "控制台接口注册 %d 个(含插件路由)" % n)
        except Exception as e:                   # noqa: BLE001 —— 接口注册失败不该挡住控制台
            errors.record("web.console_pages", exc=e, kind="register_failed")
            core.log("web", "ERROR", "控制台接口注册失败: %s" % e)
        APP.start()
    else:
        APP = app
        try:
            console_pages.register(APP)
        except Exception as e:                   # noqa: BLE001
            errors.record("web.console_pages", exc=e, kind="register_failed")
    h = host or cfg.data["web"].get("host", "127.0.0.1")
    p = int(port or cfg.data["web"].get("port", 8091))
    srv = ThreadingHTTPServer((h, p), Handler)
    srv.daemon_threads = True
    h, p = srv.server_address[0], srv.server_address[1]
    url = "http://%s:%d/?token=%s" % (h, p, cfg.data["web"]["token"])
    return srv, url, APP


def main(argv=None) -> int:
    core.ensure_utf8_console()   # 非 UTF-8 控制台下也能安全打印中文
    core.startup_debug()         # QQOPS_DEBUG_START=<秒> 时卡住会自动 dump 线程栈
    ap = argparse.ArgumentParser(description="QQOps 控制台")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    core.ensure_dirs()
    try:
        srv, url, app = build_server(port=args.port, host=args.host)
    except OSError as e:
        errors.record("web.bind", exc=e, kind="bind_failed", message="控制台端口绑定失败")
        core.log("web", "ERROR", "控制台启动失败: %s" % e)
        print("[webapp] 启动失败: %s" % e, flush=True)
        return 1
    core.log("web", "INFO", "控制台启动 %s" % url)
    print("[webapp] QQOps console: %s" % url, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            app.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
