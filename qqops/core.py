"""QQOps 核心层: 配置 / 存储 / NapCat 客户端 / 服务管理 / 日志。

设计约束:
  * 纯标准库, 可在 Termux 原生 Python 上直接运行;
  * 所有写操作原子化(临时文件 + rename), 避免断电/被杀进程写坏配置;
  * 配置带默认值与校验, 未知键保留(向前兼容);
  * 日志按大小滚动。
"""
from __future__ import annotations

import csv
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

VERSION = "1.1.0"

HOME = os.path.expanduser("~")
BASE = os.environ.get("QQOPS_HOME") or os.path.join(HOME, "qqops")
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
             "token": "", "max_chars": 1200},
    "schedule": {"enabled": True, "times": ["22:00"], "hours": 24},
    "retention": {"message_days": 0, "log_max_mb": 4, "backup_keep": 7},
    "extras": {},   # 按设备覆盖 NapCat/AstrBot 的启动命令(见 core.EXTRAS)
}
SECRET_KEYS = {("web", "token"), ("collector", "secret"),
               ("summarizer", "api_key"), ("push", "token")}


def ensure_dirs() -> None:
    for d in (DATA, LOG_DIR, REPORT_DIR, BACKUP_DIR):
        os.makedirs(d, exist_ok=True)


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
        if not self.data["web"].get("token"):
            self.data["web"]["token"] = gen_token()
            self.save()
        return self.data

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


def log(name: str, level: str, msg: str, max_mb: float = 4.0) -> None:
    """按名字写日志文件, 超过 max_mb 时轮转一次。"""
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
        except Exception:
            pass
        return con

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
    return bool(_run(["pgrep", "-f", pattern], 8).strip())


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
                   "web": "qqops.webapp"}


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


def start_service(name: str) -> str:
    """nohup 后台启动一个服务(不依赖 screen, SSH 断线不受影响)。"""
    mod = SERVICE_MODULES.get(name)
    if not mod:
        return "未知服务: %s" % name
    if pgrep("[-]m %s" % mod):
        return "%s 已在运行" % name
    ensure_dirs()
    os.makedirs(RUN_DIR, exist_ok=True)
    logf = os.path.join(LOG_DIR, "%s.log" % mod.split(".")[-1])
    with open(logf, "ab") as out:
        p = subprocess.Popen([sys.executable, "-m", mod], cwd=_base_dir(),
                             stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    with open(pid_path(name), "w") as f:
        f.write(str(p.pid))
    for _ in range(8):
        if pgrep("[-]m %s" % mod):
            return "%s 已启动 (pid %d)" % (name, p.pid)
        time.sleep(1)
    return "%s 启动失败, 见 %s" % (name, logf)


def stop_service(name: str) -> str:
    mod = SERVICE_MODULES.get(name, "")
    pf = pid_path(name)
    if os.path.isfile(pf):
        try:
            os.kill(int(open(pf).read().strip()), 15)
        except Exception:
            pass
        try:
            os.remove(pf)
        except Exception:
            pass
    if mod:
        kill_pattern("[-]m %s" % mod)
        for _ in range(6):
            if not pgrep("[-]m %s" % mod):
                return "%s 已停止" % name
            time.sleep(1)
        kill_pattern("[-]m %s" % mod, sig=9)
        time.sleep(1)
        return "%s 已停止(强制)" % name
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
        "napcat": one("napcat", "[o]pt/QQ/qq", napcat_port),
    }


def status_snapshot(cfg: Config | None = None) -> dict:
    """给控制台用的完整状态快照(服务/端口/采集/调度/存储/生态)。"""
    cfg = cfg or Config()
    store = Store()
    stats = store.stats(24)
    sch = cfg.data.get("schedule", {})
    nxt = next_run_time(sch.get("times") or [])
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
                      "times": sch.get("times") or [],
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


def next_run_time(times: list, now_ts: int | None = None) -> dict:
    """算出 schedule.times 里最近的下一次触发时间。"""
    now_ts = now_ts or now()
    lt = time.localtime(now_ts)
    best = 0
    for t in (times or []):
        try:
            hh, mm = [int(x) for x in str(t).split(":")]
        except Exception:
            continue
        ts = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1)))
        while ts <= now_ts:
            ts += 86400
        if best == 0 or ts < best:
            best = ts
    return {"ts": best, "in": max(0, best - now_ts) if best else 0}


def count_files(path: str, suffix: str = "") -> int:
    try:
        return len([f for f in os.listdir(path) if f.endswith(suffix)])
    except Exception:
        return 0


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
