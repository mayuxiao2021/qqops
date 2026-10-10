"""容错内核 —— 崩溃账本 / 异常守卫 / 重试 / 退避熔断 / 全局钩子 / 脱敏。

设计原则(见 docs/ARCHITECTURE.md 第 5 节):
  * **失败必须可见**: 任何被吞掉的异常都要进账本(data/crashes.jsonl), 能在控制台看到;
  * **失败必须可隔离**: 一个插件/阶段/请求的异常不能中断其它部分;
  * **反复失败要收敛**: 短时间反复崩溃 → 退避 + 熔断, 而不是无脑重启造成雪崩;
  * **绝不泄露密钥**: 账本与日志里的 token/api key 一律脱敏后再落盘。

本模块只用标准库, 可在 Termux / Windows 下工作。
"""
from __future__ import annotations

import functools
import itertools
import json
import os
import re
import sys
import threading
import time
import traceback

__all__ = [
    "CrashLedger", "ledger", "record", "guard", "safe_call", "retry", "call_with_retry",
    "Backoff", "redact", "install_global_hooks", "request_id", "recent", "stats",
]

_LOCK = threading.RLock()
_RID = itertools.count(1)
_DEDUP_WINDOW = 60.0          # 同一 component+异常 在这个窗口内只记一条(带累计次数)


# ------------------------------------------------------------------ 路径/工具
def _fallback_data_dir() -> str:
    env = os.environ.get("QQOPS_DATA")
    if env:
        return env
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "data")


def data_dir() -> str:
    """数据目录: 优先用 core.DATA(单一事实来源), 取不到再自行推断。"""
    try:
        from .. import core          # 延迟导入: 避免与 core 形成导入环
        return core.DATA
    except Exception:
        return _fallback_data_dir()


def request_id() -> str:
    """短请求号: 便于把"控制台报错"与"服务端日志/账本"对起来。"""
    n = next(_RID)
    return "%s%03d" % (format(int(time.time()) % 100000, "x"), n % 1000)


# ------------------------------------------------------------------ 脱敏
_SECRET_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"), r"\1***"),
    (re.compile(r"(?i)((?:token|api[_-]?key|password|passwd|secret|authorization)"
                r"[\"'\s:=]{1,4})([A-Za-z0-9_\-\.]{6})[A-Za-z0-9_\-\.]*"), r"\1\2***"),
    (re.compile(r"(?i)(bearer\s+)([A-Za-z0-9_\-\.]{6})[A-Za-z0-9_\-\.]*"), r"\1\2***"),
    (re.compile(r"(?i)(X-Token:\s*)(\S{4})\S+"), r"\1\2***"),
]


def redact(text: str) -> str:
    """把可能出现在异常信息里的密钥打码(账本与日志落盘前必过)。"""
    if not text:
        return ""
    out = str(text)
    for pat, rep in _SECRET_PATTERNS:
        out = pat.sub(rep, out)
    return out


def _now() -> float:
    return time.time()


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return str(int(ts))


