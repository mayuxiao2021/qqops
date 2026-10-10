"""QQOps 工具集: 初始化 / 旧数据导入 / 自检 / 备份 / 清理。

用法:
  python3 -m qqops.tools init
  python3 -m qqops.tools doctor
  python3 -m qqops.tools import --db <旧库路径> [--config <旧配置>]
  python3 -m qqops.tools backup
  python3 -m qqops.tools prune --days 30
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys

from . import core


def cmd_init(_a) -> int:
    core.ensure_dirs()
    cfg = core.Config()
    core.Store()
    errs = cfg.validate()
    print("数据目录 : %s" % core.DATA)
    print("配置文件 : %s" % core.CONF_PATH)
    print("数据库   : %s" % core.DB_PATH)
    print("访问令牌 : %s" % cfg.data["web"]["token"])
    if errs:
        print("配置告警 : %s" % "; ".join(errs))
    print("初始化完成 ✅")
    return 0


def _device_keepalive() -> list:
    """手机保活体检: 幽灵进程查杀 / 电池优化 / 后台权限 / 唤醒锁 / 开机自启。

    注意: Termux 应用**没有**权限读这些系统设置(settings/dumpsys 都会失败),
    所以在手机里跑 doctor 只能报"本机测不到"; 真正的核对要在 PC 上用 adb 做。
    这里绝不把"读不到"写成"未设置" —— 那会平白吓人一跳。
    """
    rows = []

    def sh(cmd) -> str:
        return core._run(cmd, 8).strip()

    def unreadable(out: str) -> bool:
        low = out.lower()
        return (not out) or any(k in low for k in
                                ("exception", "permission", "denied", "can't", "cannot",
                                 "not found", "unknown", "error"))

    v = sh(["settings", "get", "global", "settings_enable_monitor_phantom_procs"])
    if v in ("0", "false"):
        rows.append(("OK ", "幽灵进程查杀", "已关闭 —— 不会因为后台进程数超限被杀"))
    elif unreadable(v):
        rows.append(("?? ", "幽灵进程查杀", "本机测不到(Termux 无权读系统设置); PC 上核验: "
                                       "adb shell settings get global settings_enable_monitor_phantom_procs "
                                       "(应为 false)"))
    else:
        rows.append(("!! ", "幽灵进程查杀", "启用中(%s), Android 12+ 会杀后台进程 → PC 上执行: "
                                       "adb shell settings put global settings_enable_monitor_phantom_procs false" % v))

    wl = sh(["dumpsys", "deviceidle", "whitelist"])
    if "com.termux" in wl:
        rows.append(("OK ", "电池优化白名单", "com.termux 已加入"))
    elif unreadable(wl):
        rows.append(("?? ", "电池优化白名单", "本机测不到(Termux 无权读 dumpsys); PC 上核验: "
                                            "adb shell dumpsys deviceidle whitelist | findstr termux"))
    else:
        rows.append(("!! ", "电池优化白名单", "com.termux 未加入 → PC 上执行: "
                                             "adb shell dumpsys deviceidle whitelist +com.termux"))

    ops = sh(["cmd", "appops", "get", "com.termux", "RUN_ANY_IN_BACKGROUND"])
    if "allow" in ops:
        rows.append(("OK ", "后台运行权限", "允许"))
    elif unreadable(ops):
        rows.append(("?? ", "后台运行权限", "本机测不到; PC 上执行: "
                                          "adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow"))
    else:
        rows.append(("!! ", "后台运行权限", "当前为 %s → PC 上执行: "
                                          "adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow" % ops[:40]))

    if shutil.which("termux-wake-lock"):
        out = core._run(["termux-wake-lock"], 10).strip()
        if unreadable(out) and out:
            rows.append(("!! ", "唤醒锁", "termux-wake-lock 调用失败(通常是没装 Termux:API 应用): %s" % out[:60]))
        else:
            rows.append(("?? ", "唤醒锁", "已调用; 但要装了 Termux:API 应用才真正生效"
                                        "(adb 侧的保活设置更关键)"))
    else:
        rows.append(("?? ", "唤醒锁", "未安装 termux-wake-lock(可选: 装 Termux:API)"))

    boot = os.path.join(os.path.expanduser("~"), ".termux", "boot")
    if os.path.isdir(boot) and any(f.endswith(".sh") for f in os.listdir(boot)):
        rows.append(("OK ", "开机自启", "已写 %s 下的脚本(需安装 Termux:Boot 才会生效)" % boot))
    else:
        rows.append(("!! ", "开机自启", "未配置 —— 安装 Termux:Boot 后重跑 install.sh 即可自动写入"))
    return rows


def cmd_doctor(_a) -> int:
    core.ensure_dirs()
    cfg = core.Config()
    st = core.Store()
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %-22s %s" % ("OK " if cond else "!! ", name, detail))

    import platform
    print("环境")
    chk("python", sys.version_info >= (3, 9), sys.version.split()[0])
    chk("平台", True, platform.platform())
    chk("screen", shutil.which("screen") is not None, shutil.which("screen") or "未安装")
    print("配置")
    chk("配置文件", os.path.isfile(core.CONF_PATH), core.CONF_PATH)
    chk("校验", not cfg.validate(), "; ".join(cfg.validate()) or "通过")
    chk("令牌", bool(cfg.data["web"]["token"]), "已生成" if cfg.data["web"]["token"] else "缺失")
    print("存储")
    chk("数据库", os.path.isfile(core.DB_PATH), core.DB_PATH)
    chk("可写", os.access(core.DATA, os.W_OK), core.DATA)
    n = st.stats(24)
    chk("消息", n["total"] >= 0, "总计 %d 条, 近24h %d 条" % (n["total"], n["window"]))
    print("服务")
    inst = core.instance_status(cfg)
    prof = inst["power"]
    print("  [%-3s] %-22s %s" % ("OK " if inst["alive"] else "!! ", "运行形态",
                                "%s(配置 %s)%s" % (inst["mode"], (cfg.data.get("runtime") or {}).get("mode", "auto"),
                                                 " · 已运行 pid %s · %.1f MB" % (inst["pid"], inst["rss"] / 1048576.0)
                                                 if inst["alive"] else " · 未运行")))
    print("  [%-3s] %-22s %s" % ("OK ", "功耗档位",
                                "%s(%s): 调度 %ds / 主管 %ds / 控制台刷新 %ds / SQLite %dKB / 富化 %s"
                                % (prof["power_mode"], prof["label"], prof["scheduler_tick"],
                                   prof["watchdog_interval"], prof["console_refresh"],
                                   prof["sqlite_cache_kb"],
                                   "夜间窗口" if prof["defer_enrich"] else "收到就抓")))
    svc = inst["services"]
    for k, v in svc.items():
        chk(k, v["running"], "端口 %s" % (v["port"] or "-"))
    chk("采集口监听", core.port_open(cfg.data["collector"]["port"]),
        "127.0.0.1:%s" % cfg.data["collector"]["port"])
    print("NapCat")
    nh = core.napcat_from_config(cfg).health()
    chk("API", nh.get("ok"), json.dumps(nh, ensure_ascii=False))
    print("模型")
    sc = cfg.data["summarizer"]
    chk("BaseURL", str(sc["base_url"]).startswith("http"), sc["base_url"])
    chk("API Key", bool(sc.get("api_key")), "已配置" if sc.get("api_key") else "未配置(只能出统计块)")
    print("手机保活(进程为什么会被杀)")
    for tag, name, detail in _device_keepalive():
        print("  [%s] %-16s %s" % (tag.strip(), name, detail))
    print("\n结论: %s" % ("全部通过 ✅" if ok else "存在问题 ⚠️ (见上面 !!)"))
    return 0 if ok else 1


def cmd_import(a) -> int:
    core.ensure_dirs()
    cfg = core.Config()
    store = core.Store()
    imported = 0
    if a.db and os.path.isfile(a.db):
        src = sqlite3.connect(a.db)
        src.row_factory = sqlite3.Row
        try:
            cols = {r[1] for r in src.execute("PRAGMA table_info(messages)")}
            if not cols:
                print("旧库没有 messages 表, 跳过")
            else:
                con = store.conn()
                q = ("INSERT INTO messages(ts,message_type,group_id,user_id,nickname,card,message_id,"
                     "raw_message,segments,kinds) VALUES(?,?,?,?,?,?,?,?,?,?)")
                for r in src.execute("SELECT * FROM messages"):
                    con.execute(q, (r["ts"], r["message_type"], r["group_id"], r["user_id"],
                                    r["nickname"], r["card"] if "card" in cols else "",
                                    r["message_id"] if "message_id" in cols else 0,
                                    r["raw_message"], r["segments"] if "segments" in cols else "",
                                    r["kinds"] if "kinds" in cols else ""))
                    imported += 1
                con.commit()
                con.close()
                # 群名
                try:
                    for r in src.execute("SELECT id,name FROM groups"):
                        store.upsert_group(int(r["id"]), r["name"] or "")
                except Exception:
                    pass
        finally:
            src.close()
    if a.config and os.path.isfile(a.config):
        try:
            old = json.load(open(a.config, encoding="utf-8-sig"))
            patch = {}
            if old.get("api"):
                patch["summarizer"] = {"base_url": old["api"].get("base_url", cfg.data["summarizer"]["base_url"]),
                                       "api_key": old["api"].get("api_key", ""),
                                       "model": old["api"].get("model", cfg.data["summarizer"]["model"])}
            if old.get("groups"):
                gl = [int(g) for g in old["groups"]]
                patch.setdefault("summarizer", {})["groups"] = gl
                for gid in gl:                      # 同步勾选状态, 控制台即为准
                    store.upsert_group(gid, "", 1)
            if old.get("kinds"):
                patch.setdefault("summarizer", {})["kinds"] = old["kinds"]
            if old.get("window_hours"):
                patch.setdefault("summarizer", {})["window_hours"] = old["window_hours"]
            if old.get("push"):
                patch["push"] = {"enable": bool(old["push"].get("enable")),
                                 "user_id": int(old["push"].get("user_id") or 0),
                                 "group_id": int(old["push"].get("group_id") or 0),
                                 "napcat_api_port": int(old["push"].get("napcat_api_port") or 3000),
                                 "token": old["push"].get("token", "")}
            if patch:
                cfg.set_values(patch)
                print("已导入旧配置字段: %s" % ", ".join(patch.keys()))
        except Exception as e:
            print("旧配置导入失败: %s" % e)
    print("导入消息 %d 条" % imported)
    return 0


def cmd_backup(_a) -> int:
    core.ensure_dirs()
    st = core.Store()
    p = st.backup(core.BACKUP_DIR)
    cfg_copy = os.path.join(core.BACKUP_DIR, "config-%s.json" % core.now())
    shutil.copy(core.CONF_PATH, cfg_copy)
    print("备份完成:\n  %s\n  %s" % (p, cfg_copy))
    return 0


def cmd_prune(a) -> int:
    n = core.Store().prune(int(a.days))
    print("已清理 %d 条超过 %s 天的消息" % (n, a.days))
    return 0


def main(argv=None) -> int:
    core.ensure_utf8_console()   # 非 UTF-8 控制台下也能安全打印中文
    ap = argparse.ArgumentParser(prog="qqops.tools", description="QQOps 工具集")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("doctor").set_defaults(func=cmd_doctor)
    p = sub.add_parser("import")
    p.add_argument("--db")
    p.add_argument("--config")
    p.set_defaults(func=cmd_import)
    sub.add_parser("backup").set_defaults(func=cmd_backup)
    p = sub.add_parser("prune")
    p.add_argument("--days", type=int, required=True)
    p.set_defaults(func=cmd_prune)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
