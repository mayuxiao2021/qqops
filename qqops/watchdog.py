"""看门狗 —— 手机上进程容易被系统清掉(幽灵进程查杀/Doze/内存紧张), 这里定期体检并拉起。

设计要点:
  * 只守护核心三件套(采集器/调度器/控制台), 不守护自己;
  * 体检不只看"进程在不在", 还看"端口能应答 / 心跳是新的"(避免假活);
  * 发现假活(进程在但没反应)先清掉再拉起;
  * 心写 state.json, 控制台可据此显示看门狗是否活着;
  * `python3 -m qqops.watchdog --once` 可手动体检一次(排查用)。
"""
from __future__ import annotations

import argparse
import sys
import time

from . import core


def check_once(cfg: core.Config | None = None, auto_start: bool = True,
               verbose: bool = False) -> list:
    """体检一轮。返回 [(服务, 状态, 说明)]。状态: ok / unhealthy / restarted / started / dead"""
    cfg = cfg or core.Config()
    results = []
    for name in core.WATCH_TARGETS:
        mod = core.SERVICE_MODULES[name]
        if core.pgrep("[-]m %s" % mod):
            h = core.service_health(name, cfg, timeout=4)
            if h["ok"]:
                results.append((name, "ok", h["why"]))
                continue
            results.append((name, "unhealthy", h["why"]))
            if auto_start:                       # 假活: 先清掉再拉起
                core.kill_pattern("[-]m %s" % mod)
                time.sleep(1)
                results.append((name, "restarted", core.start_service(name)))
            continue
        results.append((name, "dead", "进程不存在"))
        if auto_start:
            results.append((name, "started", core.start_service(name)))
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
            core.state_set("watchdog_beat", core.now())
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
