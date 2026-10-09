"""QQOps Scheduler —— 定时总结 + 数据维护。

特性:
  * 每 15 秒轮询一次配置(改配置无需重启);
  * 支持多个每日时间点(schedule.times);
  * 同一时间点当天只跑一次(状态写 state.json, 重启不重复跑);
  * 顺带做保留策略清理与每日备份, 备份数量按 retention.backup_keep 滚动;
  * 心跳写入数据库, 控制台可据此判断调度器是否存活。
"""
from __future__ import annotations

import os
import sys
import time

from . import core, summarizer

TICK = 15
MAINT_AT = "03:30"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _last_run(key: str) -> str:
    return str(core.state_get(key, "") or "")


def _mark(key: str) -> None:
    core.state_set(key, _today())


def _do_maintenance(cfg: core.Config, store: core.Store) -> None:
    days = int(cfg.data.get("retention", {}).get("message_days", 0) or 0)
    if days > 0:
        n = store.prune(days)
        core.log("scheduler", "INFO", "保留策略: 清理 %d 条超过 %d 天的消息" % (n, days))
    keep = int(cfg.data.get("retention", {}).get("backup_keep", 7) or 7)
    try:
        path = store.backup(core.BACKUP_DIR)
        core.atomic_write(os.path.join(core.BACKUP_DIR, "config-%s.json" % time.strftime("%Y%m%d")),
                          open(core.CONF_PATH, encoding="utf-8").read())
        core.log("scheduler", "INFO", "备份完成: %s" % path)
        files = sorted([f for f in os.listdir(core.BACKUP_DIR) if f.startswith("messages-")])
        for f in files[:-keep] if keep > 0 else []:
            os.remove(os.path.join(core.BACKUP_DIR, f))
    except Exception as e:
        core.log("scheduler", "ERROR", "备份失败: %s" % e)


def main(argv=None) -> int:
    core.ensure_dirs()
    core.log("scheduler", "INFO", "调度器启动 (version %s)" % core.VERSION)
    print("[scheduler] started", flush=True)
    store = core.Store()

    while True:
        try:
            cfg = core.Config()
            store = core.Store()
            core.state_set("scheduler_beat", core.now())
            now = time.strftime("%H:%M")
            sch = cfg.data.get("schedule", {})
            if sch.get("enabled", True):
                for t in (sch.get("times") or []):
                    key = "ran_%s" % t
                    if now == t and _last_run(key) != _today():
                        _mark(key)
                        core.log("scheduler", "INFO", "到点触发总结: %s" % t)
                        res = summarizer.run(hours=float(sch.get("hours", 24) or 24),
                                             job="schedule:%s" % t)
                        core.log("scheduler", "INFO", "定时总结结束: %s" % res.get("detail", res.get("status")))
            if now == MAINT_AT and _last_run("ran_maint") != _today():
                _mark("ran_maint")
                _do_maintenance(cfg, store)
        except Exception as e:
            core.log("scheduler", "ERROR", "循环异常: %s" % e)
        time.sleep(TICK)


if __name__ == "__main__":
    sys.exit(main())
