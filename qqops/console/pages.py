"""控制台接口(框架级) —— 应用状态 / 插件管理 / 运维(崩溃账本、日志流、任务) / 仪表盘。

所有接口统一返回 dict(由 webapp 包成 JSON), 异常由 webapp 的请求级守卫兜住,
所以这里只管把数据取出来 —— 取不到就返回部分数据 + `partial` 标记, 不抛给前端。
"""
from __future__ import annotations

import time

from .. import core
from ..framework import errors, log as fw_log

__all__ = ["register"]


def register(app) -> list:
    """把框架级控制台接口注册到应用路由表; 返回注册的路径列表。"""
    R = app.router
    paths = []

    def add(path, fn, methods=("GET",), auth=True, title=""):
        R.add(path, fn, methods=methods, auth=auth, plugin_id="", meta={"title": title})
        paths.append(path)
        return fn

    # ------------------------------------------------------------ 应用与框架
    def app_status(req, ctx):
        return app.status()

    def app_handlers(req, ctx):
        return {"counts": app.registry.counts(), "handlers": app.registry.summary()}

    def app_routes(req, ctx):
        return app.router.state()

    def app_pipeline(req, ctx):
        return {"stages": app.pipeline.describe() if app.pipeline else [],
                "bus": app.bus.state() if app.bus else {}}

    def app_log(req, ctx):
        """实时日志流: 前端带上上次的 seq 拉增量。"""
        seq = int(req.q("since") or 0)
        out = fw_log.BROKER.since(seq, module=req.q("module"), level=req.q("level"),
                                  limit=int(req.q("limit") or 300))
        out["modules"] = fw_log.BROKER.modules()[:30]
        out["counts"] = fw_log.BROKER.counts()
        return out

    def app_crashes(req, ctx):
        limit = int(req.q("limit") or 50)
        return {"stats": errors.stats(), "items": errors.recent(limit)}

    def app_crashes_clear(req, ctx):
        return {"ok": True, "cleared": errors.ledger().clear()}

    def app_errors_test(req, ctx):
        """故意抛一个错 —— 用来验证"异常会被账本收走"这条链路(自查用)。"""
        data = req.json() if hasattr(req, "json") else {}
        raise RuntimeError("这是一条自检异常: %s" % (data.get("note") or "manual"))

    def app_selftest(req, ctx):
        """一键自检: 给控制台的"诊断"用, 每项独立降级, 不影响其它项。"""
        out = {"ts": int(time.time()), "version": core.VERSION, "items": []}

        def item(name, ok, detail=""):
            out["items"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})

        cfg = ctx.cfg
        try:
            snap = core.status_snapshot(cfg)
            for name, row in (snap.get("services") or {}).items():
                item("服务 %s" % name, row.get("running"), row.get("health", {}).get("why", "")
                     if isinstance(row.get("health"), dict) else "")
        except Exception as e:
            item("服务状态", False, e)
        try:
            st = core.Store().stats(1)
            item("数据库", st.get("total", 0) > 0, "%s 条消息" % st.get("total"))
        except Exception as e:
            item("数据库", False, e)
        try:
            ok, detail = core.Store().check_integrity()
            item("数据库完整性", ok, detail)
        except Exception as e:
            item("数据库完整性", False, e)
        try:
            pst = app.plugins.state() if app.plugins else {}
            item("插件", pst.get("failed", 0) == 0,
                 "共 %s 个, 失败 %s 个" % (pst.get("total", 0), pst.get("failed", 0)))
        except Exception as e:
            item("插件", False, e)
        try:
            cs = errors.stats()
            item("异常账本", cs.get("total", 0) < 50, "%s 次" % cs.get("total", 0))
        except Exception as e:
            item("异常账本", False, e)
        try:
            from .. import media
            item("媒体目录", True, str(getattr(media, "MEDIA_DIR", "")))
        except Exception as e:
            item("媒体目录", False, e)
        out["ok"] = all(i["ok"] for i in out["items"])
        return out

    # ------------------------------------------------------------ 插件
    def plugins_state(req, ctx):
        mgr = app.plugins
        if mgr is None:
            return {"total": 0, "plugins": [], "error": "插件系统未初始化"}
        st = mgr.state()
        st["commands"] = mgr.commands()
        st["pages"] = mgr.pages()
        st["tasks"] = mgr.tasks()
        # 每个插件带上配置字段数, 方便前端显示"可配置"
        for row in st.get("plugins", []):
            try:
                row["config_fields"] = len(mgr.config_view(row["id"])["fields"])
            except Exception:
                row["config_fields"] = 0
        return st

    def plugin_config(req, ctx):
        pid = req.q("id")
        if not pid:
            return {"ok": False, "error": "缺少 id"}
        return {"ok": True, **app.plugins.config_view(pid)}

    def plugin_config_save(req, ctx):
        data = req.json() if hasattr(req, "json") else {}
        pid = str(data.get("id") or "")
        patch = data.get("config") or {}
        if not pid:
            return {"ok": False, "error": "缺少 id"}
        ok, errs, cfg = app.plugins.set_config(pid, patch)
        return {"ok": ok, "errors": errs, "config": cfg}

    def plugin_toggle(req, ctx):
        data = req.json() if hasattr(req, "json") else {}
        pid = str(data.get("id") or "")
        on = bool(data.get("enabled", True))
        if not pid:
            return {"ok": False, "error": "缺少 id"}
        st = app.plugins.toggle(pid, on)
        return {"ok": st in ("loaded", "disabled"), "status": st, "id": pid}

    def plugin_reload(req, ctx):
        data = req.json() if hasattr(req, "json") else {}
        pid = str(data.get("id") or "")
        if pid:
            return {"ok": True, "id": pid, "status": app.plugins.reload(pid)}
        for p in list(app.plugins.plugins.keys()):
            app.plugins.reload(p)
        return {"ok": True, "status": "reloaded_all"}

    def plugin_command(req, ctx):
        data = req.json() if hasattr(req, "json") else {}
        return app.plugins.run_command(str(data.get("name") or ""),
                                       data.get("args") or {}, ctx)

    # ------------------------------------------------------------ 任务
    def tasks_state(req, ctx):
        mgr = app.plugins
        rows = mgr.tasks() if mgr else []
        st = {}
        try:
            for t in rows:
                meta = (t.get("every") and "每 %s 秒" % t["every"]) or \
                       (t.get("cron") and "cron %s" % t["cron"]) or "-"
                st[t["task"]] = {"schedule": meta, "last": t.get("last_run") or 0}
        except Exception:
            pass
        return {"tasks": rows, "detail": st}

    def task_run(req, ctx):
        data = req.json() if hasattr(req, "json") else {}
        name = str(data.get("task") or "")
        rows = [r for r in app.registry.handlers("task") if r.name == name] if name else []
        if not rows:
            return {"ok": False, "error": "没有这个任务: %s" % name}
        return app.plugins.run_task(rows[0], ctx)

    # ------------------------------------------------------------ 仪表盘
    def dashboard(req, ctx):
        """首页要的一切: 健康、消息趋势、报告、异常、提醒。一次取回, 失败项降级为空。"""
        out = {"ts": int(time.time()), "version": core.VERSION, "partial": []}

        def safe(key, fn, default):
            try:
                out[key] = fn()
            except Exception as e:                       # noqa: BLE001
                out[key] = default
                out["partial"].append({"key": key, "error": "%s: %s" % (type(e).__name__, e)})

        store = core.Store()
        snap = {}

        def _snapshot():
            nonlocal snap
            snap = core.status_snapshot(ctx.cfg)
            return snap
        safe("snapshot", _snapshot, {})
        if isinstance(snap, dict):                   # 把快照里的键摊平, 前端少一层嵌套
            for k, v in snap.items():
                out.setdefault(k, v)
        safe("stats", lambda: store.stats(24), {})
        safe("runs", lambda: store.runs(5), [])
        safe("events", lambda: store.event_stats(), {})
        safe("crashes", lambda: errors.stats(), {})
        safe("plugins", lambda: (app.plugins.state() if app.plugins else {}), {})
        safe("bus", lambda: (app.bus.state() if app.bus else {}), {})
        safe("schedule", lambda: core.next_run(ctx.cfg.data.get("schedule") or {}), {})
        safe("reports", lambda: sorted(
            [f for f in __import__("os").listdir(core.REPORT_DIR) if f.endswith(".md")],
            reverse=True)[:5], [])
        safe("media_pending", lambda: len([m for m in store.media_list(200)
                                           if m.get("status") not in ("ok", "done")]), 0)
        return out

    # ------------------------------------------------------------ 资源与功耗
    def resources(req, ctx):
        """内存/线程/进程 + 当前功耗档位。手机上看"合并进程到底省了多少"。"""
        cfg = ctx.cfg
        inst = core.instance_status(cfg)
        prof = inst["power"]
        procs = core.qqops_processes()
        svc_rows = []
        seen = set()
        for n, s in (inst["services"] or {}).items():
            if n == "napcat":
                continue
            svc_rows.append({"name": n, "running": bool(s.get("running")), "pid": s.get("pid") or 0,
                             "rss": s.get("rss") or 0, "port": s.get("port") or 0,
                             "mode": s.get("mode") or inst["mode"]})
            if s.get("pid"):
                seen.add(int(s["pid"]))
        total = sum(p.get("rss") or 0 for p in procs)
        single = next((p.get("rss") or 0 for p in procs if "qqops.runner" in (p.get("cmd") or "")), 0)
        est = {}
        if inst["mode"] == "single":
            est = {"note": "当前已是单进程: 四个服务共享一个解释器, 不再重复占用内存",
                   "current": total, "multi_equivalent": max(total * 3, total + 3 * 25 * 1048576)}
        else:
            est = {"note": "切成单进程大约能省下一个解释器的份额(每进程 ≈25MB)",
                   "current": total, "single_expected": max(total - 3 * 25 * 1048576, 25 * 1048576)}
        return {"runtime": {k: inst.get(k) for k in ("mode", "running_mode", "pid", "alive", "rss")},
                "power": prof, "services": svc_rows, "processes": procs,
                "totals": {"rss": total, "count": len(procs), "unique_pids": len(seen)},
                "estimate": est, "self_rss": core.self_rss(),
                "threads": (ctx.bus and ctx.bus.state().get("workers") or 0),
                "profiles": prof.get("all_profiles") or {}}

    def power_set(req, ctx):
        """切换运行模式 / 功耗档位, 然后重启实例使其生效。

        注意: 控制台进程本身就是被重启的对象 —— 所以**先把配置写好并回复**,
        再用分离进程执行重启, 前端延迟刷新页面。
        """
        data = req.json() if hasattr(req, "json") else {}
        patch = {}
        if str(data.get("mode") or "") in ("auto", "single", "multi"):
            patch["mode"] = str(data["mode"])
        if str(data.get("power_mode") or "") in ("performance", "balanced", "saver"):
            patch["power_mode"] = str(data["power_mode"])
        if not patch:
            return {"ok": False, "error": "没有可应用的改动(可选 mode / power_mode)"}
        cfg = core.Config()
        cfg.set_values({"runtime": patch})
        new_cfg = core.Config()
        out = {"ok": True, "applied": patch,
               "power": core.power_profile(new_cfg),
               "runtime_mode": core.resolve_mode(new_cfg)}
        if data.get("restart", True):
            out["restart"] = _spawn_restart()
        else:
            out["restart"] = "已保存(未重启, 下次启动生效)"
        return out

    def _spawn_restart() -> str:
        """分离式重启实例: 不在当前进程里 stop(会把回复也带走)。"""
        base = core._base_dir()
        import subprocess
        import sys
        script = os.path.join(base, "bin", "qqops")
        try:
            if os.path.isfile(script) and os.name != "nt":
                subprocess.Popen(["bash", script, "restart"], cwd=base,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
                return "已触发重启(bin/qqops restart)"
        except Exception as e:                       # noqa: BLE001
            errors.record("console.restart", exc=e, kind="restart_spawn_failed")
        try:
            subprocess.Popen([sys.executable, "-m", "qqops.svc", "restart"], cwd=base,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            return "已触发重启(python -m qqops.svc restart)"
        except Exception as e:                       # noqa: BLE001
            errors.record("console.restart", exc=e, kind="restart_spawn_failed")
            return "触发重启失败: %s" % e

    # ------------------------------------------------------------ 注册
    add("/api/app/status", app_status, title="应用状态")
    add("/api/app/handlers", app_handlers, title="处理器清单")
    add("/api/app/routes", app_routes, title="接口清单")
    add("/api/app/pipeline", app_pipeline, title="管线状态")
    add("/api/app/log", app_log, title="实时日志")
    add("/api/app/crashes", app_crashes, title="异常账本")
    add("/api/app/crashes/clear", app_crashes_clear, methods=("POST",), title="清空账本")
    add("/api/app/selftest", app_selftest, title="一键自检")
    add("/api/app/errors_test", app_errors_test, methods=("POST",), title="故意报错(验证账本)")
    add("/api/plugins", plugins_state, title="插件列表")
    add("/api/plugins/config", plugin_config, title="插件配置")
    add("/api/plugins/config/save", plugin_config_save, methods=("POST",), title="保存插件配置")
    add("/api/plugins/toggle", plugin_toggle, methods=("POST",), title="启用/停用插件")
    add("/api/plugins/reload", plugin_reload, methods=("POST",), title="重载插件")
    add("/api/plugins/command", plugin_command, methods=("POST",), title="执行插件命令")
    add("/api/ops/tasks", tasks_state, title="任务列表")
    add("/api/ops/tasks/run", task_run, methods=("POST",), title="立即执行任务")
    add("/api/ops/resources", resources, title="资源与功耗")
    add("/api/ops/power", power_set, methods=("POST",), title="切换模式/档位")
    add("/api/ops/dashboard", dashboard, title="仪表盘")
    return paths
