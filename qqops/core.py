"""QQOps 核心层: 配置 / 存储 / NapCat 客户端 / 服务管理 / 日志。

设计约束:
  * 纯标准库, 可在 Termux 原生 Python 上直接运行;
  * 所有写操作原子化(临时文件 + rename), 避免断电/被杀进程写坏配置;
  * 配置带默认值与校验, 未知键保留(向前兼容);
  * 日志按大小滚动。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import secrets

VERSION = "3.0.0"

# 路径解析(踩过坑, 别改回 "靠 $HOME 猜"):
#   曾经用 BASE = ~/qqops —— 但 $HOME 是环境给的, 从别的目录/cron/adb run-as 调用时
#   $HOME 可能指向别处, 于是程序会在一个新位置**另建一套空数据**, 控制台看起来"消息 0 条"。
#   现在按优先级: QQOPS_HOME > 代码所在仓库根目录 > ~/qqops。
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # 仓库根目录
HOME = os.path.expanduser("~")
BASE = os.environ.get("QQOPS_HOME") or _HERE or os.path.join(HOME, "qqops")
DATA = os.environ.get("QQOPS_DATA") or os.path.join(BASE, "data")
CONF_PATH = os.path.join(DATA, "config.json")
DB_PATH = os.path.join(DATA, "messages.db")
LOG_DIR = os.path.join(DATA, "logs")
REPORT_DIR = os.path.join(DATA, "reports")
BACKUP_DIR = os.path.join(DATA, "backups")
STATE_PATH = os.path.join(DATA, "state.json")

SCREEN_COLLECTOR = "qqops-collector"
SCREEN_SCHEDULER = "qqops-scheduler"
SCREEN_WEB = "qqops-web"

# ---------------------------------------------------------------- 默认配置
DEFAULT_CONFIG = {
    "version": VERSION,
    "web": {"host": "127.0.0.1", "port": 8091, "token": "", "session_hours": 12, "auto_refresh": 15},
    "collector": {
        "host": "127.0.0.1", "port": 8080, "secret": "", "require_signature": False,
        "groups": [], "kinds": [], "store_private": False, "debug": False,
    },
    "summarizer": {
        "base_url": "https://api.deepseek.com/v1", "api_key": "", "model": "deepseek-chat",
        "temperature": 0.3, "timeout": 180,
        "groups": [], "kinds": ["text", "image", "reply", "at", "json"],
        "window_hours": 24, "min_messages": 3, "max_messages": 600,
        "max_chars_per_group": 12000, "prompt_extra": "",
        "input_price_per_mtok": 0.0, "output_price_per_mtok": 0.0,
    },
    "push": {"enable": False, "user_id": 0, "group_id": 0, "napcat_api_port": 3000,
             "token": "", "max_chars": 1200, "report_link": True},
    "schedule": {
        "enabled": True,
        "mode": "daily",             # daily=每天定点 | interval=每隔 N 分钟 | weekly=每周几
        "times": ["22:00"],          # mode=daily: 每日时间点(可多个)
        "every_minutes": 360,        # mode=interval: 间隔分钟数
        "weekdays": [1, 2, 3, 4, 5], # mode=weekly: 1=周一 … 7=周日
        "weekly_time": "22:00",      # mode=weekly: 当天的时刻
        "skip_if_no_new": True,      # 自上次运行以来没有新消息就跳过(不发空报告)
        "min_new_messages": 5,       # 至少多少条新消息才值得跑
    },
    "retention": {"message_days": 0, "log_max_mb": 4, "backup_keep": 7},
    "watchdog": {"enabled": True, "interval": 60, "manage_extras": False},
    "extract": {
        "structured": True,          # LLM 输出结构化 JSON(事件/待办/链接/事实/记忆)
        "max_events": 8, "max_todos": 8, "max_links": 6, "memory_chars": 600,
        "low_confidence_drop": 0.35,  # 低于此置信度的事件不入库
    },
    "enrich": {
        "vision": {"enable": True, "model": "", "max_images": 8, "max_bytes": 3_000_000, "detail": "简要"},
        "files": {"enable": True, "max_mb": 8, "max_files": 3, "max_chars": 4000},
        "links": {"enable": True, "max_links": 5, "max_bytes": 512_000, "timeout": 12,
                  "max_redirects": 3, "allow_all": False},
    },
    "extras": {},   # 按设备覆盖 NapCat/AstrBot 的启动命令(见 core.EXTRAS)
}
SECRET_KEYS = {("web", "token"), ("collector", "secret"),
               ("summarizer", "api_key"), ("push", "token")}
# 个人档案: 由用户维护, 每次总结都会作为背景注入(目标/偏好/长期计划)
PROFILE_PATH = os.path.join(DATA, "profile.md")
PROFILE_DEFAULT = """# 个人档案(每次总结都会带上, 请按自己的情况修改)

## 我是谁 / 在做什么
- (例: 大二学生, 主要关注社团活动、课程作业、比赛)

## 我关心的群
- (例: 九重天迎新群 = 社团头像与模板事务; 班级群 = 通知与截止日)

## 长期目标
- (例: 6 月前完成社团招新视觉方案)

