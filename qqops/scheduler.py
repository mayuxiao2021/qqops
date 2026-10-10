"""QQOps Scheduler —— 定时总结(每天定点 / 每隔 N 分钟 / 每周几) + 数据维护。

特性:
  * 每 15 秒轮询配置(改配置无需重启);
  * 三种调度模式, 每次运行后记录 last_summary_ts, 供"下次时间"与"有无新消息"判断;
  * skip_if_no_new: 自上次运行以来新消息不足 min_new_messages 就跳过(不发空报告, 不花模型钱);
  * 同一时间点当天只跑一次(状态写 state.json, 重启不重复跑);
  * 顺带做保留策略清理与每日备份, 备份数量按 retention.backup_keep 滚动;
  * 心跳写入数据库, 控制台可据此判断调度器是否存活。
"""
from __future__ import annotations

import os
import sys
import time

from . import core, summarizer
from .framework import errors
from .framework.app import QQOpsApp

TICK = 15
MAINT_AT = "03:30"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _last_run(key: str) -> str:
    return str(core.state_get(key, "") or "")


def _mark(key: str) -> None:
    core.state_set(key, _today())


def _due(cfg: core.Config, store: core.Store, now_str: str) -> tuple:
    """判断此刻是否该跑。返回 (是否运行, 触发标签)。"""
    sch = cfg.data.get("schedule", {})
    if not sch.get("enabled", True):
        return False, ""
    mode = str(sch.get("mode") or "daily").lower()
    if mode == "interval":
        every = max(1, int(sch.get("every_minutes") or 360)) * 60
        last = int(core.state_get("last_summary_ts", 0) or 0) or core.now()
        if core.now() - last >= every:
            return True, "每隔 %d 分钟" % (every // 60 or 1)
        return False, ""
    if mode == "weekly":
        wds = [int(x) for x in (sch.get("weekdays") or [1, 2, 3, 4, 5])]
        wd = time.localtime().tm_wday + 1
        at = str(sch.get("weekly_time") or "22:00")
        key = "ran_weekly"
        if wd in wds and now_str >= at and _last_run(key) != _today():
            _mark(key)
            return True, "每周 %s %s" % (wd, at)
        return False, ""
    for t in (sch.get("times") or []):
        key = "ran_%s" % t
        if now_str == t and _last_run(key) != _today():
            _mark(key)
            return True, "每天 %s" % t
    return False, ""


def _do_maintenance(cfg: core.Config, store: core.Store) -> None:
    days = int(cfg.data.get("retention", {}).get("message_days", 0) or 0)
    if days > 0:
        n = store.prune(days)
        core.log("scheduler", "INFO", "保留策略: 清理 %d 条超过 %d 天的消息" % (n, days))
    keep = int(cfg.data.get("retention", {}).get("backup_keep", 7) or 7)
    try:
        path = store.backup(core.BACKUP_DIR)
        with open(core.CONF_PATH, encoding="utf-8") as f:
            core.atomic_write(os.path.join(core.BACKUP_DIR, "config-%s.json" % time.strftime("%Y%m%d")), f.read())
        core.log("scheduler", "INFO", "备份完成: %s" % path)
        files = sorted([f for f in os.listdir(core.BACKUP_DIR) if f.startswith("messages-")])
        for f in (files[:-keep] if keep > 0 else []):
            os.remove(os.path.join(core.BACKUP_DIR, f))
    except Exception as e:
        errors.record("scheduler.backup", exc=e, kind="backup_failed")
        core.log("scheduler", "ERROR", "备份失败: %s" % e)


def _run_summary(cfg: core.Config, store: core.Store, label: str, new_msgs: int) -> None:
    """跑一次总结。失败要有清晰记录, 且不能让调度循环崩掉。"""
    try:
        res = summarizer.run(job="schedule:%s" % label)
        core.state_set("last_summary_ts", core.now())
        msg = res.get("detail", res.get("status"))
        core.log("scheduler", "INFO", "定时总结结束: %s" % msg)
        if res.get("status") not in ("ok", "skipped"):
            errors.record("scheduler.summary", kind="summary_bad_status",
                          message="总结状态 %s: %s" % (res.get("status"), msg))
    except Exception as e:                       # noqa: BLE001 —— 一次失败不能停掉调度
        errors.record("scheduler.summary", exc=e, kind="summary_failed",
                      extra={"label": label, "new_msgs": new_msgs})
        core.log("scheduler", "ERROR", "定时总结失败: %s" % e)


def main(argv=None) -> int:
    core.ensure_dirs()
    core.log("scheduler", "INFO", "调度器启动 (version %s)" % core.VERSION)
    print("[scheduler] started", flush=True)
    store = core.Store()
    # 框架装配: 调度进程只跑 tasks 角色(插件的定时任务在这里执行)
    try:
        app = QQOpsApp(name="scheduler", roles=("tasks",), cfg=core.Config(),
                       store=store).setup().start()
    except Exception as e:                       # noqa: BLE001
        errors.record("scheduler.app", exc=e, kind="setup_failed")
        app = None
        core.log("scheduler", "ERROR", "框架装配失败(只跑内置定时): %s" % e)

    while True:
        try:
            cfg = core.Config()
            store = core.Store()
            core.state_set("scheduler_beat", core.now())
            now_str = time.strftime("%H:%M")
            sch = cfg.data.get("schedule", {})

            ok, label = _due(cfg, store, now_str)
            if ok:
                last = int(core.state_get("last_summary_ts", 0) or 0)
                new_msgs = store.count_since(last) if last else 10 ** 9
                need = int(sch.get("min_new_messages", 5) or 0)
                if sch.get("skip_if_no_new", True) and new_msgs < need:
                    core.log("scheduler", "INFO", "跳过本次(%s): 距上次只有 %d 条新消息(<%d)" % (label, new_msgs, need))
                    rid = store.run_start("schedule:%s" % label)
                    store.run_finish(rid, "skipped", "无新消息可总结(%d 条 < %d 条)" % (new_msgs, need))
                else:
                    core.log("scheduler", "INFO", "到点触发总结: %s(新消息 %d 条)" % (label, new_msgs))
                    _run_summary(cfg, store, label, new_msgs)
            if now_str == MAINT_AT and _last_run("ran_maint") != _today():
                _mark("ran_maint")
                _do_maintenance(cfg, store)
            # 插件的定时任务(到点才跑, 失败只记账)
            if app is not None:
                app.run_due_tasks()
        except Exception as e:
            errors.record("scheduler.loop", exc=e, kind="loop_error")
            core.log("scheduler", "ERROR", "循环异常: %s" % e)
        time.sleep(TICK)


if __name__ == "__main__":
    sys.exit(main())
