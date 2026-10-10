"""单进程运行器 —— 把 采集/调度/控制台/看门狗 合并进**一个进程**(线程化)。

为什么合并(真机实测):
  * 手机上每个 Python 解释器常驻 ≈25MB, 四个进程 ≈100MB; 合并后 ≈30MB;
  * 唤醒次数从 4 套循环降到 1 套主管循环, 待机更省电;
  * 线程级自愈: 某个线程挂了由主管按退避重启, 比"进程被系统悄悄杀掉"更可控;
  * 插件只需装配一次(一个 QQOpsApp 同时具备 ingest/tasks/console 三种角色)。

代价(所以保留多进程模式 `runtime.mode=multi`):
  * 隔离性弱: 一个线程里的致命错误会带走整个进程;
  * 不能单独重启某个服务, 只能整体重启;
  * 排查问题时不如分进程直观。

主管循环(supervisor)每 interval 秒做三件事:
  1) 给四个服务写心跳(`beat_<name>`), 控制台据此看出"哪个环节不转了";
  2) 检查线程是否活着 —— 死了按 **退避** 重启; 反复崩溃则熔断并记账;
  3) 按档位间隔跑调度滴答(与心跳同拍, 减少唤醒次数)。

用法:
    python3 -m qqops.runner                # 跟随配置(runtime.mode)
    python3 -m qqops.runner --services collector,web
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from . import collector, core, scheduler, webapp
from .framework import errors
from .framework.app import QQOpsApp

ALL = ("collector", "scheduler", "web", "watchdog")


class Runner:
    """单进程运行器。线程名带 qqops- 前缀, 便于 ps/top 里辨认。"""

    def __init__(self, cfg: core.Config = None, store: core.Store = None,
                 services=ALL, app=None):
        self.cfg = cfg or core.Config()
        core.apply_power_profile(self.cfg)
        self.store = store or core.Store()
        self.services = tuple(s for s in services if s in ALL) or ALL
        self.app = app
        self.profile = core.power_profile(self.cfg)
        self.stop_event = threading.Event()
        self.threads = {}
        self.servers = {}
        self.backoff = errors.Backoff(base=10.0, factor=3.0, cap=300.0, window=900.0,
                                      trip=5, stable_after=600.0)
        self.restarts = {}
        self.tick = int(self.profile["scheduler_tick"])
        self.interval = int(self.profile["watchdog_interval"])
        self.started_at = 0.0

    # ---------------------------------------------------------------- 线程工厂
    def _spawn(self, name: str) -> bool:
        """起(或重起)一个服务线程。返回是否成功。"""
        try:
            if name == "collector":
                srv = collector.build_server(self.cfg, self.store, self.app)
                self.servers["collector"] = srv
                t = threading.Thread(target=srv.serve_forever, name="qqops-collector",
                                     kwargs={"poll_interval": 0.5}, daemon=True)
            elif name == "web":
                srv, url, _app = webapp.build_server(self.cfg, self.app)
                self.servers["web"] = srv
                self.app = _app
                core.log("runner", "INFO", "控制台: %s" % url)
                print("[runner] console: %s" % url, flush=True)
                t = threading.Thread(target=srv.serve_forever, name="qqops-web",
                                     kwargs={"poll_interval": 0.5}, daemon=True)
            elif name == "scheduler":
                t = threading.Thread(target=scheduler.loop, name="qqops-scheduler",
                                     kwargs={"stop": self.stop_event, "tick": self.tick,
                                             "app": self.app}, daemon=True)
            else:
                return False
            t.start()
            self.threads[name] = t
            return True
        except Exception as e:                       # noqa: BLE001 —— 单个服务起不来不影响其它
            errors.record("runner.spawn.%s" % name, exc=e, kind="spawn_failed",
                          message="无法启动 %s 线程" % name)
            core.log("runner", "ERROR", "启动 %s 失败: %s" % (name, e))
            return False

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> "Runner":
        core.ensure_dirs()
        self.started_at = time.time()
        if self.app is None:
            try:
                self.app = QQOpsApp(name="qqops", cfg=self.cfg, store=self.store,
                                    roles=("ingest", "tasks", "console")).setup().start()
            except Exception as e:                   # noqa: BLE001 —— 框架挂了也要能收消息
                errors.record("runner.app", exc=e, kind="setup_failed")
                core.log("runner", "ERROR", "框架装配失败(服务继续运行): %s" % e)
        for name in self.services:
            if name == "watchdog":
                continue                             # 主管循环本身就在跑, 不需要线程
            self.restarts[name] = 0
            if not self._spawn(name):
                self.restarts[name] = 1
        core.runtime_write("single", list(self.services))
        core.log("runner", "INFO", "单进程模式启动: %s | 档位 %s(调度 %ds / 主管 %ds)"
                 % (",".join(self.services), self.profile["power_mode"], self.tick, self.interval))
        return self

    def stop(self, timeout: float = 5.0) -> None:
        core.log("runner", "INFO", "正在退出…")
        self.stop_event.set()
        for name, srv in self.servers.items():
            try:
                srv.shutdown()                       # 从主线程调用, 不是服务线程 → 不会死锁
                srv.server_close()
            except Exception:
                pass
        for name, t in self.threads.items():
            try:
                t.join(timeout=timeout / max(1, len(self.threads)))
            except Exception:
                pass
        try:
            if self.app:
                self.app.stop()
        except Exception:
            pass
        try:
            core.state_set("collector_counters", dict(collector.Handler.counters))
        except Exception:
            pass
        core.runtime_clear()
        core.beat("watchdog", 0)

    # ---------------------------------------------------------------- 主管
    def supervise_once(self) -> list:
        """一轮主管: 写心跳 + 检查线程 + 自愈 + 跑调度。返回 [(服务, 状态, 说明)]。"""
        core.beat("watchdog")
        # 主管本身永远是"看门狗"角色, 无论 services 里有没有它
        out = [("watchdog", "ok", "主管运行中")]
        for name in self.services:
            if name == "watchdog":
                continue
            t = self.threads.get(name)
            alive = bool(t and t.is_alive())
            if alive:
                core.beat(name)                      # 只给"真的活着"的线程写心跳, 心跳不说谎
                self.backoff.ok(name)
                out.append((name, "ok", "线程存活"))
                continue
            # 线程不在了 → 退避重启 / 熔断
            if self.backoff.tripped(name):
                errors.record("runner.%s" % name, kind="crash_loop",
                              message="%s 线程反复退出, 已熔断, 请查看异常账本" % name)
                out.append((name, "crash_loop", "反复退出已熔断"))
                continue
            if not self.backoff.due(name):
                out.append((name, "waiting", "退避中"))
                continue
            delay = self.backoff.fail(name)
            self.restarts[name] = self.restarts.get(name, 0) + 1
            ok = self._spawn(name)
            core.log("runner", "WARN", "%s 线程不在, 已重启(第 %d 次, 下次退避 %.0fs)"
                     % (name, self.restarts[name], delay))
            out.append((name, "restarted" if ok else "dead",
                        "已重启(第 %d 次)" % self.restarts[name] if ok else "重启失败"))
        # 调度滴答(与心跳同拍, 省唤醒)
        try:
            if self._tick_due():
                scheduler.tick_once(self.cfg, self.store, self.app)
                self._last_tick = time.time()
        except Exception as e:                       # noqa: BLE001
            errors.record("runner.tick", exc=e, kind="tick_failed")
        return out

    _last_tick = 0.0

    def _tick_due(self) -> bool:
        return (time.time() - self._last_tick) >= max(1, self.tick)

    def run_forever(self) -> int:
        """安装信号 → 主管循环。阻塞直到收到退出信号。"""
        def bye(*_a):
            self.stop_event.set()
        for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig = getattr(signal, sig_name, None)
            if sig is None or threading.current_thread() is not threading.main_thread():
                continue
            try:
                signal.signal(sig, bye)
            except Exception:
                pass
        interval = max(5, self.interval)
        while not self.stop_event.is_set():
            try:
                for name, state, msg in self.supervise_once():
                    if state not in ("ok",):
                        core.log("runner", "WARN", "%s %s: %s" % (name, state, msg))
            except Exception as e:                   # noqa: BLE001 —— 主管绝不能死
                errors.record("runner.supervise", exc=e, kind="supervise_failed")
            # 分片睡眠: 收到退出信号能立刻响应
            end = time.time() + interval
            while time.time() < end and not self.stop_event.is_set():
                time.sleep(0.3)
        self.stop()
        return 0

    # ---------------------------------------------------------------- 观测
    def status(self) -> dict:
        rt = core.runtime_state()
        return {
            "mode": "single", "pid": os.getpid(), "alive": True,
            "services": list(self.services),
            "threads": {n: bool(t.is_alive()) for n, t in self.threads.items()},
            "restarts": dict(self.restarts),
            "power": self.profile,
            "rss": core.self_rss(),
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
            "runtime_file": rt.get("mode"),
        }


def main(argv=None) -> int:
    core.ensure_utf8_console()   # 非 UTF-8 控制台下也能安全打印中文
    ap = argparse.ArgumentParser(description="QQOps 单进程运行器(四服务合并)")
    ap.add_argument("--services", default=",".join(ALL),
                    help="要跑的服务, 逗号分隔: %s" % "/".join(ALL))
    a = ap.parse_args(argv)
    core.ensure_dirs()
    cfg = core.Config()
    services = tuple(s.strip() for s in str(a.services).split(",") if s.strip())
    runner = Runner(cfg=cfg, services=services).start()
    prof = runner.profile
    print("[runner] 单进程模式: %s | 档位 %s | PID %d | RSS %.1f MB"
          % (",".join(runner.services), prof["power_mode"], os.getpid(),
             core.self_rss() / 1048576.0), flush=True)
    return runner.run_forever()


if __name__ == "__main__":
    sys.exit(main())
