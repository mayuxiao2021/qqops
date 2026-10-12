"""停机补采 —— 把"采集器不在时"漏掉的消息从 OneBot 历史接口补回库里。

为什么需要它
------------
NapCat 的 HTTP 上报是**尽力而为**: 采集器没在监听的那一次 POST 失败就失败了,
NapCat 既不重试也不排队 —— 那段时间的消息在库里就是一个空洞。

实测(2026-10-09 ~ 10-12 三天, NapCat 日志 vs 消息库按 message_id 逐条比对):
NapCat 共上报 18167 条, 最初有 1046 条(5.9%)在库里找不到 —— 其中

  * 252 条是机器人自己发的(NapCat 配了 `reportSelfMessage: false`, 本就不该来);
  * 其余落在采集器停机区间内(最长一次 34 分钟, 一次丢了 479 条)与反复重启的部署窗口;
  * **"采集器收下了却没入库" = 0 条** —— 运行期采集链路不丢消息。

补采把能回捞的都捞了回来(深挖那次入库 18424 条, 含装 QQOps 之前的历史), 未匹配降到
394 条(2.2%), 其中真正漏掉的只剩 114 条(0.6%)—— 都是 NapCat 本地历史也已被清理的部分。

好在 NapCat 本地保留着聊天历史(`get_group_msg_history` / `get_friend_msg_history`),
所以恢复后可以把缺口补回来。入库复用采集器同一条 `insert_message` 路径,
(group_id, message_id) 唯一索引保证**幂等**: 重复补采不会写重, 也不会覆盖已有内容。

用法
----
自动: 单进程/多进程启动后由 `runner` 在后台补一次(受 `collector.backfill*` 配置控制)。
手动: `qqops backfill [--minutes 720] [--dry-run]`, 或控制台「运维 → 存储 → 停机补采」。
"""

from __future__ import annotations

import threading
import time

from . import core
from .framework import errors as fwerrors

__all__ = ["run", "start_async", "status", "holes", "main", "LOCK", "STATE"]

LOCK = threading.Lock()
STATE = {"running": False, "started_at": 0, "finished_at": 0, "minutes": 0,
         "dry_run": False, "result": None, "error": ""}
MAX_COUNT = 1000          # NapCat 单页回看条数上限(实测 1000 仍能返回)
DEEP_PAGES = 20           # 深挖模式最多翻多少页


def status() -> dict:
    """当前/最近一次补采的状态(给控制台轮询)。"""
    with LOCK:
        st = dict(STATE)
    if not st.get("result"):
        saved = core.state_get("backfill", None)
        if saved:
            st["result"] = saved
    return st


def _log(level: str, msg: str) -> None:
    core.log("backfill", level, msg)


# ---------------------------------------------------------------- 缺口识别
def holes(store: core.Store, minutes: int = 720, min_gap: int = 45,
          now: int = 0, extra_groups=None) -> dict:
    """找出"最后一条消息距今超过 min_gap 秒"的群和私聊对象 —— 这些就是候选缺口。

    注意: 群里本来就很安静也会被算成缺口(我们无法区分"没人说话"和"消息丢了"),
    所以补采是**拉一页历史做对比**, 拿回来没有新消息就说明本来就没消息, 不影响数据。
    """
    now = int(now or time.time())
    since = now - int(minutes) * 60
    con = store.conn()
    try:
        groups = {}
        for r in con.execute("SELECT group_id g, MAX(ts) last FROM messages "
                             "WHERE message_type='group' AND group_id>0 GROUP BY g"):
            groups[int(r["g"])] = int(r["last"] or 0)
        users = {}
        for r in con.execute("SELECT user_id u, MAX(ts) last FROM messages "
                             "WHERE message_type<>'group' AND user_id>0 GROUP BY u"):
            users[int(r["u"])] = int(r["last"] or 0)
    finally:
        con.close()
    for g in store.enabled_groups():                       # 关注群即使最近没消息也要看
        groups.setdefault(int(g), 0)
    for g in (extra_groups or []):                         # 配置里点名关注的群也一样
        try:
            groups.setdefault(int(g), 0)
        except Exception:                                  # noqa: BLE001
            continue
    gap_groups = {g: t for g, t in groups.items() if now - t >= int(min_gap)}
    gap_users = {u: t for u, t in users.items() if now - t >= int(min_gap)}
    return {"groups": gap_groups, "users": gap_users, "since": since, "now": now}