# ------------------------------------------------------------------ 崩溃账本
class CrashLedger:
    """崩溃/异常账本。

    落盘格式: 每行一个 JSON(追加写), 便于 tail 与增量读取。
    自带轮转(超过 MAX_BYTES 就压缩成 .1)与限流(同 key 60 秒内只记一条, 附带 count)。
    """

    MAX_BYTES = 1_000_000
    KEEP = 200

    def __init__(self, path: str = None):
        self._path = path
        self._seen = {}                     # (component, exc_name) -> [count, first_ts, last_ts, line_ts]

    # ---------------------------------------------------------- 路径
    @property
    def path(self) -> str:
        if self._path:
            return self._path
        return os.path.join(data_dir(), "crashes.jsonl")

    # ---------------------------------------------------------- 写
    def record(self, component: str, exc=None, kind: str = "error",
               message: str = "", extra: dict = None, trace: str = None) -> dict:
        """记一笔。同 key 在窗口内重复时只累加计数, 不刷屏。"""
        now = _now()
        exc_name = type(exc).__name__ if exc is not None else ""
        key = (str(component), exc_name or str(kind))
        with _LOCK:
            st = self._seen.get(key)
            if st and now - st[2] < _DEDUP_WINDOW:
                st[0] += 1
                st[2] = now
                return {"dedup": True, "count": st[0], "component": component,
                        "exc": exc_name, "ts": now}
            count, first = 1, (st[1] if st else now)
            self._seen[key] = [1, first, now, now]

        if trace is None and exc is not None:
            try:
                trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            except Exception:
                trace = ""
        rec = {
            "ts": now,
            "time": _fmt_ts(now),
            "component": redact(str(component))[:80],
            "kind": str(kind)[:32],
            "exc": exc_name[:80],
            "message": redact(message or (str(exc) if exc is not None else ""))[:600],
            "traceback": redact(trace or "")[-2000:],
            "count": count,
            "first_ts": first,
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
        }
        if extra:
            try:
                safe = redact(json.dumps(extra, ensure_ascii=False, default=str))
                parsed = json.loads(safe)
                rec["extra"] = parsed if isinstance(parsed, dict) else {"_": parsed}
            except Exception:
                rec["extra"] = {"_": redact(str(extra))[:200]}
        self._append(rec)
        return rec

    def _append(self, rec: dict) -> None:
        path = self.path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > self.MAX_BYTES:
                try:
                    os.replace(path, path + ".1")
                except Exception:
                    pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass                       # 账本自己绝不能把调用方拖下水

    # ---------------------------------------------------------- 读
    def load(self, limit: int = None) -> list:
        out = []
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            return []
        return out[-limit:] if limit else out

    def recent(self, limit: int = 50) -> list:
        """最近的记录(倒序, 新的在前)。"""
        return list(reversed(self.load(limit)))

    def stats(self, since: float = None, limit: int = None) -> dict:
        rows = self.load(limit)
        by_comp, by_exc, total = {}, {}, 0
        for r in rows:
            ts = r.get("ts") or 0
            if since and ts < since:
                continue
            n = int(r.get("count") or 1)
            total += n
            c = r.get("component") or "?"
            e = r.get("exc") or r.get("kind") or "?"
            by_comp[c] = by_comp.get(c, 0) + n
            by_exc[e] = by_exc.get(e, 0) + n
        return {
            "total": total,
            "records": len(rows),
            "by_component": dict(sorted(by_comp.items(), key=lambda kv: -kv[1])[:10]),
            "by_exception": dict(sorted(by_exc.items(), key=lambda kv: -kv[1])[:10]),
            "last": (rows[-1].get("time") if rows else ""),
            "file": self.path,
        }

    def clear(self) -> int:
        """清空账本, 返回删除的记录条数。"""
        n = len(self.load())
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
            if os.path.exists(self.path + ".1"):
                os.remove(self.path + ".1")
        except Exception:
            pass
        with _LOCK:
            self._seen.clear()
        return n

    def summary_text(self, limit: int = 5) -> str:
        st = self.stats()
        if not st["total"]:
            return "无异常记录 ✅"
        lines = ["异常合计 %d 次(去重 %d 条), 最近 %s"
                 % (st["total"], st["records"], st["last"])]
        for comp, n in list(st["by_component"].items())[:limit]:
            lines.append("  %-16s %d 次" % (comp, n))
        return "\n".join(lines)


LEDGER = CrashLedger()


def ledger() -> CrashLedger:
    return LEDGER


def record(component: str, exc=None, kind: str = "error",
           message: str = "", extra: dict = None) -> dict:
    """记一笔异常到账本(便捷函数)。"""
    return LEDGER.record(component, exc=exc, kind=kind, message=message, extra=extra)


def recent(limit: int = 50) -> list:
    return LEDGER.recent(limit)


def stats(since: float = None) -> dict:
    return LEDGER.stats(since)


