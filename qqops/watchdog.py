"""看门狗 —— 手机上进程容易被系统清掉(幽灵进程查杀/Doze/内存紧张), 这里定期体检并拉起。

设计要点:
  * 只守护核心三件套(采集器/调度器/控制台), 不守护自己;
  * 体检不只看"进程在不在", 还看"端口能应答 / 心跳是新的"(避免假活);
  * 发现假活(进程在但没反应)先清掉再拉起;
  * **退避 + 熔断**: 反复崩溃的服务按 10s→30s→90s… 退避重启; 窗口内崩太多次则停止重启并告警
    (否则一个坏配置会让手机陷入"崩-拉-崩"的雪崩, 这也是"进程老是被杀"观感的一部分);
  * 心跳写入 state.json, 控制台可据此显示看门狗是否活着;
  * `python3 -m qqops.watchdog --once` 可手动体检一次(排查用)。
"""
from __future__ import annotations

import argparse
import sys
import time

from . import core
from .framework import errors

BACKOFF = errors.Backoff(base=10.0, factor=3.0, cap=300.0, window=900.0, trip=5,
                         stable_after=600.0)


def check_once(cfg: core.Config | None = None, auto_start: bool = True,
               verbose: bool = False) -> list:
    """体检一轮。返回 [(服务, 状态, 说明)]。

    状态: ok / unhealthy / restarted / started / dead / waiting(退避中) / crash_loop(熔断)
    """
    cfg = cfg or core.Config()
    results = []
    for name in core.WATCH_TARGETS:
        mod = core.SERVICE_MODULES[name]
        alive = core.pgrep("[-]m %s" % mod)
        h = core.service_health(name, cfg, timeout=4) if alive else {"ok": False, "why": "进程不存在"}
        if h["ok"]:
            BACKOFF.ok(name)
            results.append((name, "ok", h["why"]))
            continue
        state = "unhealthy" if alive else "dead"
        results.append((name, state, h["why"]))
        if not auto_start:
            continue
        if BACKOFF.tripped(name):
            st = BACKOFF.state(name)
            errors.record("watchdog.%s" % name, kind="crash_loop",
                          message="%s 在 %d 秒内崩溃 %d 次, 已停止自动重启(等人工处理后重置)"
                                  % (name, int(BACKOFF.window), len(st.get("fails") or [])))
            core.log("watchdog", "ERROR", "%s 反复崩溃, 已熔断停止自动重启" % name)
            results.append((name, "crash_loop", "反复崩溃已熔断, 请查看异常账本"))
            continue
        if not BACKOFF.due(name):
            wait = BACKOFF.delay(name) - (time.time() - (BACKOFF.state(name).get("last_fail") or 0))
            results.append((name, "waiting", "退避中, 还需 %.0f 秒" % max(0, wait)))
            continue
        if alive:
            core.kill_pattern("[-]m %s" % mod)
            time.sleep(1)
        delay = BACKOFF.fail(name)
        msg = core.start_service(name)
        core.log("watchdog", "WARN", "%s %s: %s(下次退避 %.0fs)"
                 % (name, "restarted" if alive else "started", msg, delay))
        results.append((name, "restarted" if alive else "started", msg))
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qqops.watchdog", description="QQOps 看门狗")
    ap.add_argument("--once", action="store_true", help="只体检一轮就退出")
    ap.add_argument("--interval", type=int, default=0, help="覆盖体检间隔(秒)")
    a = ap.parse_args(argv)
    core.ensure_dirs()
    cfg = core.Config()
    wd = cfg.data.get("watchdog") or {}
    interval = max(15, int(a.interval or wd.get("interval", 60)))

    if a.once:
        for name, state, msg in check_once(cfg, auto_start=False):
            print("  %-10s %-10s %s" % (name, state, msg))
        return 0

    core.log("watchdog", "INFO", "看门狗启动 (每 %d 秒体检一次)" % interval)
    print("[watchdog] started, interval=%ds" % interval, flush=True)
    # 先落一次心跳: 首轮体检要跑好几秒, 不先写心跳的话启动瞬间会被
    # service_health 判成"没心跳", 报出假的"启动异常"(手机上真踩过)。
    core.beat("watchdog")
    while True:
        try:
            cfg = core.Config()
            wd = cfg.data.get("watchdog") or {}
            interval = max(15, int(a.interval or wd.get("interval", 60)))
            if wd.get("enabled", True):
                res = check_once(cfg, auto_start=True)
                for name, state, msg in res:
                    if state != "ok":
                        core.log("watchdog", "WARN", "%s %s: %s" % (name, state, msg))
            core.beat("watchdog")
            if wd.get("manage_extras"):
                for ex, spec in core.EXTRAS.items():
                    if not core.pgrep(spec["pattern"]):
                        core.log("watchdog", "WARN", "旁路服务 %s 不在, 尝试拉起" % ex)
                        core.extra_action(cfg, ex, "start")
        except Exception as e:
            core.log("watchdog", "ERROR", "体检异常: %s" % e)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