# ---------------------------------------------------------------- 补采主流程
def _fetch(napcat: core.NapCat, kind: str, target: int, last: int, since: int,
           count: int, max_pages: int = 4, deep: bool = False) -> tuple[list, int, bool]:
    """一页页往回捞, 直到够到该够到的地方 / 没有更早的消息 / 页数上限。

    够到哪儿:
      * 平时(启动后自动补采)只够到 `last`(库里已有的最新一条) —— 停机造成的空洞一定在
        尾巴上, 回看到最后一条已有消息就够了, 通常一页就停;
      * `deep=True` 时够到 `since`(窗口起点) —— 用来修**夹在中间的老空洞**
        (比如某次停机之后的消息都补齐了, 但空洞还留在几天前的中间)。

    返回 (消息列表[老→新], 用掉的页数, 是否摸到底)。
    """
    target_ts = since if deep else max(last, since)
    got: list = []
    pages, reached, seq, prev_oldest = 0, False, 0, 0
    while pages < max(1, int(max_pages)):
        page = (napcat.group_history(target, count, seq, bool(seq)) if kind == "group"
                else napcat.friend_history(target, count, seq, bool(seq)))
        if not page:
            reached = True
            break
        pages += 1
        got = list(page) + got                    # 页内是老→新, 新页拼在前面
        # 注意: 历史里偶尔会有 time 缺失/为 0 的条目(系统消息之类), 不能拿它当"最老",
        # 否则会误判成"已经翻到窗口起点"而提前收工(实测踩过: 有些群只翻了一页就停)。
        timed = [m for m in page if int(m.get("time") or 0) > 0]
        if not timed:
            break
        oldest = min(timed, key=lambda m: int(m.get("time") or 0))
        oldest_ts = int(oldest.get("time") or 0)
        if oldest_ts <= target_ts:
            reached = True
            break
        if oldest_ts >= prev_oldest and seq:
            # 私聊历史实测不支持 reverseOrder: 再要还是同一页。发现没有前进就收工,
            # 否则会把同一页要 60 次(浪费几分钟还报"未够到底")。
            break
        prev_oldest = oldest_ts
        try:
            seq = int(oldest.get("message_seq") or oldest.get("message_id") or 0)
        except Exception:                          # noqa: BLE001
            break
        if not seq:
            break
    return got, pages, reached