# ------------------------------------------------------------------ 异常守卫
class guard:
    """异常守卫: 记账 + 降级, 支持装饰器与 with 两种写法。

        @guard("plugin.x", fallback=[])
        def collect(): ...

        with guard("stage.enrich"):
            do_something()

    - `fallback`: 出错时的降级返回值(传可调用对象则调用它取结果)
    - `reraise=True`: 记账后照旧抛出(用于"必须让上层知道"的场合)
    - `on_error`: 回调 `(component, exc, info)`, 用于告警
    """

    def __init__(self, component: str, fallback=None, reraise: bool = False,
                 kind: str = "error", on_error=None, extra: dict = None):
        self.component = component
        self.fallback = fallback
        self.reraise = reraise
        self.kind = kind
        self.on_error = on_error
        self.extra = extra
        self._handled = False

    # ---- 统一处理
    def handle(self, exc: BaseException, extra: dict = None):
        info = record(self.component, exc=exc, kind=self.kind,
                      extra={**(self.extra or {}), **(extra or {})})
        if self.on_error:
            try:
                self.on_error(self.component, exc, info)
            except Exception:
                pass
        return self._fallback_value()

    def _fallback_value(self):
        fb = self.fallback
        if callable(fb):
            try:
                return fb()
            except Exception:
                return None
        return fb

    # ---- 装饰器
    def __call__(self, fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except Exception as e:                     # noqa: BLE001 —— 这里就是要兜住一切
                self.handle(e)
                if self.reraise:
                    raise
                return self._fallback_value()
        return wrapper

    # ---- 上下文管理器
    def __enter__(self):
        self._handled = False
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        self.handle(exc)
        if self.reraise:
            return False
        return True                                   # 吞掉异常, 继续往下跑


def safe_call(fn, *a, component: str = "qqops", fallback=None, **kw):
    """调用一个函数, 出错就记账并返回降级值。"""
    g = guard(component, fallback=fallback)
    return g(fn)(*a, **kw)


# ------------------------------------------------------------------ 重试
def retry(times: int = 3, delay: float = 1.0, backoff: float = 2.0,
          on=(Exception,), component: str = "qqops", max_delay: float = 30.0,
          on_give_up=None):
    """指数退避重试(装饰器)。彻底失败后记账并抛出最后一次异常。

        @retry(times=3, component="llm.call")
        def call(): ...
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            d, last = float(delay), None
            for i in range(max(1, int(times))):
                try:
                    return fn(*a, **kw)
                except on as e:                        # noqa: PERF203
                    last = e
                    if i + 1 >= max(1, int(times)):
                        break
                    time.sleep(min(d, max_delay))
                    d *= backoff
            record(component, exc=last, kind="retry_exhausted",
                   message="重试 %s 次后仍失败: %s" % (times, last))
            if on_give_up:
                try:
                    return on_give_up(last)
                except Exception as e:
                    record(component, exc=e, kind="give_up_failed")
            raise last
        return wrapper
    return deco


def call_with_retry(fn, *a, **kw):
    """函数式重试: retry 参数从 kw 里取, 其余透传。"""
    times = int(kw.pop("times", 3))
    delay = float(kw.pop("delay", 1.0))
    component = kw.pop("component", "qqops")
    on = kw.pop("on", (Exception,))
    on_give_up = kw.pop("on_give_up", None)
    return retry(times=times, delay=delay, on=on, component=component,
                 on_give_up=on_give_up)(fn)(*a, **kw)


# ------------------------------------------------------------------ 退避/熔断
class Backoff:
    """退避与熔断: 让"反复崩溃"收敛, 而不是无脑重启造成雪崩。

        b = Backoff()
        if b.tripped("collector"):   # 窗口内崩太多次 → 不要再自动重启
            ...
        delay = b.fail("collector")  # 5 → 15 → 45 → 135 ... 上限 cap
        b.ok("collector")            # 稳定运行足够久后清零
    """

    def __init__(self, base: float = 5.0, factor: float = 3.0, cap: float = 300.0,
                 window: float = 600.0, trip: int = 5, stable_after: float = 600.0):
        self.base, self.factor, self.cap = float(base), float(factor), float(cap)
        self.window, self.trip = float(window), int(trip)
        self.stable_after = float(stable_after)
        self._st = {}                                # key -> dict
        self._lock = threading.RLock()

    def _slot(self, key: str) -> dict:
        with self._lock:
            return self._st.setdefault(key, {"attempt": 0, "fails": [], "tripped": False,
                                             "last_fail": 0.0, "last_ok": 0.0})

    def fail(self, key: str) -> float:
        """记一次失败, 返回本次建议等待的秒数; 窗口内失败过多则熔断。"""
        now = _now()
        s = self._slot(key)
        with self._lock:
            s["attempt"] += 1
            s["last_fail"] = now
            s["fails"] = [t for t in s["fails"] if now - t <= self.window] + [now]
            if len(s["fails"]) >= self.trip:
                s["tripped"] = True
            d = min(self.base * (self.factor ** (s["attempt"] - 1)), self.cap)
        return d

    def ok(self, key: str) -> None:
        """记一次成功: 稳定运行超过 stable_after 就清零退避与熔断。"""
        now = _now()
        s = self._slot(key)
        with self._lock:
            prev_ok = s["last_ok"] or now
            s["last_ok"] = now
            if now - prev_ok >= self.stable_after or (s["last_fail"] and
                                                     now - s["last_fail"] >= self.stable_after):
                s["attempt"] = 0
                s["fails"] = []
                s["tripped"] = False

    def delay(self, key: str) -> float:
        s = self._slot(key)
        with self._lock:
            if s["attempt"] <= 0:
                return 0.0
            return min(self.base * (self.factor ** (s["attempt"] - 1)), self.cap)

    def tripped(self, key: str) -> bool:
        s = self._slot(key)
        now = _now()
        with self._lock:
            s["fails"] = [t for t in s["fails"] if now - t <= self.window]
            if len(s["fails"]) < self.trip:
                s["tripped"] = False
            return bool(s["tripped"])

    def due(self, key: str) -> bool:
        """距离上次失败是否已经等够了退避时间(到了才允许再试)。"""
        s = self._slot(key)
        if s["attempt"] <= 0:
            return True
        return _now() - s["last_fail"] >= self.delay(key)

    def reset(self, key: str = None) -> None:
        with self._lock:
            if key is None:
                self._st.clear()
            else:
                self._st.pop(key, None)

    def state(self, key: str = None) -> dict:
        with self._lock:
            if key is not None:
                s = dict(self._slot(key))
                s["delay"] = self.delay(key)
                s["tripped"] = self.tripped(key)
                return s
            return {k: {**v, "delay": self.delay(k), "tripped": self.tripped(k)}
                    for k, v in self._st.items()}


# ------------------------------------------------------------------ 全局钩子
def install_global_hooks(component: str = "process", on_error=None,
                         install_thread_hook: bool = True) -> None:
    """把"漏网"的异常也送进账本: 主线程、子线程、以及线程未捕获异常。

    任何服务进程启动时都应该调用一次 —— 这样即便某处忘了 try/except,
    我们至少能在控制台的崩溃账本里看到它, 而不是进程静默消失。
    """
    def _hook(exc_type, exc, tb, where="main"):
        try:
            LEDGER.record(component, exc=exc, kind="uncaught",
                          message="%s 未捕获异常: %s" % (where, exc),
                          trace="".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        if on_error:
            try:
                on_error(component, exc, {"where": where})
            except Exception:
                pass

    def _excepthook(exc_type, exc, tb):
        _hook(exc_type, exc, tb, "main")

    sys.excepthook = _excepthook
    if install_thread_hook and hasattr(threading, "excepthook"):
        def _threadhook(args):
            _hook(args.exc_type, args.exc_value, args.exc_traceback,
                  "thread:%s" % getattr(args.thread, "name", "?"))
        threading.excepthook = _threadhook
