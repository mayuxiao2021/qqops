"""服务进程管理 CLI —— 与 Web 控制台共用 core.start_service/stop_service。

之所以用 Python 而不是 shell 的 `( ... & )`:后者在 Termux 下会残留不退出的父进程,
继承调用方的管道,导致 `cmd | tail` 之类的管道永不结束。Python 的
`Popen(..., start_new_session=True, stdin=DEVNULL, stdout=文件)` 是干净的守护化方式。

用法:
  python3 -m qqops.svc status                 # 内置三服务
  python3 -m qqops.svc restart                # 重启内置三服务
  python3 -m qqops.svc extras                 # 旁路服务(NapCat/AstrBot)状态
  python3 -m qqops.svc --extra stop astrbot   # 停掉旁路服务
"""
from __future__ import annotations

import argparse
import sys
import time

from . import core


def _show_extras() -> None:
    cfg = core.Config()
    for name, st in core.extra_status(cfg).items():
        print("  %s %-18s %-28s %s" % ("●" if st["running"] else "○", name, st["label"],
                                       ("PID %s" % st["pid"]) if st["pid"] else "未运行"))


def _status(cfg: core.Config, names: list) -> int:
    inst = core.instance_status(cfg)
    prof = inst["power"]
    print("  运行模式: %s(配置 %s)%s  档位: %s(%s)"
          % (inst["mode"], (cfg.data.get("runtime") or {}).get("mode", "auto"),
             " · 已运行" if inst["alive"] else " · 未运行", prof["power_mode"], prof["label"]))
    if inst["alive"]:
        print("  进程    : pid %s · 内存 %.1f MB · 服务 %s"
              % (inst["pid"], inst["rss"] / 1048576.0, ",".join(inst["services"]) or "-"))
    print("  资源档  : 调度 %ds / 主管 %ds / 控制台刷新 %ds / SQLite 缓存 %dKB / 富化 %s"
          % (prof["scheduler_tick"], prof["watchdog_interval"], prof["console_refresh"],
             prof["sqlite_cache_kb"], "夜间窗口" if prof["defer_enrich"] else "立即"))
    for n in names:
        s = inst["services"].get(n, {})
        port = ("  端口 %s" % s["port"]) if s.get("port") else ""
        pid = ("  进程号 %s" % s["pid"]) if s.get("pid") else ""
        h = core.service_health(n, cfg, timeout=3) if s.get("running") else {"why": "未运行"}
        mark = "●" if (s.get("running") and h.get("ok")) else ("◐" if s.get("running") else "○")
        print("  %s %-10s%s%s  %s" % (mark, n, port, pid, h.get("why", "")))
    return 0


def _switch(cfg: core.Config, key: str, val: str, label: str) -> int:
    """切换运行模式 / 功耗档位, 并重启实例让它生效。"""
    if not val:
        inst = core.instance_status(cfg)
        cur = (cfg.data.get("runtime") or {}).get(key, "")
        print("  当前 %s: %s" % (label, cur or ("single" if inst["mode"] == "single" else "multi")))
        return 0
    allowed = {"mode": ("auto", "single", "multi"),
               "power_mode": ("performance", "balanced", "saver")}[key]
    if val not in allowed:
        print("  用法: qqops %s %s" % (label, "|".join(allowed)))
        return 2
    cfg.set_values({"runtime": {key: val}})
    new_cfg = core.Config()
    if key == "power_mode":
        prof = core.power_profile(new_cfg)
        print("  已切换到 %s 档(%s): 调度 %ds / 主管 %ds / 控制台刷新 %ds / SQLite %dKB / 富化 %s"
              % (prof["power_mode"], prof["label"], prof["scheduler_tick"],
                 prof["watchdog_interval"], prof["console_refresh"], prof["sqlite_cache_kb"],
                 "夜间窗口" if prof["defer_enrich"] else "立即"))
    else:
        print("  运行模式已设为 %s(解析为 %s)" % (val, core.resolve_mode(new_cfg)))
    print("  正在重启实例使其生效…")
    print("  " + core.start_instance(new_cfg))
    return 0