def run(cfg: core.Config = None, store: core.Store = None, *, minutes: int = 0,
        groups=None, users=None, dry_run: bool = False, count: int = 0,
        include_self: bool = None, budget: float = 300.0, napcat=None,
        deep: bool = False, max_pages: int = 0) -> dict:
    """补采入口。返回统计字典(也写进 `state.json` 的 `backfill` 键)。"""
    cfg = cfg or core.Config()
    store = store or core.Store()
    col = cfg.data.get("collector") or {}
    minutes = int(minutes or col.get("backfill_minutes", 720))
    count = int(count or col.get("backfill_count", 100))
    count = max(10, min(MAX_COUNT, count))
    max_pages = int(max_pages or col.get("backfill_max_pages", 4))
    if deep:
        max_pages = max(max_pages, DEEP_PAGES)
    min_gap = int(col.get("backfill_min_gap", 45))
    if include_self is None:
        include_self = bool(col.get("backfill_include_self", False))
    if napcat is None:
        napcat = core.napcat_from_config(cfg)

    started = time.time()
    try:
        self_id = int((napcat.login_info() or {}).get("user_id") or 0)
    except Exception as e:                                  # noqa: BLE001
        msg = "NapCat API 不可用, 补采跳过: %s" % e
        _log("WARN", msg)
        return {"ok": False, "error": msg, "seconds": 0}

    plan = holes(store, minutes=minutes, min_gap=min_gap,
                 extra_groups=(cfg.data.get("summarizer") or {}).get("groups") or [])
    todo_g = {int(g): plan["groups"].get(int(g), 0) for g in (groups or plan["groups"])}
    todo_u = {int(u): plan["users"].get(int(u), 0) for u in (users or plan["users"])}
    since = plan["since"]
    # 试运行: 只统计"本该补多少条"。必须拿库里的 (群, message_id) 对一遍 —— 否则会把
    # 已经有的也算成"该补", 报出来的数字会大得离谱(实测把 309 条里 300 条已有的都算上了)。
    existing = set()
    if dry_run:
        con = store.conn()
        try:
            existing = {(int(g or 0), int(m)) for g, m in con.execute(
                "SELECT coalesce(group_id,0), message_id FROM messages WHERE message_id IS NOT NULL")}
        finally:
            con.close()
    stat = {"ok": True, "started_at": int(started), "minutes": minutes, "dry_run": bool(dry_run),
            "deep": bool(deep), "self_id": self_id, "groups": len(todo_g), "users": len(todo_u),
            "fetched": 0, "inserted": 0, "duplicate": 0, "self_skipped": 0,
            "would_insert": 0, "outside_window": 0, "incomplete": [], "errors": [],
            "seconds": 0, "scanned": 0, "new_groups": 0}
    _log("INFO", "开始补采: %d 个群 / %d 个私聊, 往回 %d 分钟, 每次取 %d 条%s%s"
         % (len(todo_g), len(todo_u), minutes, count, "(深挖到窗口起点)" if deep else "",
            "(试运行, 不落库)" if dry_run else ""))

    def handle(kind: str, target: int, last: int, pages: int = 0) -> None:
        if time.time() - started > budget:
            stat["incomplete"].append("%s:%s(超时)" % (kind, target))
            return
        try:
            msgs, used, reached = _fetch(napcat, kind, target, last, since, count,
                                         max_pages=pages or max_pages, deep=deep)
        except Exception as e:                              # noqa: BLE001
            stat["errors"].append("%s:%s %s" % (kind, target, e))
            return
        stat["scanned"] += 1
        stat["fetched"] += len(msgs)
        if not reached and pages != 1:
            stat["incomplete"].append("%s:%s(%d 页未够到底)" % (kind, target, used))
        # 逐会话记账: 补采没补全时, 靠这条才能看出是"翻页没够到"还是"历史里就没有"
        oldest = min([int(m.get("time") or 0) for m in msgs if int(m.get("time") or 0) > 0] or [0])
        _log("INFO", "  %s %s: %d 页 %d 条, 最老 %s%s"
             % ("群" if kind == "group" else "私聊", target, used, len(msgs),
                time.strftime("%m-%d %H:%M", time.localtime(oldest)) if oldest else "-",
                "" if reached else "(未够到底)"))
        for m in msgs:
            try:
                ts = int(m.get("time") or 0)
                uid = int(m.get("user_id") or 0)
            except Exception:                               # noqa: BLE001
                continue
            # 只按时间窗过滤: **不能**用"比库里最新一条还新"来过滤 —— 采集器恢复后先收到
            # 新消息、补采才跑的情况下, 缺口里的消息反而比库里最新的要旧, 会被整段跳过。
            # 已有的重复条交给 (group_id, message_id) 唯一索引拦(INSERT OR IGNORE)。
            if ts < since:
                stat["outside_window"] += 1
                continue
            if uid == self_id and not include_self:
                stat["self_skipped"] += 1
                continue
            ev = dict(m)
            ev.setdefault("post_type", "message")
            ev["self_id"] = self_id
            ev["platform"] = ev.get("platform") or "aiocqhttp"
            if kind == "group":
                ev["message_type"] = "group"
                ev["group_id"] = target
            else:
                ev["message_type"] = "private"
                ev["user_id"] = target
            if dry_run:
                try:
                    key = (int(ev.get("group_id") or 0), int(ev.get("message_id") or 0))
                except Exception:                           # noqa: BLE001
                    key = (0, 0)
                if key in existing:                         # 已经有的: 真跑也只算重复
                    stat["duplicate"] += 1
                else:
                    stat["would_insert"] += 1
                continue
            try:
                res = store.insert_message(ev)
            except Exception as e:                          # noqa: BLE001
                stat["errors"].append("%s:%s 落库失败 %s" % (kind, target, e))
                continue
            if res == "inserted":
                stat["inserted"] += 1
                if kind == "group":
                    store.upsert_group(target)
            elif res == "duplicate":
                stat["duplicate"] += 1

    for g, last in sorted(todo_g.items(), key=lambda kv: kv[1]):
        handle("group", g, last)
    for u, last in sorted(todo_u.items(), key=lambda kv: kv[1]):
        handle("private", u, last)
    # 库里完全没有的群(我们刚被拉进去): 补一页最近的, 只算"窗口内"的那些
    new_groups = []
    if not groups and col.get("backfill_new_groups", True):
        try:
            live = {int(g["id"]) for g in napcat.group_list()}
            con = store.conn()
            try:
                have = {int(r[0]) for r in con.execute(
                    "SELECT DISTINCT group_id FROM messages WHERE group_id>0")}
            finally:
                con.close()
            new_groups = sorted(live - have - set(todo_g))
        except Exception as e:                              # noqa: BLE001 —— 拿不到群列表不影响补采
            stat["errors"].append("get_group_list: %s" % e)
        for g in new_groups:
            handle("group", g, since, pages=1)
        stat["new_groups"] = len(new_groups)

    stat["seconds"] = round(time.time() - started, 2)
    stat["finished_at"] = int(time.time())
    lvl = "WARN" if stat["errors"] else "INFO"
    _log(lvl, "补采结束: 扫描 %d 个会话, 取回 %d 条, 入库 %d 条(重复 %d), 跳过自己 %d 条, "
              "没用上 %d 条, 耗时 %.1fs%s"
         % (stat["scanned"], stat["fetched"], stat["inserted"], stat["duplicate"],
            stat["self_skipped"], stat["outside_window"], stat["seconds"],
            (", 失败 %d 个" % len(stat["errors"])) if stat["errors"] else ""))
    for e in stat["errors"][:5]:
        _log("WARN", "  补采失败: %s" % e)
    try:
        core.state_set("backfill", {k: stat[k] for k in
                                    ("finished_at", "minutes", "groups", "users", "fetched",
                                     "inserted", "duplicate", "self_skipped", "seconds")})
    except Exception:                                       # noqa: BLE001
        pass
    return stat


