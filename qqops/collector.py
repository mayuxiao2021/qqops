"""QQOps Collector —— 接收 NapCat 的 OneBot HTTP 上报并落库。

要点(都是踩过坑换来的):
  * NapCat 用 Transfer-Encoding: chunked 发请求体, 必须显式消费;
  * 应答使用 HTTP/1.0 + Connection: close, 否则客户端会解析错位;
  * 仅监听 loopback, 签名可选(有 X-Signature 就校验);
  * 断线重启由调度器/CLI 负责, 本进程只做一件事: 收下并落库。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import core


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "QQOps/" + core.VERSION

    cfg: core.Config = None      # type: ignore
    store: core.Store = None     # type: ignore
    counters = {"ingested": 0, "skipped": 0, "rejected": 0, "errors": 0, "started": 0}

    # ------------------------------------------------------------ 工具
    def _body(self) -> bytes:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            parts = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except Exception:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                parts.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(parts)
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _reply(self, code: int = 200, payload: dict | None = None) -> None:
        out = json.dumps(payload if payload is not None else {"status": "ok", "retcode": 0, "data": None},
                         ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(out)
        self.close_connection = True

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        c = self.counters
        self._reply(200, {"service": "qqops-collector", "version": core.VERSION, "ok": True,
                          "uptime": core.now() - c["started"], "counters": c,
                          "db": self.store.path})

    def do_POST(self):
        c = self.counters
        try:
            body = self._body()
        except Exception as e:
            c["errors"] += 1
            core.log("collector", "ERROR", "读取请求体失败: %s" % e)
            return self._reply(400, {"status": "bad_request"})

        cfg = self.cfg.data["collector"]
        sig = self.headers.get("X-Signature") or ""
        secret = cfg.get("secret") or ""
        if secret and sig:
            want = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
            if not hmac.compare_digest(sig, want):
                c["rejected"] += 1
                core.log("collector", "WARN", "签名不匹配, 已拒绝")
                return self._reply(403, {"status": "forbidden"})
        elif secret and cfg.get("require_signature"):
            c["rejected"] += 1
            return self._reply(403, {"status": "signature_required"})

        try:
            ev = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            c["errors"] += 1
            return self._reply(400, {"status": "invalid_json"})

        if ev.get("post_type") == "message":
            if self._keep(ev):
                try:
                    self.store.insert_message(ev)
                    self.store.upsert_group(int(ev.get("group_id") or 0))
                    c["ingested"] += 1
                    if cfg.get("debug"):
                        core.log("collector", "INFO", "[%s] %s %s: %s" % (
                            ev.get("message_type"), ev.get("group_id"),
                            (ev.get("sender") or {}).get("nickname"),
                            (ev.get("raw_message") or "")[:60]))
                except Exception as e:
                    c["errors"] += 1
                    core.log("collector", "ERROR", "落库失败: %s" % e)
            else:
                c["skipped"] += 1
        self._reply()

    def _keep(self, ev: dict) -> bool:
        cfg = self.cfg.data["collector"]
        mtype = ev.get("message_type")
        if mtype != "group" and not cfg.get("store_private"):
            return False
        groups = [int(g) for g in (cfg.get("groups") or [])]
        if groups and int(ev.get("group_id") or 0) not in groups:
            return False
        kinds = set(cfg.get("kinds") or [])
        if kinds and not (kinds & set(core.kinds_of(ev.get("message")).split(","))):
            return False
        return True

    def log_message(self, *a):
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QQOps 采集器")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default=None)
    args = ap.parse_args(argv)

    core.ensure_dirs()
    cfg = core.Config()
    store = core.Store()
    Handler.cfg, Handler.store = cfg, store
    Handler.counters["started"] = core.now()

    host = args.host or cfg.data["collector"].get("host", "127.0.0.1")
    port = args.port or int(cfg.data["collector"].get("port", 8080))

    srv = ThreadingHTTPServer((host, port), Handler)
    core.log("collector", "INFO", "监听 http://%s:%d (db=%s)" % (host, port, store.path))
    print("[collector] listening on http://%s:%d  db=%s" % (host, port, store.path), flush=True)

    def shutdown(*_a):
        # 注意: 不能在信号处理器里调用 srv.shutdown() —— serve_forever 跑在同一线程,
        # 会死锁导致进程杀不掉并一直占着端口。这里落一次指标后立即退出。
        core.log("collector", "INFO", "收到退出信号, 正在关闭")
        core.state_set("collector_counters", dict(Handler.counters))
        sys.stdout.flush()
        os._exit(0)

    # 逐平台取信号: SIGHUP 在 Windows 上不存在(直接引用会让进程启动即崩)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, shutdown)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        shutdown()
    finally:
        core.state_set("collector_counters", dict(Handler.counters))
    return 0


if __name__ == "__main__":
    sys.exit(main())