## 输出偏好
- 我只要"与我有关的"信息, 与我无关的闲聊可以略过
- 有截止日的务必列出日期
"""
# 链接抓取黑名单(SSRF 防护): 命中即拒绝, 不解析、不请求
LINK_BLOCK = ("localhost", "127.", "0.0.0.0", "10.", "192.168.", "172.16.", "172.17.",
              "172.18.", "172.19.", "172.2", "172.30.", "172.31.", "169.254.", "[::1]",
              "file://", "ftp://", "gopher://", "data:")


def ensure_dirs() -> None:
    for d in (DATA, LOG_DIR, REPORT_DIR, BACKUP_DIR, RUN_DIR):
        os.makedirs(d, exist_ok=True)
    _clean_legacy_pidfiles()


def _clean_legacy_pidfiles() -> None:
    """清掉旧版本遗留的 `qqops.<模块>.pid`。

    现在的 PID 文件是 `run/<服务名>.pid`(见 pid_path); 旧版本按模块名写过一批,
    它们指向早已死掉(且可能被复用)的 PID。stop_service 不会读它们, 但留着会误导排查,
    所以每次初始化目录时顺手删掉。
    """
    try:
        for f in os.listdir(RUN_DIR):
            if f.startswith("qqops.") and f.endswith(".pid"):
                try:
                    os.remove(os.path.join(RUN_DIR, f))
                except Exception:
                    pass
    except Exception:
        pass


def atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = None, os.path.join(d, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def gen_token(n: int = 24) -> str:
    return "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(n))


class Config:
    """配置读写(带默认值合并 / 校验 / 密钥掩码)。"""

    def __init__(self, path: str = CONF_PATH):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self) -> dict:
        raw = {}
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8-sig") as f:
                    raw = json.load(f) or {}
            except Exception as e:  # 配置损坏时保留现场, 用默认值继续
                broken = self.path + ".broken-%d" % int(time.time())
                try:
                    shutil.copy(self.path, broken)
                except Exception:
                    pass
                log("core", "ERROR", "配置解析失败(%s), 已备份到 %s" % (e, broken))
                raw = {}
        self.data = deep_merge(DEFAULT_CONFIG, raw)
        if self.data.get("version") != VERSION:
            self.data["version"] = VERSION      # 让控制台显示的版本与实际一致
            self.save()
        if not self.data["web"].get("token"):
            self.data["web"]["token"] = gen_token()
            self.save()
        self.migrate()
        return self.data

    def migrate(self) -> list:
        """清理重复/废弃的配置项(去重), 返回改动说明。

        已知重复:
          * schedule.hours 与 summarizer.window_hours 是同一个"时间窗" → 合并到后者;
          * 旧版本遗留的 schedule 键(如 times 之外的东西) → 删除。
        """
        notes = []
        sch = self.data.setdefault("schedule", {})
        if "hours" in sch:
            try:
                old = float(sch.pop("hours"))
            except Exception:
                old = None
            cur = float(self.data["summarizer"].get("window_hours", 24) or 24)
            if old and abs(cur - 24) < 0.01:
                self.data["summarizer"]["window_hours"] = old
                notes.append("把重复的 schedule.hours=%s 合并为 summarizer.window_hours" % old)
            else:
                notes.append("删除重复的 schedule.hours(时间窗以 summarizer.window_hours 为准)")
        for k in [k for k in list(sch) if k not in DEFAULT_CONFIG["schedule"]]:
            sch.pop(k, None)
            notes.append("删除无用的 schedule.%s" % k)
        if notes:
            self.save()
            log("core", "INFO", "配置去重: " + "; ".join(notes))
        return notes

    def save(self) -> None:
        self.data["version"] = VERSION
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")

    def validate(self) -> list:
        errs = []
        w = self.data.get("web", {})
        if not isinstance(w.get("port"), int) or not (1 <= w["port"] <= 65535):
            errs.append("web.port 必须是 1-65535 的整数")
        c = self.data.get("collector", {})
        if not isinstance(c.get("port"), int) or not (1 <= c["port"] <= 65535):
            errs.append("collector.port 必须是 1-65535 的整数")
        s = self.data.get("summarizer", {})
        if s.get("api_key") and not str(s["base_url"]).startswith("http"):
            errs.append("summarizer.base_url 必须以 http(s):// 开头")
        try:
            if float(s.get("window_hours", 24)) <= 0:
                errs.append("summarizer.window_hours 必须为正数")
        except Exception:
            errs.append("summarizer.window_hours 必须是数字")
        if int(self.data.get("push", {}).get("napcat_api_port", 3000)) <= 0:
            errs.append("push.napcat_api_port 非法")
        for t in (self.data.get("schedule", {}).get("times") or []):
            if not isinstance(t, str) or len(t) != 5 or t[2] != ":":
                errs.append("schedule.times 元素必须是 HH:MM 格式: %r" % t)
        return errs

    def masked(self) -> dict:
        out = json.loads(json.dumps(self.data))
        for sec, key in SECRET_KEYS:
            v = out.get(sec, {}).get(key) or ""
            out[sec][key] = ("*" * max(0, len(v) - 4) + v[-4:]) if v else ""
        return out

    def set_values(self, patch: dict) -> None:
        """按点号路径或嵌套 dict 覆盖配置; 掩码值('****')忽略。"""
        for sec, kv in (patch or {}).items():
            if not isinstance(kv, dict):
                continue
            tgt = self.data.setdefault(sec, {})
            for k, v in kv.items():
                if isinstance(v, str) and v.startswith("*") and ("*" * 4) in v:
                    continue  # 前端回传的掩码, 不覆盖真实密钥
                tgt[k] = v
        self.save()


def redact_text(text: str) -> str:
    """密钥脱敏(日志/账本落盘前用)。只在可疑时才走正则, 避免给热路径添负担。"""
    s = str(text or "")
    low = s.lower()
    if any(k in low for k in ("sk-", "token", "key", "bearer", "password", "secret")):
        try:
            from .framework.errors import redact
            return redact(s)
        except Exception:
            return s
    return s


def log(name: str, level: str, msg: str, max_mb: float = 4.0) -> None:
    """按名字写日志文件, 超过 max_mb 时轮转一次。

    同时广播给控制台的实时日志流(见 framework/log.py), 这样"运维→日志"页
    可以按游标拉增量, 不用反复读文件。
    """
    msg = redact_text(msg)
    try:
        from .framework import log as _blog          # 延迟导入: 避免与框架层形成导入环
        _blog.publish(name, level, msg)
    except Exception:
        pass
    try:
        ensure_dirs()
        path = os.path.join(LOG_DIR, "%s.log" % name)
        if os.path.isfile(path) and os.path.getsize(path) > max_mb * 1024 * 1024:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg))
    except Exception:
        pass


def tail(path: str, lines: int = 200) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, max(4096, lines * 200))
            f.seek(size - block)
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])
    except Exception as e:
        return "(读取失败: %s)" % e


# ---------------------------------------------------------------- 存储层
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        message_type TEXT, group_id INTEGER, user_id INTEGER,
        nickname TEXT, card TEXT, message_id INTEGER,
        raw_message TEXT, segments TEXT, kinds TEXT,
        platform TEXT DEFAULT 'aiocqhttp')""",
    "CREATE INDEX IF NOT EXISTS idx_msg_group_ts ON messages(group_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts)",
    "CREATE INDEX IF NOT EXISTS idx_msg_kinds ON messages(kinds)",
    """CREATE TABLE IF NOT EXISTS groups(
        id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER DEFAULT 0, updated_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, started_at INTEGER, finished_at INTEGER,
        status TEXT, detail TEXT, report TEXT, tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0, cost REAL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at)",
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)",
    # ---- 结构化产出: 事件 / 待办 / 链接 / 媒体 ----
    """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedup TEXT UNIQUE, kind TEXT, title TEXT NOT NULL, detail TEXT,
        who TEXT, group_id INTEGER, due_at INTEGER, status TEXT DEFAULT 'open',
        priority INTEGER DEFAULT 2, confidence REAL DEFAULT 0,
        source_ids TEXT, origin TEXT DEFAULT 'auto',
        first_seen INTEGER, last_seen INTEGER, notes TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_events_group ON events(group_id)",
    """CREATE TABLE IF NOT EXISTS memories(
        scope TEXT, key TEXT, content TEXT, updated_at INTEGER, hits INTEGER DEFAULT 0,
        PRIMARY KEY(scope, key))""",
    """CREATE TABLE IF NOT EXISTS links(
        url TEXT PRIMARY KEY, title TEXT, domain TEXT, group_id INTEGER,
        first_seen INTEGER, last_seen INTEGER, status TEXT DEFAULT 'new',
        fetched_at INTEGER, excerpt TEXT, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS media(
        key TEXT PRIMARY KEY, group_id INTEGER, message_id INTEGER, kind TEXT,
        name TEXT, url TEXT, path TEXT, size INTEGER DEFAULT 0,
        caption TEXT, caption_at INTEGER, status TEXT DEFAULT 'new', error TEXT,
        first_seen INTEGER)""",
]

# 独立(非 external-content)的 FTS5 表: 便于把群名一并索引; 数据由 Python 侧同步
FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(raw_message, nickname, group_name, tokenize='unicode61')"


