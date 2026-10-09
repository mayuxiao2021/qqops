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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qqops.svc", description="QQOps 服务进程管理")
    ap.add_argument("action", choices=["start", "stop", "restart", "status", "extras"])
    ap.add_argument("names", nargs="*", help="collector / scheduler / web, 或旁路服务名")
    ap.add_argument("--extra", action="store_true", help="操作旁路服务(NapCat/AstrBot)")
    a = ap.parse_args(argv)

    cfg = core.Config()

    if a.action == "extras":
        _show_extras()
        return 0

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
        st = core.service_status(cfg)
        for n in names:
            s = st.get(n, {})
            port = ("  端口 %s" % s["port"]) if s.get("port") else ""
            pid = ("  PID %s" % s["pid"]) if s.get("pid") else ""
            print("  %s %-10s%s%s" % ("●" if s.get("running") else "○", n, port, pid))
        return 0

    rc = 0
    for n in names:
        if a.action == "start":
            msg = core.start_service(n)
        elif a.action == "stop":
            msg = core.stop_service(n)
        else:
            core.stop_service(n)
            time.sleep(1)
            msg = core.start_service(n)
        ok = "失败" not in msg
        rc = rc or (0 if ok else 1)
        print("  %s %s" % ("●" if ok else "○", msg))
    return rc


if __name__ == "__main__":
    sys.exit(main())
