"""QQOps Collector —— 接收 NapCat 的 OneBot HTTP 上报并落库。

接收处理规范(逐条都有对应测试, 详见 docs/INGEST.md):
  * `Transfer-Encoding: chunked` 必须显式消费(NapCat 用它发请求体);
  * 请求体上限 8MB, 超了直接 413 —— 不能因为一条畸形上报把内存吃光;
  * 应答 HTTP/1.0 + `Connection: close`, 且必须**先落库再异步发事件**, 让 NapCat 尽快拿到 200;
  * 只认 loopback; 签名可选(配了 secret 就校验 X-Signature 的 HMAC-SHA1);
  * `post_type=message` → 落库; `message_sent` → 也落库(标记 self, 对话上下文需要);
  * `post_type=notice` → 撤回事件标记消息已撤回; 其它通知记 debug;
  * `post_type=meta_event` → 心跳写 napcat_beat(这是"NapCat 是否真连着"的唯一可靠信号);
  * 去重: (group_id, message_id) 唯一索引 + INSERT OR IGNORE, 重连补发不会重复入库;
  * 时钟回拨保护: 事件时间落在未来超过 1 天时以本地时间为准。
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
from .framework import errors
from .framework.app import QQOpsApp
from .framework.event import Event

MAX_BODY = 8 * 1024 * 1024          # 8MB: 正常上报远小于此, 超过即视为异常


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "QQOps/" + core.VERSION

    cfg: core.Config = None      # type: ignore
    store: core.Store = None     # type: ignore
    app: QQOpsApp = None         # type: ignore
    counters = {"ingested": 0, "skipped": 0, "rejected": 0, "errors": 0, "started": 0,
                "duplicate": 0, "notice": 0, "meta": 0, "sent": 0}

    # ------------------------------------------------------------ 工具
    def _body(self):
        """读请求体。超过上限或格式异常返回 None(调用方回 4xx)。"""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            parts, total = [], 0
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except Exception:
                    return None
                if size == 0:
                    self.rfile.readline()
                    break
                total += size
                if total > MAX_BODY:
                    return None
                parts.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(parts)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            return None
        if n > MAX_BODY:
            return None
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
        body = None
        try:
            body = self._body()
        except Exception as e:
            c["errors"] += 1
            core.log("collector", "ERROR", "读取请求体失败: %s" % e)
            return self._reply(400, {"status": "bad_request"})
        if body is None:
            c["errors"] += 1
            errors.record("collector.body", kind="body_too_large",
                          message="请求体超过 %d 字节或分块格式异常, 已拒绝" % MAX_BODY)
            return self._reply(413, {"status": "payload_too_large"})

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
            if not isinstance(ev, dict):
                raise ValueError("上报体必须是 JSON 对象")
        except Exception as e:
            c["errors"] += 1
            core.log("collector", "WARN", "上报体解析失败: %s" % e)
            return self._reply(400, {"status": "invalid_json"})

        # 按 OneBot v11 的 post_type 分派
        ptype = str(ev.get("post_type") or "")
        if ptype in ("message", "message_sent"):
            self._on_message(ev, ptype)
        elif ptype == "notice":
            self._on_notice(ev)
        elif ptype == "meta_event":
            self._on_meta(ev)
        elif ptype == "request":
            c["meta"] += 1
            core.log("collector", "INFO", "收到请求事件(%s), 按规范忽略" % (ev.get("request_type") or "?"))
        else:
            c["skipped"] += 1
            core.log("collector", "WARN", "未知 post_type: %r(已跳过)" % ptype)
        self._reply()

    # ------------------------------------------------------------ 消息
    def _on_message(self, ev: dict, ptype: str) -> None:
        c = self.counters
        if not self._keep(ev, ptype):
            c["skipped"] += 1
            return
        try:
            res = self.store.insert_message(ev)
        except Exception as e:                       # noqa: BLE001
            c["errors"] += 1
            errors.record("collector.store", exc=e, kind="store_failed",
                          extra={"group": ev.get("group_id")})
            core.log("collector", "ERROR", "落库失败: %s" % e)
            return
        if res == "duplicate":                       # 断线重连补发
            c["duplicate"] += 1
            return
        if res == "empty":
            c["skipped"] += 1
            return
        self.store.upsert_group(int(ev.get("group_id") or 0))
        if ptype == "message_sent":
            c["sent"] += 1
        else:
            c["ingested"] += 1
        cfg = self.cfg.data["collector"]
        if cfg.get("debug"):
            core.log("collector", "INFO", "[%s%s] %s %s: %s" % (
                ev.get("message_type"), "/self" if ptype == "message_sent" else "",
                ev.get("group_id"), (ev.get("sender") or {}).get("nickname"),
                (ev.get("raw_message") or "")[:60]))
        # 落库之后把事件交给总线(插件/阶段在后台处理, 不阻塞采集, 保证 NapCat 快速拿到 200)
        if self.app is not None:
            try:
                kind = "message.sent" if ptype == "message_sent" else "message.received"
                session = ("group:%s" % (ev.get("group_id") or 0)
                           if (ev.get("message_type") or "group") == "group"
                           else "private:%s" % (ev.get("user_id") or 0))
                self.app.publish(Event(kind, payload=ev, source="onebot", session=session),
                                 wait=False)
            except Exception as e:                   # noqa: BLE001
                errors.record("collector.publish", exc=e, kind="publish_failed")

    def _keep(self, ev: dict, ptype: str = "message") -> bool:
        cfg = self.cfg.data["collector"]
        mtype = ev.get("message_type")
        if mtype != "group" and not cfg.get("store_private"):
            return False
        groups = [int(g) for g in (cfg.get("groups") or [])]
        if groups and int(ev.get("group_id") or 0) not in groups:
            return False
        if ptype == "message_sent":
            return True                              # 机器人自己的消息不受类型过滤(对话上下文需要)
        kinds = set(cfg.get("kinds") or [])
        if kinds and not (kinds & set(core.kinds_of(ev.get("message")).split(","))):
            return False
        return True

    # ------------------------------------------------------------ 通知(撤回等)
    def _on_notice(self, ev: dict) -> None:
        c = self.counters
        c["notice"] += 1
        nt = str(ev.get("notice_type") or "")
        gid = int(ev.get("group_id") or 0)
        if nt == "group_recall":
            marked = self.store.mark_recalled(gid, int(ev.get("message_id") or 0))
            core.log("collector", "INFO", "撤回: 群 %s 消息 %s(标记 %d 条)"
                     % (gid, ev.get("message_id"), marked))
            if self.app is not None:
                self.app.publish(Event("message.recalled",
                                       payload={"group_id": gid,
                                                "message_id": ev.get("message_id"),
                                                "user_id": ev.get("user_id"),
                                                "operator": ev.get("operator_id"),
                                                "marked": marked},
                                       source="onebot", session="group:%s" % gid), wait=False)
        elif nt == "friend_recall":
            core.log("collector", "INFO", "撤回(私聊): %s" % ev.get("message_id"))
        else:
            core.log("collector", "DEBUG", "通知事件已记录(暂不处理): %s" % nt)

    # ------------------------------------------------------------ 元事件(心跳/生命周期)
    def _on_meta(self, ev: dict) -> None:
        c = self.counters
        c["meta"] += 1
        mt = str(ev.get("meta_event_type") or "")
        if mt == "heartbeat":
            st = ev.get("status") or {}
            online = bool(st.get("online", True))
            core.state_set("napcat_beat", core.now())
            core.state_set("napcat_status", {"online": online, "good": bool(st.get("good", True)),
                                             "interval": ev.get("interval")})
            prev = core.state_get("napcat_online", None)
            if prev is None or bool(prev) != online:
                core.state_set("napcat_online", online)
                core.log("collector", "INFO", "NapCat 连接状态: %s" % ("在线" if online else "离线"))
            if self.app is not None:
                self.app.publish(Event("adapter.heartbeat",
                                       payload={"online": online, "good": bool(st.get("good", True))},
                                       source="onebot"), wait=False)
        elif mt == "lifecycle":
            core.log("collector", "INFO", "元事件 lifecycle: %s" % (ev.get("sub_type") or ""))
        else:
            core.log("collector", "DEBUG", "元事件: %s" % mt)

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

    # 框架装配: 采集进程只跑 ingest 角色(管线阶段 + 事件处理器)
    try:
        Handler.app = QQOpsApp(name="collector", roles=("ingest",), cfg=cfg,
                               store=store).setup().start()
    except Exception as e:                       # noqa: BLE001 —— 框架装配失败也要能收消息
        errors.record("collector.app", exc=e, kind="setup_failed")
        core.log("collector", "ERROR", "框架装配失败(继续以裸采集模式运行): %s" % e)

    host = args.host or cfg.data["collector"].get("host", "127.0.0.1")
    port = args.port or int(cfg.data["collector"].get("port", 8080))

    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        # 端口被占: 明确记账并退出(否则会变成一个"看起来在跑"的空壳)
        errors.record("collector.bind", exc=e, kind="bind_failed",
                      message="端口 %s:%d 绑定失败" % (host, port))
        core.log("collector", "ERROR", "绑定 %s:%d 失败: %s" % (host, port, e))
        print("[collector] 绑定失败: %s" % e, flush=True)
        return 1
    core.log("collector", "INFO", "监听 http://%s:%d (db=%s)" % (host, port, store.path))
    print("[collector] listening on http://%s:%d  db=%s" % (host, port, store.path), flush=True)

    def shutdown(*_a):
        # 注意: 不能在信号处理器里调用 srv.shutdown() —— serve_forever 跑在同一线程,
        # 会死锁导致进程杀不掉并一直占着端口。这里落一次指标后立即退出。
        core.log("collector", "INFO", "收到退出信号, 正在关闭")
        core.state_set("collector_counters", dict(Handler.counters))
        try:
            if Handler.app:
                Handler.app.stop()
        except Exception:
            pass
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