# ---------------------------------------------------------------- 后台/CLI
def start_async(cfg: core.Config = None, store: core.Store = None, **kw) -> dict:
    """后台补采(不阻塞调用方, 同时只允许一个在跑)。"""
    with LOCK:
        if STATE["running"]:
            return {"ok": False, "error": "已有补采在运行", "started_at": STATE["started_at"]}
        STATE.update({"running": True, "started_at": int(time.time()),
                      "finished_at": 0, "result": None, "error": "",
                      "minutes": int(kw.get("minutes") or 0), "dry_run": bool(kw.get("dry_run"))})

    def work():
        try:
            res = run(cfg, store, **kw)
            with LOCK:
                STATE["result"] = res
                STATE["error"] = "" if res.get("ok") else str(res.get("error") or "")
        except Exception as e:                              # noqa: BLE001
            fwerrors.record("backfill.run", exc=e, kind="backfill_failed")
            with LOCK:
                STATE["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            with LOCK:
                STATE["running"] = False
                STATE["finished_at"] = int(time.time())

    threading.Thread(target=work, name="qqops-backfill", daemon=True).start()
    return {"ok": True, "started": True}


def main(argv=None) -> int:
    """命令行: python -m qqops.backfill [--minutes 720] [--dry-run] [--group 123] [--quiet]"""
    import argparse
    ap = argparse.ArgumentParser(prog="qqops backfill", description="补采停机期间漏掉的消息")
    ap.add_argument("--minutes", type=int, default=0, help="往回补多久(默认取配置, 720 分钟)")
    ap.add_argument("--count", type=int, default=0, help="每页取多少条历史(默认取配置, 100)")
    ap.add_argument("--pages", type=int, default=0, help="每个会话最多往回翻几页(默认取配置, 4)")
    ap.add_argument("--budget", type=float, default=0, help="最多跑多少秒(默认 300, 到点就停并如实报告)")
    ap.add_argument("--group", type=int, action="append", default=[], help="只补指定群(可重复)")
    ap.add_argument("--dry-run", action="store_true", help="只统计不落库")
    ap.add_argument("--deep", action="store_true",
                    help="深挖到窗口起点(修夹在中间的老空洞, 每个会话最多翻 %d 页)" % DEEP_PAGES)
    ap.add_argument("--quiet", action="store_true", help="只打印一行结果")
    a = ap.parse_args(argv)
    res = run(minutes=a.minutes, count=a.count, groups=a.group or None, dry_run=a.dry_run,
              deep=a.deep, max_pages=a.pages, budget=(a.budget or 300.0))
    if not res.get("ok"):
        print("补采失败:", res.get("error"))
        return 1
    if a.quiet:
        print("补采: 入库 %d 条(重复 %d, 跳过自己 %d, 耗时 %.1fs)"
              % (res["inserted"], res["duplicate"], res["self_skipped"], res["seconds"]))
    else:
        print("补采完成%s" % ("(深挖)" if res.get("deep") else ""))
        print("  扫描会话    : %d 个群 / %d 个私聊(其中新群 %d 个)"
              % (res["groups"], res["users"], res.get("new_groups", 0)))
        if res["dry_run"]:
            print("  取回 / 入库 : %d 条 / 0 条(试运行; 本该补回 %d 条, 重复 %d)"
                  % (res["fetched"], res["would_insert"], res["duplicate"]))
        else:
            print("  取回 / 入库 : %d 条 / %d 条(重复 %d)" % (res["fetched"], res["inserted"],
                                                              res["duplicate"]))
        print("  跳过        : 自己发的 %d 条, 窗口外 %d 条" % (res["self_skipped"],
                                                              res["outside_window"]))
        print("  耗时        : %.1fs" % res["seconds"])
        if res["incomplete"]:
            print("  未取到底    : %d 个会话(缺口太长, 可加大 --minutes/--count 再跑一次)"
                  % len(res["incomplete"]))
        for e in res["errors"][:5]:
            print("  失败        : %s" % e)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