def main(argv=None) -> int:
    core.ensure_utf8_console()   # 非 UTF-8 控制台下也能安全打印中文
    core.startup_debug()         # QQOPS_DEBUG_START=<秒> 时卡住会自动 dump 线程栈
    ap = argparse.ArgumentParser(prog="qqops.svc", description="QQOps 服务进程管理")
    ap.add_argument("action", choices=["start", "stop", "restart", "status", "extras",
                                       "mode", "power"])
    ap.add_argument("names", nargs="*", help="collector / scheduler / web, 或旁路服务名 / 模式值")
    ap.add_argument("--extra", action="store_true", help="操作旁路服务(NapCat/AstrBot)")
    a = ap.parse_args(argv)

    cfg = core.Config()

    if a.action == "extras":
        _show_extras()
        return 0
    if a.action == "mode":
        return _switch(cfg, "mode", (a.names[0].lower() if a.names else ""), "mode")
    if a.action == "power":
        return _switch(cfg, "power_mode", (a.names[0].lower() if a.names else ""), "power")

    if a.extra:
        names = a.names or list(core.EXTRAS)
        bad = [n for n in names if n not in core.EXTRAS]
        if bad:
            print("未知旁路服务: %s (可选: %s)" % (", ".join(bad), " / ".join(core.EXTRAS)))
            return 2
        rc = 0
        for n in names:
            msg = core.extra_action(cfg, n, a.action)
            ok = "失败" not in msg
            rc = rc or (0 if ok else 1)
            print("  %s %s" % ("●" if ok else "○", msg))
        return rc

    names = a.names or list(core.SERVICE_MODULES)
    bad = [n for n in names if n not in core.SERVICE_MODULES]
    if bad:
        print("未知服务: %s (可选: %s)" % (", ".join(bad), " / ".join(core.SERVICE_MODULES)))
        return 2

    if a.action == "status":
        return _status(cfg, names)

    # ---- 整套实例的启停(带模式感知): 不给具体服务名时就是"整套" ----
    whole = not a.names
    if whole and a.action in ("start", "stop", "restart"):
        if a.action == "stop":
            print("  %s" % core.stop_instance(cfg))
            return 0
        if a.action == "restart":
            print("  %s" % core.stop_instance(cfg))
            time.sleep(1)
        msg = core.start_instance(cfg)
        ok = "失败" not in msg and "异常" not in msg
        print("  %s %s" % ("●" if ok else "○", msg))
        return 0 if ok else 1

    # ---- 指定了具体服务: 多进程模式下逐服务操作 ----
    rc = 0
    if core.instance_status(cfg)["mode"] == "single" and core.runtime_state().get("alive"):
        print("  ! 当前是单进程模式: 四个服务在同一个进程里, 只能整体 qqops restart")
        return 2
    if a.action == "restart":
        # 先全停(看门狗最先停, 否则它会把你正在停的服务又拉起来), 再全起(看门狗最后起)
        stop_order = [n for n in names if n == "watchdog"] + [n for n in names if n != "watchdog"]
        for n in stop_order:
            print("  ○ %s" % core.stop_service(n))
        time.sleep(1)
        start_order = [n for n in names if n != "watchdog"] + [n for n in names if n == "watchdog"]
        for n in start_order:
            msg = core.start_service(n)
            ok = "失败" not in msg and "异常" not in msg
            rc = rc or (0 if ok else 1)
            print("  %s %s" % ("●" if ok else "○", msg))
        return rc
    if a.action == "stop":
        names = [n for n in names if n == "watchdog"] + [n for n in names if n != "watchdog"]
    for n in names:
        if a.action == "start":
            msg = core.start_service(n)
        else:
            msg = core.stop_service(n)
        ok = "失败" not in msg and "异常" not in msg
        rc = rc or (0 if ok else 1)
        print("  %s %s" % ("●" if ok else "○", msg))
    return rc


if __name__ == "__main__":
    sys.exit(main())
