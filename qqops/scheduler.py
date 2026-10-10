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

from . import core
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
    from . import summarizer          # 懒加载: 只有真到点总结时才需要这几个大模块
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


def tick_once(cfg: core.Config = None, store: core.Store = None, app=None,
              now_str: str = None) -> dict:
    """跑一轮调度: 判断是否到点总结 / 维护窗口 / 插件的定时任务。

    返回本轮动作摘要(便于测试与日志)。**永不抛异常** —— 单进程模式下它跑在线程里,
    一旦抛出会把整个进程带走, 所以这里逐项隔离 + 记账。
    """
    cfg = cfg or core.Config()
    store = store or core.Store()
    out = {"beat": core.now(), "summary": "", "maintenance": False, "tasks": []}
    core.beat("scheduler", out["beat"])
    now_str = now_str or time.strftime("%H:%M")
    sch = cfg.data.get("schedule", {})

    try:
        ok, label = _due(cfg, store, now_str)
        if ok:
            last = int(core.state_get("last_summary_ts", 0) or 0)
            new_msgs = store.count_since(last) if last else 10 ** 9
            need = int(sch.get("min_new_messages", 5) or 0)
            if sch.get("skip_if_no_new", True) and new_msgs < need:
                core.log("scheduler", "INFO", "跳过本次(%s): 距上次只有 %d 条新消息(<%d)"
                         % (label, new_msgs, need))
                rid = store.run_start("schedule:%s" % label)
                store.run_finish(rid, "skipped", "无新消息可总结(%d 条 < %d 条)" % (new_msgs, need))
                out["summary"] = "skipped:%s" % label
            else:
                core.log("scheduler", "INFO", "到点触发总结: %s(新消息 %d 条)" % (label, new_msgs))
                _run_summary(cfg, store, label, new_msgs)
                out["summary"] = label
    except Exception as e:                       # noqa: BLE001
        errors.record("scheduler.summary", exc=e, kind="summary_failed")
        core.log("scheduler", "ERROR", "总结流程异常: %s" % e)

    try:
        if now_str == MAINT_AT and _last_run("ran_maint") != _today():
            _mark("ran_maint")
            _do_maintenance(cfg, store)
            out["maintenance"] = True
    except Exception as e:                       # noqa: BLE001
        errors.record("scheduler.maintain", exc=e, kind="maintenance_failed")

    try:
        if app is not None:
            for res in app.run_due_tasks():
                out["tasks"].append({"task": res.get("task"), "ok": res.get("ok")})
    except Exception as e:                       # noqa: BLE001
        errors.record("scheduler.tasks", exc=e, kind="task_loop_error")
    return out


def loop(stop=None, tick: int = None, app=None) -> None:
    """调度循环。`stop` 为 threading.Event(单进程 runner 用); 为 None 时永不退出。

    间隔每轮重读配置: 在控制台改"定时/功耗档位"后, 不用重启就能生效。
    """
    core.ensure_dirs()
    store = core.Store()
    while True:
        try:
            cfg = core.Config()
            t = int(tick or power_tick(cfg))
            tick_once(cfg, store, app)
        except Exception as e:                       # noqa: BLE001 —— 循环绝不能死
            errors.record("scheduler.loop", exc=e, kind="loop_error")
            core.log("scheduler", "ERROR", "循环异常: %s" % e)
            t = 30
        if stop is not None:
            if stop.wait(max(1, int(t))):
                return
        else:
            time.sleep(max(1, int(t)))


def power_tick(cfg: core.Config = None) -> int:
    """当前档位下的调度间隔(秒)。"""
    try:
        return int((core.power_profile(cfg).get("scheduler_tick") or 15))
    except Exception:
        return 15


def main(argv=None) -> int:
    core.ensure_dirs()
    cfg = core.Config()
    core.apply_power_profile(cfg)
    core.log("scheduler", "INFO", "调度器启动 (version %s, 档位 %s, 间隔 %ds)"
             % (core.VERSION, core.power_profile(cfg)["power_mode"], power_tick(cfg)))
    print("[scheduler] started", flush=True)
    store = core.Store()
    # 框架装配: 调度进程只跑 tasks 角色(插件的定时任务在这里执行)
    app = None
    try:
        app = QQOpsApp(name="scheduler", roles=("tasks",), cfg=core.Config(),
                       store=store).setup().start()
    except Exception as e:                       # noqa: BLE001
        errors.record("scheduler.app", exc=e, kind="setup_failed")
        core.log("scheduler", "ERROR", "框架装配失败(只跑内置定时): %s" % e)
    loop(tick=power_tick(cfg), app=app)
    return 0


if __name__ == "__main__":
    sys.exit(main())