class Store:
    """SQLite 存储(消息 / 群 / 任务运行历史 / 元数据 / 全文索引)。"""

    def __init__(self, path: str = DB_PATH):
        ensure_dirs()
        self.path = path
        self.fts_ok = False
        self._init()

    def conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=5000")      # 多进程同时写时先等一会儿再报错
        except Exception:
            pass
        return con

    def check_integrity(self) -> tuple:
        """快速自检数据库(手机断电/被杀进程后有可能留下损坏页)。

        返回 `(是否完好, 说明)`; 损坏时记账, 控制台能在异常账本里看到。
        """
        try:
            con = self.conn()
            try:
                row = con.execute("PRAGMA quick_check").fetchone()
                detail = row[0] if row else "?"
            finally:
                con.close()
            ok = str(detail).lower() in ("ok", "1")
            if not ok:
                try:
                    from .framework import errors
                    errors.record("store.integrity", kind="db_corrupt",
                                  message="数据库自检未通过: %s" % str(detail)[:200],
                                  extra={"db": self.path})
                except Exception:
                    pass
                log("core", "ERROR", "数据库自检未通过: %s" % str(detail)[:200])
            return ok, str(detail)
        except Exception as e:
            return False, "自检失败: %s" % e

    def _init(self) -> None:
        con = self.conn()
        try:
            for sql in SCHEMA:
                con.execute(sql)
            try:
                con.execute(FTS_SCHEMA)
                self.fts_ok = True
            except Exception as e:          # 没有 FTS5 的 SQLite 就退回 LIKE 检索
                self.fts_ok = False
                log("core", "WARN", "FTS5 不可用, 退化为 LIKE 检索: %s" % e)
            con.commit()
        finally:
            con.close()
        if self.fts_ok:
            self.fts_ensure(con_factory=self.conn)

    def fts_ensure(self, con_factory=None) -> int:
        """索引缺失/落后时重建(首次迁移或索引被清空)。返回补齐的条数。"""
        if not self.fts_ok:
            return 0
        con = (con_factory or self.conn)()
        try:
            n_msg = con.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
            n_fts = con.execute("SELECT COUNT(*) c FROM messages_fts").fetchone()["c"]
            if n_fts >= n_msg:
                return 0
            con.execute("DELETE FROM messages_fts")
            rows = con.execute(
                """SELECT m.id, m.raw_message, m.nickname, COALESCE(g.name,'') gname
                   FROM messages m LEFT JOIN groups g ON g.id=m.group_id""").fetchall()
            con.executemany("INSERT INTO messages_fts(rowid,raw_message,nickname,group_name) VALUES(?,?,?,?)",
                            [(r["id"], fts_text(r["raw_message"]), fts_text(r["nickname"]),
                              fts_text(r["gname"])) for r in rows])
            con.commit()
            log("core", "INFO", "FTS 索引已重建: %d 条" % len(rows))
            return len(rows)
        except Exception as e:
            log("core", "ERROR", "FTS 重建失败: %s" % e)
            return 0
        finally:
            con.close()

    # -- 消息
    def insert_message(self, ev: dict) -> bool:
        """落库一条消息。遇到 SQLite 忙锁会退避重试, 避免偶发锁冲突丢消息。"""
        last = None
        for attempt in range(3):
            try:
                return self._insert_once(ev)
            except sqlite3.OperationalError as e:
                last = e
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise last

    def _insert_once(self, ev: dict) -> bool:
        mtype = ev.get("message_type")
        gid = int(ev.get("group_id") or 0)
        sd = ev.get("sender") or {}
        msg = ev.get("message")
        kinds = kinds_of(msg)
        raw = ev.get("raw_message") or ""
        nick = sd.get("nickname") or ""
        con = self.conn()
        try:
            cur = con.execute(
                "INSERT INTO messages(ts,message_type,group_id,user_id,nickname,card,message_id,"
                "raw_message,segments,kinds,platform) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (int(ev.get("time") or time.time()), mtype, gid, int(ev.get("user_id") or 0),
                 nick, sd.get("card") or "", int(ev.get("message_id") or 0),
                 raw, json.dumps(msg, ensure_ascii=False), kinds,
                 ev.get("platform") or "aiocqhttp"))
            if self.fts_ok:
                row = con.execute("SELECT name FROM groups WHERE id=?", (gid,)).fetchone()
                try:
                    con.execute("INSERT INTO messages_fts(rowid,raw_message,nickname,group_name) "
                                "VALUES(?,?,?,?)",
                                (cur.lastrowid, fts_text(raw), fts_text(nick),
                                 fts_text((row["name"] if row else "") or "")))
                except Exception as e:
                    log("core", "WARN", "FTS 写入失败: %s" % e)
            con.commit()
            return True
        finally:
            con.close()

    def stats(self, hours: int = 24) -> dict:
        since = int(time.time()) - hours * 3600
        con = self.conn()
        try:
            total = con.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
            window = con.execute("SELECT COUNT(*) c FROM messages WHERE ts>=?", (since,)).fetchone()["c"]
            hour_ago = con.execute("SELECT COUNT(*) c FROM messages WHERE ts>=?",
                                   (int(time.time()) - 3600,)).fetchone()["c"]
            groups = con.execute("SELECT COUNT(DISTINCT group_id) c FROM messages").fetchone()["c"]
            series = [0] * 24
            for r in con.execute("SELECT ts FROM messages WHERE ts>=?", (since,)):
                idx = int((r["ts"] - since) // 3600)
                if 0 <= idx < 24:
                    series[idx] += 1
            kinds = {r["kinds"]: r["c"] for r in con.execute(
                "SELECT kinds, COUNT(*) c FROM messages WHERE ts>=? GROUP BY kinds ORDER BY c DESC LIMIT 8", (since,))}
            top = [dict(r) for r in con.execute(
                """SELECT m.group_id, COALESCE(g.name,'') name, COUNT(*) c, MAX(m.ts) last
                   FROM messages m LEFT JOIN groups g ON g.id=m.group_id
                   WHERE m.ts>=? GROUP BY m.group_id ORDER BY c DESC LIMIT 10""", (since,))]
            active = [dict(r) for r in con.execute(
                """SELECT nickname, COUNT(*) c FROM messages WHERE ts>=? AND nickname<>''
                   GROUP BY nickname ORDER BY c DESC LIMIT 5""", (since,))]
            return {"total": total, "window": window, "last_hour": hour_ago, "groups": groups,
                    "series": series, "kinds": kinds, "top_groups": top, "active_users": active,
                    "generated_at": int(time.time()), "window_hours": hours}
        finally:
            con.close()

    def messages(self, group: str = "", kind: str = "", q: str = "", hours: int = 24,
                 page: int = 1, size: int = 50) -> dict:
        size = max(1, min(500, int(size)))
        page = max(1, int(page))
        where, args = ["m.ts>=?"], [int(time.time()) - int(hours) * 3600]
        mode = "recent"
        if group:
            where.append("m.group_id=?")
            args.append(int(group))
        if kind:
            where.append("m.kinds LIKE ?")
            args.append("%" + kind + "%")
        if q:
            mq = fts_query(q) if self.fts_ok else ""
            if mq:
                where.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
                args.append(mq)
                mode = "fts"
            else:                      # 无 FTS5 或查询词为空时退回 LIKE
                where.append("(m.raw_message LIKE ? OR m.nickname LIKE ?)")
                args += ["%" + q + "%", "%" + q + "%"]
                mode = "like"
        w = " AND ".join(where)
        con = self.conn()
        try:
            total = con.execute("SELECT COUNT(*) c FROM messages m WHERE " + w, args).fetchone()["c"]
            rows = [dict(r) for r in con.execute(
                """SELECT m.id,m.ts,m.group_id,COALESCE(g.name,'') group_name,m.user_id,m.nickname,
                          m.raw_message,m.kinds
                   FROM messages m LEFT JOIN groups g ON g.id=m.group_id
                   WHERE %s ORDER BY m.ts DESC LIMIT ? OFFSET ?""" % w,
                args + [size, (page - 1) * size])]
            return {"rows": rows, "total": total, "page": page, "size": size,
                    "pages": max(1, (total + size - 1) // size),
                    "mode": mode, "fts_available": self.fts_ok}
        finally:
            con.close()

    def count_since(self, ts: int) -> int:
        """统计某时刻之后的消息数(调度器用来判断"有没有新消息值得总结")。"""
        con = self.conn()
        try:
            return con.execute("SELECT COUNT(*) c FROM messages WHERE ts>?", (int(ts),)).fetchone()["c"]
        finally:
            con.close()

    def fts_count(self) -> int:
        con = self.conn()
        try:
            return con.execute("SELECT COUNT(*) c FROM messages_fts").fetchone()["c"] if self.fts_ok else 0
        finally:
            con.close()

    def rebuild_fts(self) -> int:
        """强制重建全文索引(控制台"重建索引"按钮/迁移后使用)。"""
        if not self.fts_ok:
            return 0
        con = self.conn()
        try:
            con.execute("DELETE FROM messages_fts")
            con.commit()
        finally:
            con.close()
        return self.fts_ensure()

    # -- 群
    def upsert_group(self, gid: int, name: str = "", enabled: int | None = None) -> None:
        con = self.conn()
        try:
            cur = con.execute("SELECT id, name FROM groups WHERE id=?", (int(gid),)).fetchone()
            if cur:
                renamed = bool(name) and (cur["name"] or "") != name
                if renamed:
                    con.execute("UPDATE groups SET name=?, updated_at=? WHERE id=?",
                                (name, int(time.time()), int(gid)))
                    if self.fts_ok:
                        # 群名变了同步索引里的副本(否则按群名搜索会搜到旧名)
                        con.execute("UPDATE messages_fts SET group_name=? WHERE rowid IN "
                                    "(SELECT id FROM messages WHERE group_id=?)",
                                    (fts_text(name), int(gid)))
                if enabled is not None:
                    con.execute("UPDATE groups SET enabled=?, updated_at=? WHERE id=?",
                                (int(enabled), int(time.time()), int(gid)))
            else:
                con.execute("INSERT INTO groups(id,name,enabled,updated_at) VALUES(?,?,?,?)",
                            (int(gid), name, int(enabled or 0), int(time.time())))
            con.commit()
        finally:
            con.close()

    def group_rows(self) -> dict:
        con = self.conn()
        try:
            return {int(r["id"]): {"name": r["name"] or "", "enabled": int(r["enabled"] or 0)}
                    for r in con.execute("SELECT id,name,enabled FROM groups")}
        finally:
            con.close()

    def group_counts(self, hours: int = 168) -> dict:
        since = int(time.time()) - hours * 3600
        con = self.conn()
        try:
            return {int(r["group_id"]): r["c"] for r in con.execute(
                "SELECT group_id, COUNT(*) c FROM messages WHERE ts>=? GROUP BY group_id", (since,))}
        finally:
            con.close()

    def enabled_groups(self) -> list:
        con = self.conn()
        try:
            return [int(r["id"]) for r in con.execute("SELECT id FROM groups WHERE enabled=1")]
        finally:
            con.close()

    # -- 任务运行历史
    def run_start(self, job: str) -> int:
        con = self.conn()
        try:
            cur = con.execute("INSERT INTO runs(job,started_at,status) VALUES(?,?,?)",
                              (job, int(time.time()), "running"))
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def run_finish(self, run_id: int, status: str, detail: str = "", report: str = "",
                   tokens_in: int = 0, tokens_out: int = 0, cost: float = 0.0) -> None:
        con = self.conn()
        try:
            con.execute("""UPDATE runs SET finished_at=?,status=?,detail=?,report=?,
                           tokens_in=?,tokens_out=?,cost=? WHERE id=?""",
                        (int(time.time()), status, detail[:2000], report[:300],
                         int(tokens_in), int(tokens_out), float(cost), int(run_id)))
            con.commit()
        finally:
            con.close()

    def runs(self, limit: int = 30) -> list:
        con = self.conn()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),))]
        finally:
            con.close()

    # -- 事件 / 待办: 结构化产出的长期留存(带去重合并)
    @staticmethod
    def event_dedup(kind: str, title: str, gid: int) -> str:
        norm = re.sub(r"[\s\W_]+", "", (title or "").lower())[:60]
        return hashlib.sha1(("%s|%s|%s" % (kind or "", norm, int(gid or 0))).encode()).hexdigest()[:16]

    def event_upsert(self, ev: dict) -> tuple:
        """写入事件; 已存在(同 kind+标题+群)则合并并返回 (id, False)。"""
        kind = (ev.get("kind") or "event").strip()[:20]
        title = (ev.get("title") or "").strip()[:200]
        if not title:
            return (0, False)
        gid = int(ev.get("group_id") or 0)
        dedup = self.event_dedup(kind, title, gid)
        ts_now = now()                      # 注意别用 now 作局部名, 会遮蔽模块函数
        due = int(ev.get("due_at") or 0)
        src = json.dumps(ev.get("source_ids") or [], ensure_ascii=False)
        con = self.conn()
        try:
            cur = con.execute("SELECT id, detail, due_at, priority, confidence, source_ids FROM events "
                              "WHERE dedup=?", (dedup,)).fetchone()
            if cur:
                old_src = []
                try:
                    old_src = json.loads(cur["source_ids"] or "[]")
                except Exception:
                    pass
                new_src = []
                try:
                    new_src = json.loads(src)
                except Exception:
                    pass
                merged = json.dumps(sorted(set(old_src) | set(new_src))[-20:], ensure_ascii=False)
                con.execute("""UPDATE events SET last_seen=?, detail=?, due_at=?, priority=?,
                               confidence=?, source_ids=? WHERE id=?""",
                            (ts_now,
                             ev.get("detail") if len(str(ev.get("detail") or "")) > len(str(cur["detail"] or "")) else cur["detail"],
                             due or cur["due_at"],
                             min(int(ev.get("priority") or 3), int(cur["priority"] or 3)),   # 1=最重要 → 取更重要的
                             max(float(ev.get("confidence") or 0), float(cur["confidence"] or 0)),
                             merged, cur["id"]))
                con.commit()
                return (int(cur["id"]), False)
            cur = con.execute("""INSERT INTO events(dedup,kind,title,detail,who,group_id,due_at,status,
                                 priority,confidence,source_ids,origin,first_seen,last_seen)
                                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (dedup, kind, title, str(ev.get("detail") or "")[:2000], str(ev.get("who") or "")[:40],
                               gid, due, ev.get("status") or "open", int(ev.get("priority") or 2),
                               float(ev.get("confidence") or 0), src, ev.get("origin") or "auto", ts_now, ts_now))
            con.commit()
            return (int(cur.lastrowid), True)
        finally:
            con.close()

    def events(self, status: str = "open", kind: str = "", group: str = "", q: str = "",
               limit: int = 200, soon_only: bool = False) -> list:
        where, args = [], []
        if status and status != "all":
            where.append("e.status=?")
            args.append(status)
        if kind:
            where.append("e.kind=?")
            args.append(kind)
        if group:
            where.append("e.group_id=?")
            args.append(int(group))
        if q:
            where.append("(e.title LIKE ? OR e.detail LIKE ?)")
            args += ["%" + q + "%", "%" + q + "%"]
        if soon_only:
            where.append("e.due_at>0 AND e.due_at<=?")
            args.append(now() + 7 * 86400)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        con = self.conn()
        try:
            return [dict(r) for r in con.execute(
                """SELECT e.*, COALESCE(g.name,'') group_name FROM events e
                   LEFT JOIN groups g ON g.id=e.group_id%s
                   ORDER BY (e.due_at=0), e.due_at ASC, e.priority ASC, e.last_seen DESC
                   LIMIT ?""" % w, args + [int(limit)])]
        finally:
            con.close()

    def event_update(self, eid: int, **fields) -> bool:
        allow = {k: v for k, v in fields.items()
                 if k in ("status", "due_at", "priority", "notes", "title", "detail", "kind")}
        if not allow:
            return False
        sets = ", ".join("%s=?" % k for k in allow)
        con = self.conn()
        try:
            con.execute("UPDATE events SET %s, last_seen=? WHERE id=?" % sets,
                        list(allow.values()) + [now(), int(eid)])
            con.commit()
            return True
        finally:
            con.close()

    def event_delete(self, eid: int) -> bool:
        con = self.conn()
        try:
            con.execute("DELETE FROM events WHERE id=?", (int(eid),))
            con.commit()
            return True
        finally:
            con.close()

    def event_stats(self) -> dict:
        con = self.conn()
        try:
            by = {r["status"]: r["c"] for r in con.execute(
                "SELECT status, COUNT(*) c FROM events GROUP BY status")}
            kinds = {r["kind"]: r["c"] for r in con.execute(
                "SELECT kind, COUNT(*) c FROM events WHERE status='open' GROUP BY kind")}
            soon = con.execute("SELECT COUNT(*) c FROM events WHERE status='open' AND due_at>0 "
                               "AND due_at<=?", (now() + 7 * 86400,)).fetchone()["c"]
            overdue = con.execute("SELECT COUNT(*) c FROM events WHERE status='open' AND due_at>0 "
                                  "AND due_at<?", (now(),)).fetchone()["c"]
            return {"by_status": by, "by_kind": kinds, "due_7d": soon, "overdue": overdue}
        finally:
            con.close()

    # -- 记忆(跨天上下文)
    def memory_get(self, scope: str, key: str = "digest") -> dict:
        con = self.conn()
        try:
            r = con.execute("SELECT * FROM memories WHERE scope=? AND key=?", (scope, key)).fetchone()
            return dict(r) if r else {}
        finally:
            con.close()

    def memory_set(self, scope: str, key: str, content: str, bump_hits: bool = False) -> None:
        con = self.conn()
        try:
            con.execute("""INSERT INTO memories(scope,key,content,updated_at,hits) VALUES(?,?,?,?,?)
                           ON CONFLICT(scope,key) DO UPDATE SET content=excluded.content,
                           updated_at=excluded.updated_at, hits=hits+?""",
                        (scope, key, str(content)[:4000], now(), 1 if bump_hits else 0,
                         1 if bump_hits else 0))
            con.commit()
        finally:
            con.close()

    def memory_all(self, prefix: str = "") -> list:
        con = self.conn()
        try:
            if prefix:
                rows = con.execute("SELECT * FROM memories WHERE scope LIKE ? ORDER BY updated_at DESC",
                                   (prefix + "%",))
            else:
                rows = con.execute("SELECT * FROM memories ORDER BY updated_at DESC")
            return [dict(r) for r in rows]
        finally:
            con.close()

    # -- 链接与媒体
    def link_upsert(self, url: str, **fields) -> bool:
        if not url:
            return False
        con = self.conn()
        try:
            cur = con.execute("SELECT url FROM links WHERE url=?", (url,)).fetchone()
            if cur:
                con.execute("UPDATE links SET last_seen=? WHERE url=?", (now(), url))
            else:
                con.execute("""INSERT INTO links(url,title,domain,group_id,first_seen,last_seen,status,excerpt)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (url, str(fields.get("title") or "")[:200], str(fields.get("domain") or "")[:80],
                             int(fields.get("group_id") or 0), now(), now(),
                             fields.get("status") or "new", str(fields.get("excerpt") or "")[:4000]))
            con.commit()
            return True
        finally:
            con.close()

    def link_update(self, url: str, **fields) -> bool:
        allow = {k: v for k, v in fields.items()
                 if k in ("title", "status", "excerpt", "error", "fetched_at")}
        if not allow:
            return False
        sets = ", ".join("%s=?" % k for k in allow)
        con = self.conn()
        try:
            con.execute("UPDATE links SET %s WHERE url=?" % sets, list(allow.values()) + [url])
            con.commit()
            return True
        finally:
            con.close()

    def links(self, status: str = "", limit: int = 100, group: str = "") -> list:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if group:
            where.append("group_id=?")
            args.append(int(group))
        w = (" WHERE " + " AND ".join(where)) if where else ""
        con = self.conn()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM links%s ORDER BY last_seen DESC LIMIT ?" % w, args + [int(limit)])]
        finally:
            con.close()

    def media_upsert(self, key: str, **fields) -> bool:
        if not key:
            return False
        cols = {k: v for k, v in fields.items()
                if k in ("group_id", "message_id", "kind", "name", "url", "path", "size",
                         "caption", "caption_at", "status", "error")}
        con = self.conn()
        try:
            cur = con.execute("SELECT key FROM media WHERE key=?", (key,)).fetchone()
            if cur:
                if cols:
                    sets = ", ".join("%s=?" % k for k in cols)
                    con.execute("UPDATE media SET %s WHERE key=?" % sets, list(cols.values()) + [key])
            else:
                cols["key"] = key
                cols.setdefault("first_seen", now())
                names = ", ".join(cols)
                con.execute("INSERT INTO media(%s) VALUES(%s)" % (names, ",".join("?" * len(cols))),
                            list(cols.values()))
            con.commit()
            return True
        finally:
            con.close()

    def media_get(self, key: str) -> dict:
        con = self.conn()
        try:
            r = con.execute("SELECT * FROM media WHERE key=?", (key,)).fetchone()
            return dict(r) if r else {}
        finally:
            con.close()

    def media_list(self, limit: int = 60, kind: str = "") -> list:
        con = self.conn()
        try:
            if kind:
                rows = con.execute("SELECT * FROM media WHERE kind=? ORDER BY first_seen DESC LIMIT ?",
                                   (kind, int(limit)))
            else:
                rows = con.execute("SELECT * FROM media ORDER BY first_seen DESC LIMIT ?", (int(limit),))
            return [dict(r) for r in rows]
        finally:
            con.close()

    # -- 元数据/导出/备份
    def meta_get(self, key: str, default: str = "") -> str:
        con = self.conn()
        try:
            r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default
        finally:
            con.close()

    def meta_set(self, key: str, value: str) -> None:
        con = self.conn()
        try:
            con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            con.commit()
        finally:
            con.close()

    def export_csv(self, hours: int = 168, group: str = "") -> bytes:
        where, args = ["ts>=?"], [int(time.time()) - int(hours) * 3600]
        if group:
            where.append("group_id=?")
            args.append(int(group))
        con = self.conn()
        try:
            rows = con.execute("SELECT ts,group_id,user_id,nickname,raw_message,kinds FROM messages "
                               "WHERE " + " AND ".join(where) + " ORDER BY ts", args).fetchall()
        finally:
            con.close()
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["time", "group_id", "user_id", "nickname", "message", "kinds"])
        for r in rows:
            wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["group_id"],
                         r["user_id"], r["nickname"], r["raw_message"], r["kinds"]])
        return buf.getvalue().encode("utf-8-sig")

    def prune(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = int(time.time()) - days * 86400
        con = self.conn()
        try:
            n = con.execute("DELETE FROM messages WHERE ts<?", (cutoff,)).rowcount
            con.commit()
            con.execute("VACUUM")
            return n
        finally:
            con.close()

    def backup(self, dest: str) -> str:
        ensure_dirs()
        con = self.conn()
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target = os.path.join(dest, "messages-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
            bck = sqlite3.connect(target)
            with bck:
                con.backup(bck)
            bck.close()
            return target
        finally:
            con.close()


CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af"


def fts_text(s: str) -> str:
    """把文本转成 FTS5 索引形式: CJK/假名/谚文逐字用空格隔开。

    unicode61 分词器会把一整串中文当成一个 token, 导致"头像"搜不到"吧唧头像裁剪";
    逐字切开后, 查询侧用短语("头 像")即可实现中文子串匹配, 拉丁词仍是完整词。
    """
    out = []
    for ch in s or "":
        if re.match("[%s]" % CJK, ch):
            out.append(" " + ch + " ")
        else:
            out.append(" " if ch.isspace() else ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def fts_query(q: str) -> str:
    """构造安全的 FTS5 查询: 中文按短语(逐字), 拉丁按前缀。不泄露用户原始语法。"""
    parts = []
    for tok in re.findall("[%s]+|[A-Za-z0-9_]+" % CJK, q or ""):
        if re.match("[%s]" % CJK, tok):
            parts.append('"%s"' % " ".join(tok))
        else:
            parts.append('"%s"*' % tok)
    return " AND ".join(parts)


def kinds_of(msg) -> str:
    if isinstance(msg, str):
        return "text"
    ks = []
    for s in (msg or []):
        t = (s or {}).get("type") or "?"
        t = {"record": "voice", "video": "video"}.get(t, t)
        if t not in ks:
            ks.append(t)
    return ",".join(ks)


# ---------------------------------------------------------------- NapCat 客户端
class NapCat:
    """OneBot v11 HTTP API 客户端(仅访问本机 loopback)。"""

    def __init__(self, port: int = 3000, token: str = "", host: str = "127.0.0.1", timeout: int = 10):
        self.base = "http://%s:%d" % (host, int(port))
        self.token = token or ""
        self.timeout = timeout

    def call(self, action: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        req = urllib.request.Request(
            "%s/%s" % (self.base, action),
            data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def login_info(self) -> dict:
        return (self.call("get_login_info") or {}).get("data") or {}

    def group_list(self) -> list:
        data = (self.call("get_group_list", timeout=20) or {}).get("data") or []
        out = []
        for g in data:
            try:
                out.append({"id": int(g.get("group_id")), "name": (g.get("group_name") or "").strip()})
            except Exception:
                continue
        return out

    def send_private(self, user_id: int, text: str) -> dict:
        return self.call("send_private_msg", {"user_id": int(user_id),
                                              "message": [{"type": "text", "data": {"text": text}}]}, 30)

    def send_group(self, group_id: int, text: str) -> dict:
        return self.call("send_group_msg", {"group_id": int(group_id),
                                            "message": [{"type": "text", "data": {"text": text}}]}, 30)

    def send_text(self, text: str, user_id: int = 0, group_id: int = 0, max_chars: int = 1200) -> int:
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
        sent = 0
        for c in chunks:
            if group_id:
                self.send_group(group_id, c)
            else:
                self.send_private(user_id, c)
            sent += 1
            time.sleep(0.4)
        return sent

    def health(self) -> dict:
        t0 = time.time()
        try:
            info = self.login_info()
            return {"ok": True, "ms": int((time.time() - t0) * 1000),
                    "user_id": info.get("user_id"), "nickname": info.get("nickname")}
        except Exception as e:
            return {"ok": False, "ms": int((time.time() - t0) * 1000), "error": str(e)[:200]}


def napcat_from_config(cfg: Config) -> NapCat:
    p = cfg.data.get("push", {})
    return NapCat(port=int(p.get("napcat_api_port", 3000)), token=p.get("token", ""))


# ---------------------------------------------------------------- 进程/服务
def _run(cmd: list, timeout: int = 20) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return "(执行失败: %s)" % e


def pgrep(pattern: str) -> bool:
    """是否存在匹配的进程。

    注意: 不能只看输出非空 —— pgrep 不存在时 _run 会返回"(执行失败: ...)" 文本,
    那样会把"命令失败"误判成"匹配到了进程"(所有"是否运行"的判断都会变假真)。
    """
    out = _run(["pgrep", "-f", pattern], 8)
    return any(line.strip().isdigit() for line in out.splitlines())


def screen_list() -> list:
    out = _run(["screen", "-ls"], 10)
    names = []
    for line in out.splitlines():
        line = line.strip()
        if "." in line and ("Detached" in line or "Attached" in line):
            names.append(line.split("\t")[0].strip())
    return names


# ---------------------------------------------------------------- 服务进程管理
RUN_DIR = os.path.join(DATA, "run")
SERVICE_MODULES = {"collector": "qqops.collector", "scheduler": "qqops.scheduler",
                   "web": "qqops.webapp", "watchdog": "qqops.watchdog"}
# 看门狗守护的对象(它自己不能被自己重启)
WATCH_TARGETS = ("collector", "scheduler", "web")


def _ancestor_pids() -> set:
    """取本进程的所有祖先进程 PID(Linux)。"""
    out, pid = set(), os.getpid()
    for _ in range(12):
        try:
            ppid = 0
            with open("/proc/%d/status" % pid) as f:
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            if ppid <= 1 or ppid in out:
                break
            out.add(ppid)
            pid = ppid
        except Exception:
            break
    return out


def kill_pattern(pattern: str, sig: int = 15) -> int:
    """按 cmdline 模式终止进程, 但**排除自身与所有祖先进程**。

    直接 `pkill -f astrbot` 会把调用方自己杀掉 —— 因为 `qqops astrbot stop` 的
    命令行里也含 "astrbot"(方括号技巧只能防 pgrep 自身, 防不住父进程链)。
    """
    pids = [p for p in _run(["pgrep", "-f", pattern], 8).split() if p.strip().isdigit()]
    skip = {os.getpid()} | _ancestor_pids()
    killed = 0
    for p in pids:
        pid = int(p)
        if pid in skip:
            continue
        try:
            os.kill(pid, sig)
            killed += 1
        except Exception:
            pass
    return killed


def _base_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pid_path(name: str) -> str:
    return os.path.join(RUN_DIR, "%s.pid" % name)


def service_health(name: str, cfg: Config | None = None, timeout: float = 12.0) -> dict:
    """真实健康检查: 不只看进程在不在, 还要端口能应答 / 心跳是新的。

    只判断 pgrep 会把"启动了但立刻崩掉"误报成成功 —— 手机上很常见。
    """
    cfg = cfg or Config()
    mod = SERVICE_MODULES.get(name)
    if not mod:
        return {"ok": False, "why": "未知服务"}
    if not pgrep("[-]m %s" % mod):
        return {"ok": False, "why": "进程不存在"}
    end = time.time() + timeout
    if name == "watchdog":
        beat = 0
        while time.time() < end:                 # 刚启动时首轮体检还没跑完, 要等一等
            beat = int(state_get("watchdog_beat", 0) or 0)
            if beat and now() - beat < 180:
                return {"ok": True, "why": "心跳 %s" % fmt_ts(beat)}
            time.sleep(1)
        return {"ok": False, "why": "心跳 %s" % (fmt_ts(beat) if beat else "无")}
    if name == "scheduler":
        while time.time() < end:
            beat = int(state_get("scheduler_beat", 0) or 0)
            if now() - beat < 60:
                return {"ok": True, "why": "心跳正常"}
            time.sleep(1)
        return {"ok": False, "why": "心跳超时(60 秒内没有心跳)"}
    port = int(cfg.data["collector" if name == "collector" else "web"]["port"])
    path = "/health" if name == "web" else "/"
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=3) as r:
                if r.status == 200:
                    return {"ok": True, "why": "端口 %d 已应答" % port}
        except Exception:
            time.sleep(1)
    return {"ok": False, "why": "进程在但端口 %d 无应答" % port}


def _cmdline(pid: int) -> str:
    """读某进程的完整命令行(拿不到就返回空串)。"""
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as f:
            return f.read().decode("utf-8", "replace").replace("\0", " ").strip()
    except Exception:
        return ""


def _pid_alive_and_matches(pid: int, mod: str) -> bool:
    """校验 PID 文件里的进程是否真的是我们的服务。

    重启后 PID 会被复用 —— 不校验就可能杀掉别人的进程。
    """
    try:
        os.kill(int(pid), 0)                       # 先确认真的还活着
    except Exception:
        return False
    return bool(mod) and mod in _cmdline(pid)


def kill_pid(pid: int, sig: int = 15) -> bool:
    try:
        os.kill(int(pid), sig)
        return True
    except Exception:
        return False


def _pid_environ(pid: int) -> dict:
    """读某进程的环境变量(Termux 同 uid 可读; 读不到就返回空)。"""
    try:
        with open("/proc/%d/environ" % int(pid), "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except Exception:
        return {}
    out = {}
    for kv in raw.split("\0"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = v
    return out


_PROC_NET_OK = None


def proc_net_ok() -> bool:
    """本机能否读 /proc/net/tcp(Android 上普通应用被系统禁止, 只能退回 pgrep)。"""
    global _PROC_NET_OK
    if _PROC_NET_OK is None:
        try:
            with open("/proc/net/tcp") as f:
                f.readline()
            _PROC_NET_OK = True
        except Exception:
            _PROC_NET_OK = False
    return bool(_PROC_NET_OK)


def port_owner(port: int) -> dict:
    """查出监听某 TCP 端口的进程(纯 stdlib)。

    桌面 Linux 上: /proc/net/tcp 里有监听套接字的 inode, 再到 /proc/<pid>/fd 反查即可。
    Android 上 /proc/net/tcp 是 Permission denied(lsof/fuser/ss 同样读不了),
    这时返回 {} —— 调用方改用 _own_pids() 按"命令行 + 数据目录"判断是不是自己人。
    """
    try:
        want = "%04X" % int(port)
    except Exception:
        return {}
    if not proc_net_ok():
        return {}
    inodes = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(f) as fh:
                next(fh, None)                     # 跳过表头
                for line in fh:
                    p = line.split()
                    if len(p) < 10 or p[3] != "0A":   # 0A = LISTEN
                        continue
                    if p[1].rsplit(":", 1)[-1].upper() == want:
                        inodes.add(p[9])
        except Exception:
            continue
    if not inodes:
        return {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        d = "/proc/%s/fd" % pid
        try:
            for fd in os.listdir(d):
                try:
                    tgt = os.readlink(os.path.join(d, fd))
                except Exception:
                    continue
                if tgt.startswith("socket:[") and tgt[8:-1].strip("]") in inodes:
                    return {"pid": int(pid), "cmd": _cmdline(int(pid)),
                            "env": _pid_environ(int(pid))}
        except Exception:
            continue
    return {}


def _own_pids(mod: str) -> list:
    """本套实例的服务进程(模块名命中 + QQOPS_DATA 一致), **完全不看端口**。

    手机上没有端口反查能力, 这是唯一可靠的"这是不是我们自己人"判据;
    数据目录必须一致, 否则会把另一套实例(比如真机上跑的测试)当成僵死进程杀掉。
    """
    out, skip = [], {os.getpid()} | _ancestor_pids()
    for p in _run(["pgrep", "-f", "[-]m %s" % mod], 8).split():
        if not p.strip().isdigit():
            continue
        pid = int(p)
        if pid in skip or pid in out:
            continue
        other = (_pid_environ(pid) or {}).get("QQOPS_DATA")
        if other:
            try:
                if os.path.realpath(other) != os.path.realpath(DATA):
                    continue
            except Exception:
                continue
        out.append(pid)
    return out


def _wait_port_free(port: int, secs: float = 5.0) -> bool:
    for _ in range(max(1, int(secs))):
        if not port_open(port):
            return True
        time.sleep(1)
    return not port_open(port)


def check_port_free(port: int, mod: str = "") -> dict:
    """端口预检。返回 {} 表示可用; 否则给出占用者。

    ours=True 说明占用者就是我们自己的同一个模块 —— 那多半是"看起来停了、
    其实还占着端口"的僵死进程, 是手机上"启动即死"的第一大原因。

    手机上定位不到占用者时(见 port_owner), 用 `_own_pids` 兜底: 只要存在
    "同一模块 + 同一数据目录"的进程, 就认定端口是我们的僵死进程占着, 可以回收。
    """
    if not port or not port_open(port):
        return {}
    own = port_owner(port)
    pid, cmd, env = own.get("pid"), own.get("cmd", ""), own.get("env") or {}
    same_data = True
    if env.get("QQOPS_DATA"):
        try:
            same_data = os.path.realpath(env["QQOPS_DATA"]) == os.path.realpath(DATA)
        except Exception:
            same_data = False
    ours = bool(mod and mod in cmd and same_data)
    cands = _own_pids(mod) if mod else []
    if not pid and cands:                       # 手机: 反查不到, 但确实有本套实例在跑
        ours, pid, cmd = True, cands[0], _cmdline(cands[0])
    if pid:
        who = "(pid %s %s)" % (pid, cmd[:70])
    elif proc_net_ok():
        who = "(占用者未知)"
    else:
        who = "(手机上无法定位占用者: /proc/net/tcp 被系统禁止)"
    if cands:
        who += " [本套实例进程 %s]" % ",".join(str(x) for x in cands)
    return {"ok": False, "port": port, "pid": pid, "cmd": cmd, "ours": ours,
            "cands": cands, "why": "端口 %d 已被占用 %s" % (port, who)}


def _svc_port(name: str) -> int:
    """服务监听的端口(collector/web 才有; 其它返回 0)。"""
    key = {"collector": "collector", "web": "web"}.get(name)
    if not key:
        return 0
    try:
        return int(Config().data[key]["port"])
    except Exception:
        return 0


def _write_pidfile(name: str, pid: int) -> None:
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(pid_path(name), "w") as f:
            f.write(str(int(pid)))
    except Exception:
        pass


def _find_pid(mod: str) -> int:
    """pgrep -f 找到的第一个 pid(找不到返回 0)。"""
    for p in _run(["pgrep", "-f", "[-]m %s" % mod], 8).split():
        if p.strip().isdigit():
            return int(p)
    return 0


def _reclaim_port(name: str, port: int, mod: str) -> tuple:
    """回收被自己僵死进程占着的端口。返回 (错误说明, 提示语), 两者最多一个非空。"""
    if not port or not port_open(port):
        return "", ""
    conf = check_port_free(port, mod)
    if not conf:
        return "", ""
    if not conf.get("ours"):
        return "%s。请先停掉占用该端口的程序(或改端口)" % conf["why"], ""
    pids = list(conf.get("cands") or [])
    if conf.get("pid") and conf["pid"] not in pids:
        pids.append(conf["pid"])
    if not pids:
        return "%s。定位不到占用进程, 请手工处理(可先 qqops stop 或重启 Termux)" % conf["why"], ""
    note = "已回收占用端口 %d 的僵死进程 pid %s; " % (port, ",".join(str(p) for p in pids))
    for pid in pids:
        kill_pid(pid, 15)
    if _wait_port_free(port, 5):
        return "", note
    for pid in pids:
        kill_pid(pid, 9)
    if _wait_port_free(port, 2):
        return "", note
    return "%s 且强杀 pid %s 后仍未释放" % (conf["why"], ",".join(str(p) for p in pids)), ""


def _exit_code_soon(p, secs: float = 3.0):
    """在 secs 秒内确认子进程是否已退出; 还在跑则返回 None。

    手机上解释器启动/退出都比桌面慢, 单次 poll 会漏掉"刚变成僵尸"的瞬间,
    于是"秒退"会被误报成"起来了但端口没应答"。
    """
    end = time.time() + secs
    while True:
        rc = p.poll()
        if rc is not None:
            return rc
        if time.time() >= end:
            return None
        time.sleep(0.3)


def start_service(name: str, wait: float = 25.0) -> str:
    """nohup 后台启动一个服务, 并且**等到它真的能干活**才报成功。"""
    mod = SERVICE_MODULES.get(name)
    if not mod:
        return "未知服务: %s" % name
    if pgrep("[-]m %s" % mod):
        h = service_health(name, timeout=6)
        if h["ok"]:
            pid = _find_pid(mod)                          # 认领已有进程, 保证之后停得掉
            if pid:
                _write_pidfile(name, pid)
            return "%s 已在运行(%s)" % (name, h["why"])
    # 端口预检: 上一份僵死进程占着端口会让新进程秒退, 这是"启动不完全"的头号原因
    port, note = _svc_port(name), ""
    if port:
        bad, note = _reclaim_port(name, port, mod)
        if bad:
            return "%s 启动失败: %s" % (name, bad)
    ensure_dirs()
    os.makedirs(RUN_DIR, exist_ok=True)
    logf = os.path.join(LOG_DIR, "%s.log" % mod.split(".")[-1])
    t0 = time.time()
    try:
        with open(logf, "ab") as out:
            p = subprocess.Popen([sys.executable, "-m", mod], cwd=_base_dir(),
                                 stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
    except Exception as e:
        return "%s 启动失败(无法创建进程): %s" % (name, e)
    _write_pidfile(name, p.pid)
    while time.time() - t0 < wait:
        if p.poll() is not None:                      # 进程自己退了 → 明确报失败
            return "%s启动失败(进程秒退, 退出码 %s)。日志尾部: %s" % (
                note, p.returncode, tail(logf, 3).replace("\n", " | ")[-300:])
        if pgrep("[-]m %s" % mod):
            break
        time.sleep(0.5)
    else:
        return "%s启动失败(%.0f 秒内没起来)。日志尾部: %s" % (
            note, wait, tail(logf, 3).replace("\n", " | ")[-300:])
    # 手机上解释器启动慢: pgrep 命中时进程可能正在退出, 这里必须再确认一次,
    # 否则会把"秒退"误报成"起来了但端口没应答"。
    rc = p.poll()
    if rc is not None:
        return "%s启动失败(进程已退出, 退出码 %s)。日志尾部: %s" % (
            note, rc, tail(logf, 3).replace("\n", " | ")[-300:])
    h = service_health(name, timeout=max(4.0, wait - (time.time() - t0)))
    if h["ok"]:
        return "%s%s 已启动 (pid %s, %s)" % (note, name, p.pid, h["why"])
    rc = _exit_code_soon(p, 3.0)          # 端口没应答: 先确认它是不是已经退出了
    if rc is not None:
        return "%s启动失败(进程已退出, 退出码 %s)。日志尾部: %s" % (
            note, rc, tail(logf, 3).replace("\n", " | ")[-300:])
    return "%s%s 启动异常: %s。日志尾部: %s" % (note, name, h["why"],
                                            tail(logf, 4).replace("\n", " | ")[-300:])


def stop_service(name: str) -> str:
    mod = SERVICE_MODULES.get(name, "")
    pf = pid_path(name)
    if os.path.isfile(pf):
        try:
            pid = int(open(pf).read().strip() or 0)
        except Exception:
            pid = 0
        if pid and (not mod or _pid_alive_and_matches(pid, mod)):
            kill_pid(pid, 15)
        try:
            os.remove(pf)
        except Exception:
            pass
    if mod:
        kill_pattern("[-]m %s" % mod)
        for _ in range(6):
            if not pgrep("[-]m %s" % mod):
                break
            time.sleep(1)
        else:
            kill_pattern("[-]m %s" % mod, sig=9)
            time.sleep(1)
        # 端口残留回收: 进程没了但端口还占着, 下次启动就会秒退
        port = _svc_port(name)
        if port and port_open(port):
            own = port_owner(port)
            if own.get("pid") and mod in own.get("cmd", ""):
                kill_pid(own["pid"], 9)
                _wait_port_free(port, 3)
    return "%s 已停止" % name


# ---------------------------------------------------------------- 旁路服务(NapCat / AstrBot)
def _proot_rootfs() -> str:
    pfx = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
    return os.path.join(pfx, "var/lib/proot-distro/containers/debian/rootfs")


EXTRAS = {
    "napcat": {
        "label": "NapCat (QQ 接入 · 必需)",
        "pattern": "[o]pt/QQ/qq",
        "start": ["screen", "-dmS", "napcat", "proot-distro", "sh", "debian", "--",
                  "bash", "/root/_napcat_run.sh"],
        "screen": "napcat",
        "lock": "",
        "note": "停掉它 = 收不到也发不出任何消息",
    },
    "astrbot": {
        "label": "AstrBot (存档旁路 · 可选)",
        "pattern": "[a]strbot",
        "start": ["screen", "-dmS", "astrbot", "proot-distro", "sh", "debian", "--",
                  "bash", "/root/_run.sh"],
        "screen": "astrbot",
        "lock": "",
        "note": "只影响存档仪表盘与插件, QQOps 不依赖它",
    },
}
# 说明: 停止一律走 core.kill_pattern(pattern) —— 它按 cmdline 匹配但排除自身与祖先进程,
# 否则 `qqops astrbot stop` 会因为自己命令行里含 "astrbot" 而把自己杀掉(实测过)。


def _extra_spec(cfg: Config, name: str) -> dict:
    spec = dict(EXTRAS.get(name) or {})
    if not spec:
        return {}
    spec.update((cfg.data.get("extras") or {}).get(name) or {})   # 允许按设备覆盖
    if name == "astrbot" and not spec.get("lock"):
        spec["lock"] = os.path.join(_proot_rootfs(), "root/astrbot/astrbot.lock")
    return spec


def extra_status(cfg: Config) -> dict:
    out = {}
    for name in EXTRAS:
        spec = _extra_spec(cfg, name)
        info = proc_info(spec["pattern"])
        out[name] = {"label": spec.get("label", name), "running": bool(info["pid"]),
                     "pid": info["pid"], "rss": info["rss"], "etime": info["etime"],
                     "note": spec.get("note", "")}
    return out


def extra_action(cfg: Config, name: str, action: str) -> str:
    spec = _extra_spec(cfg, name)
    if not spec:
        return "未知旁路服务: %s" % name
    if action in ("start", "restart") and pgrep(spec["pattern"]):
        if action == "start":
            return "%s 已在运行" % name
        kill_pattern(spec["pattern"])
        time.sleep(3)
    if action == "start":
        # screen 同名会话若残留(常见为 "Remote or dead"), -dmS 会直接拒绝创建 → 先清理
        scr = spec.get("screen") or name
        _run(["screen", "-wipe"], 8)
        _run(["screen", "-S", scr, "-X", "quit"], 8)
        time.sleep(0.5)
        try:
            with open(os.path.join(LOG_DIR, "extras.log"), "ab") as out:
                subprocess.Popen(spec["start"], stdout=out, stderr=out,
                                 stdin=subprocess.DEVNULL, start_new_session=True, cwd=HOME)
        except Exception as e:
            return "%s 启动失败: %s" % (name, e)
        for _ in range(12):
            if pgrep(spec["pattern"]):
                return "%s 已启动" % name
            time.sleep(2)
        return "%s 启动中(登录需要一点时间, 见 data/logs/extras.log)" % name
    if action == "stop":
        _run(["screen", "-S", spec.get("screen") or name, "-X", "quit"], 8)
        kill_pattern(spec["pattern"])
        if spec.get("lock") and os.path.exists(spec["lock"]):
            try:
                os.remove(spec["lock"])
            except Exception:
                pass
        for _ in range(4):
            if not pgrep(spec["pattern"]):
                return "%s 已停止" % name
            time.sleep(1)
        kill_pattern(spec["pattern"], sig=9)      # proot 里 TERM 常常不传递, 直接补一刀
        time.sleep(1)
        return "%s 已停止" % name
    return "未知动作: %s" % action


def service_status(cfg: Config) -> dict:
    web_port = int(cfg.data["web"]["port"])
    col_port = int(cfg.data["collector"]["port"])
    napcat_port = int(cfg.data["push"].get("napcat_api_port", 3000))
    def one(name: str, pattern: str, port: int) -> dict:
        info = proc_info(pattern)
        return {"running": bool(info["pid"]), "port": port, "pid": info["pid"],
                "rss": info["rss"], "etime": info["etime"]}
    return {
        "collector": one("collector", "[-]m qqops.collector", col_port),
        "scheduler": one("scheduler", "[-]m qqops.scheduler", 0),
        "web": one("web", "[-]m qqops.webapp", web_port),
        "watchdog": one("watchdog", "[-]m qqops.watchdog", 0),
        "napcat": one("napcat", "[o]pt/QQ/qq", napcat_port),
    }


def status_snapshot(cfg: Config | None = None) -> dict:
    """给控制台用的完整状态快照(服务/端口/采集/调度/存储/生态)。"""
    cfg = cfg or Config()
    store = Store()
    stats = store.stats(24)
    sch = cfg.data.get("schedule", {})
    nxt = next_run(sch)
    last_ts = int(state_get("last_summary_ts", 0) or 0)
    new_msgs = store.count_since(last_ts) if last_ts else stats["window"]
    return {
        "ts": now(),
        "version": VERSION,
        "services": service_status(cfg),
        "extras": extra_status(cfg),
        "ecosystem": ecosystem_ports(cfg),
        "counters": state_get("collector_counters", {}) or {},
        "fts": {"available": store.fts_ok, "rows": store.fts_count()},
        "scheduler": {"alive": now() - int(state_get("scheduler_beat", 0) or 0) < 60,
                      "beat": int(state_get("scheduler_beat", 0) or 0),
                      "enabled": sch.get("enabled", True),
                      "mode": sch.get("mode") or "daily",
                      "label": nxt.get("label", ""),
                      "times": sch.get("times") or [],
                      "every_minutes": sch.get("every_minutes"),
                      "weekdays": sch.get("weekdays") or [],
                      "weekly_time": sch.get("weekly_time"),
                      "skip_if_no_new": sch.get("skip_if_no_new", True),
                      "min_new_messages": sch.get("min_new_messages", 5),
                      "last_run": last_ts,
                      "new_since_last": new_msgs,
                      "next": nxt},
        "messages": {"total": stats["total"], "last_hour": stats["last_hour"],
                     "window": stats["window"], "groups": stats["groups"]},
        "storage": {"db": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
                    "data": dir_size(DATA),
                    "reports": count_files(REPORT_DIR, ".md"),
                    "backups": count_files(BACKUP_DIR, ".db"),
                    "disk": disk_usage(DATA)},
        "enabled_groups": len(store.enabled_groups()),
    }


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


def disk_usage(path: str = DATA) -> dict:
    try:
        total, used, free = shutil.disk_usage(path)
        return {"total": total, "used": used, "free": free}
    except Exception:
        return {"total": 0, "used": 0, "free": 0}


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total


def proc_info(pattern: str) -> dict:
    """按 pgrep 模式取进程 PID / 常驻内存 / 运行时长。"""
    pids = _run(["pgrep", "-f", pattern], 8).split()
    if not pids:
        return {"pid": 0, "rss": 0, "etime": ""}
    pid = pids[0].strip()
    rss, etime = 0, ""
    try:
        with open("/proc/%s/statm" % pid) as f:
            rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    etime = _run(["ps", "-o", "etime=", "-p", pid], 8).strip()
    try:
        pid = int(pid)
    except Exception:
        pid = 0
    return {"pid": pid, "rss": rss, "etime": etime}


def _day_start(ts: int) -> int:
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def _hhmm(text: str) -> tuple:
    try:
        hh, mm = [int(x) for x in str(text).split(":")[:2]]
        return max(0, min(23, hh)), max(0, min(59, mm))
    except Exception:
        return 22, 0


def next_run_time(times: list, now_ts: int | None = None) -> dict:
    """(兼容旧接口)按每日时间点列表算下一次触发。"""
    now_ts = now_ts or now()
    best = 0
    for t in (times or []):
        hh, mm = _hhmm(t)
        ts = _day_start(now_ts) + hh * 3600 + mm * 60
        while ts <= now_ts:
            ts += 86400
        if best == 0 or ts < best:
            best = ts
    return {"ts": best, "in": max(0, best - now_ts) if best else 0}


def next_run(sched: dict, now_ts: int | None = None) -> dict:
    """按 schedule 配置算下一次运行(支持 每天定点 / 每隔 N 分钟 / 每周几)。"""
    now_ts = now_ts or now()
    out = {"ts": 0, "in": 0, "mode": "off", "label": "已停用"}
    if not (sched or {}).get("enabled", True):
        return out
    mode = str(sched.get("mode") or "daily").lower()
    if mode == "interval":
        every = max(1, int(sched.get("every_minutes") or 360))
        last = int(state_get("last_summary_ts", 0) or 0)
        nxt = (last or now_ts) + every * 60
        if nxt <= now_ts:            # 已经超过间隔 → 视为"现在就该跑", 与调度器判定保持一致
            nxt = now_ts
        return {"ts": nxt, "in": max(0, nxt - now_ts), "mode": mode,
                "label": "每隔 %s" % (("%d 小时" % (every // 60)) if every % 60 == 0 else ("%d 分钟" % every))}
    if mode == "weekly":
        wds = [int(x) for x in (sched.get("weekdays") or [1, 2, 3, 4, 5])]
        hh, mm = _hhmm(sched.get("weekly_time") or "22:00")
        for delta in range(0, 9):
            base = _day_start(now_ts + delta * 86400) + hh * 3600 + mm * 60
            if base > now_ts and (time.localtime(base).tm_wday + 1) in wds:
                names = "一二三四五六日"
                label = "每周" + "、".join(names[w - 1] for w in sorted(set(wds)) if 1 <= w <= 7) + " %02d:%02d" % (hh, mm)
                return {"ts": base, "in": base - now_ts, "mode": mode, "label": label}
        return out
    r = next_run_time(sched.get("times") or ["22:00"], now_ts)
    return {"ts": r["ts"], "in": r["in"], "mode": "daily",
            "label": "每天 " + "、".join(sched.get("times") or ["22:00"])}


def count_files(path: str, suffix: str = "") -> int:
    try:
        return len([f for f in os.listdir(path) if f.endswith(suffix)])
    except Exception:
        return 0


WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
            "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}


def parse_due(text: str, base_ts: int | None = None) -> int:
    """把模型给的截止时间归一成时间戳(日期类默认取**当天 23:59**, 即"那天结束前")。

    支持: 2026-10-15 / 2026/10/15 / 10-15 / 2026-10-15 20:30 / 今天 / 明天 / 后天 /
          X天后 / 本周五 / 下周三 / 周天 / 月底。识别不了返回 0(表示无截止)。

    注意: 中文日期的字符替换(年→-)只用于数字日期解析, 关键词判断一律用原始串 ——
    否则 "月底" 会先被替换成 "-底" 而永远匹配不上。
    """
    raw = str(text or "").strip()
    if not raw:
        return 0
    s = raw.replace("年", "-").replace("月", "-").replace("日", "").replace("号", "")
    now_ts = base_ts or now()
    lt = time.localtime(now_ts)

    def at(y, mo, d, hh=23, mm=59):
        try:
            return int(time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1)))
        except Exception:
            return 0

    def day_of(ts):
        l2 = time.localtime(ts)
        return at(l2.tm_year, l2.tm_mon, l2.tm_mday)

    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", s)
    if m:
        if m.group(4):
            return at(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                      int(m.group(4)), int(m.group(5)))
        return at(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})(?!\d)", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        ts = at(lt.tm_year, mo, d)
        if ts and ts < now_ts - 86400:            # 已过去 → 视为明年
            ts = at(lt.tm_year + 1, mo, d)
        return ts
    if any(k in raw for k in ("今天", "今日", "今晚")):
        return day_of(now_ts)
    for kw, off in (("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1), ("次日", 1)):
        if kw in raw:
            return day_of(now_ts + off * 86400)
    m = re.search(r"(\d{1,3})\s*天[后内以]*", raw)
    if m:
        return day_of(now_ts + int(m.group(1)) * 86400)
    if "月底" in raw or "月末" in raw:
        nm = lt.tm_mon % 12 + 1
        ny = lt.tm_year + (1 if lt.tm_mon == 12 else 0)
        return day_of(int(time.mktime((ny, nm, 1, 0, 0, 0, 0, 0, -1))) - 86400)
    m = re.search(r"(下{0,1})\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", raw)
    if m:
        next_week = bool(m.group(1))
        wd = WEEKDAYS.get(m.group(2), 0)
        delta = (wd - lt.tm_wday) % 7
        if next_week:
            delta = delta + 7 if delta < 7 else delta       # "下周X" 必是未来
        return day_of(now_ts + delta * 86400)
    return 0


# ---------------------------------------------------------------- 个人档案
def profile_read() -> str:
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return f.read()
    except Exception:
        try:
            atomic_write(PROFILE_PATH, PROFILE_DEFAULT)
        except Exception:
            pass
        return PROFILE_DEFAULT


def profile_write(text: str) -> None:
    atomic_write(PROFILE_PATH, str(text)[:20000])


ECOSYSTEM = [("NapCat WebUI", 6099), ("NapCat 反向WS", 6199),
             ("AstrBot 控制台", 6185), ("存档仪表盘", 8090)]


def ecosystem_ports(cfg: Config) -> list:
    """生态里其它组件的端口探测(它们不属于本程序, 但在同一台设备上)。"""
    out = []
    for name, port in ECOSYSTEM:
        out.append({"name": name, "port": port, "ok": port_open(port)})
    p = int(cfg.data["push"].get("napcat_api_port", 3000))
    out.append({"name": "NapCat API", "port": p, "ok": port_open(p)})
    return out


def state_get(key: str, default=None):
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return (json.load(f) or {}).get(key, default)
    except Exception:
        return default


def state_set(key: str, value) -> None:
    cur = {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            cur = json.load(f) or {}
    except Exception:
        pass
    cur[key] = value
    atomic_write(STATE_PATH, json.dumps(cur, ensure_ascii=False, indent=2) + "\n")


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def now() -> int:
    return int(time.time())


def fmt_ts(ts, f: str = "%m-%d %H:%M:%S") -> str:
    try:
        return time.strftime(f, time.localtime(int(ts)))
    except Exception:
        return "-"
