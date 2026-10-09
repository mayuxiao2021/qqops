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

from . import core, summarizer

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


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
      <h1 id="page-title">概览</h1>
      <div id="status-strip"></div>
      <div class="spacer"></div>
      <label class="toggle"><input type="checkbox" id="auto-refresh" checked><span>自动刷新</span></label>
      <button class="icon-btn" id="refresh-btn" title="立即刷新">⟳</button>
      <a class="icon-btn" id="logout-btn" href="/logout" title="退出">⎋</a>
    </header>
    <section id="view"></section>
  </main>
</div>
<div id="toasts"></div>
<div id="modal" class="hidden"><div class="modal-box"><header><span id="modal-title"></span><button class="icon-btn" id="modal-close">✕</button></header><div id="modal-body"></div></div></div>
<script>window.QQOPS={version:"%(ver)s",refresh:%(refresh)d};</script>
<script src="/static/app.js?v=%(assets)s"></script>
</body></html>
"""

LOGIN = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>QQOps 登录</title>
<link rel="stylesheet" href="/static/app.css?v=%(assets)s"></head><body class="login-body">
<form class="login-card" method="post" action="/login">
  <div class="brand big"><span class="logo">🐧</span> QQOps 控制台</div>
  <p class="muted">输入访问令牌(见 <code>config.json → web.token</code>)</p>
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

    def json(self, obj, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def html(self, text: str, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8", extra)

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

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
        if qs.get("token") == self._cfg().data["web"]["token"] and "qqops_session" not in self._cookies():
            sess = _new_session(self._cfg().data["web"]["session_hours"])
            return self.html(SHELL % {"ver": core.VERSION, "assets": ASSET_VER, "refresh": self._cfg().data["web"]["auto_refresh"]},
                             extra={"Set-Cookie": "qqops_session=%s; Path=/; HttpOnly; SameSite=Lax" % sess})
        if path in ("/", "/index.html"):
            return self.html(SHELL % {"ver": core.VERSION, "assets": ASSET_VER,
                                      "refresh": self._cfg().data["web"]["auto_refresh"]})
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
        try:
            return self.api_post(path, self.body_json())
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
        res = summarizer.run(hours=params.get("hours"), groups=params.get("groups"),
                             dry_run=params.get("dry_run", False), no_push=params.get("no_push", False),
                             job="web")
        TASK["result"] = {k: v for k, v in res.items()}
    except Exception as e:
        TASK["result"] = {"status": "failed", "errors": [str(e)]}
        core.log("web", "ERROR", "后台任务失败: %s" % e)
    finally:
        TASK["running"] = False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QQOps 控制台")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    core.ensure_dirs()
    cfg = core.Config()
    host = args.host or cfg.data["web"].get("host", "127.0.0.1")
    port = args.port or int(cfg.data["web"].get("port", 8091))
    srv = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d/?token=%s" % (host, port, cfg.data["web"]["token"])
    core.log("web", "INFO", "控制台启动 %s" % url)
    print("[webapp] QQOps console: %s" % url, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
