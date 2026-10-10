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
import contextlib
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

VERSION = "3.3.8"

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
        # 私聊不入库, 但仍交给插件处理(聊天里问 AI 要用); 关掉它私聊提问就没反应
        "command_private": True,
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
             "token": "", "max_chars": 1200, "report_link": True,
             # 推送形态: text 纯文本 | forward 合并转发卡片 | markdown Markdown 卡片
             "format": "text"},
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
    # 运行形态与资源档位(合并进程 / 低功耗)
    "runtime": {
        "mode": "auto",            # auto | single | multi —— single=四服务合并成一个进程
        "power_mode": "balanced",  # performance | balanced | saver
        "scheduler_tick": 0,       # 0=按档位(5/15/60 秒)
        "watchdog_interval": 0,    # 0=按档位(30/60/180 秒)
        "console_refresh": 0,      # 0=按档位(10/15/60 秒)
        "defer_enrich": None,      # None=按档位; true=富化(媒体/链接)推到夜间窗口
        "enrich_window": "02:00-06:00",
        "sqlite_cache_kb": 0,      # 0=按档位(8000/4000/1000)
        "log_quiet": None,         # None=按档位; true=只记 WARN 及以上
    },
    # 智能查询用的模型配置: 留空则回落到上面的 summarizer(base_url/api_key/model 只维护一份)
    "llm": {
        "base_url": "", "api_key": "", "model": "", "temperature": 0.2,
        "max_tokens": 1500, "timeout": 120, "retries": 2,
        "input_price_per_mtok": 0.0, "output_price_per_mtok": 0.0,
    },
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


@contextlib.contextmanager
def _file_lock(path: str, timeout: float = 3.0):
    """跨进程互斥(基于 O_CREAT|O_EXCL 的锁文件, 不依赖 fcntl, Windows 也能用)。

    锁文件超过 10 秒没更新视为残留(进程崩了没清) → 直接抢过来, 避免死锁。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    end = time.time() + max(0.1, timeout)
    got = False
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, b"%d" % os.getpid())
            os.close(fd)
            got = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 10:
                    os.remove(path)
                    continue
            except Exception:
                pass
            if time.time() >= end:
                break
            time.sleep(0.02)
        except Exception:
            break
    try:
        yield got
    finally:
        if got:
            try:
                os.remove(path)
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
    # Windows 上另一个进程正在读/写目标文件时 os.replace 会报 WinError 32(共享冲突),
    # 重试几次基本都能过;实在过不去就退化成直接写(总比整个操作失败好)。
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            time.sleep(0.05 * (i + 1))
    try:
        os.replace(tmp, path)
    except OSError:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


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


def ensure_utf8_console() -> None:
    """把标准输出/错误切到 UTF-8, 避免在非 UTF-8 控制台上"打印中文就崩"。

    为什么需要: Windows 上控制台默认用系统 ANSI 代码页(西文机器是 cp1252/cp437),
    Python 往那儿 `print` 中文会抛 UnicodeEncodeError —— 服务启动日志、`qqops doctor`
    的中文输出全都会炸(CI 的 windows-latest 上实测过)。切到 UTF-8 + errors=replace:
    能显示就正常显示, 显示不了只丢个别字符, **绝不让程序因为"打印"而死**。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


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
        platform TEXT DEFAULT 'aiocqhttp',
        self INTEGER DEFAULT 0, recalled INTEGER DEFAULT 0)""",
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
    # ---- AI 对话(智能查询)的会话与轮次 ----
    """CREATE TABLE IF NOT EXISTS chat_sessions(
        id TEXT PRIMARY KEY, title TEXT, created INTEGER, updated INTEGER,
        turns INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0,
        tokens_out INTEGER DEFAULT 0, cost REAL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS chat_turns(
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
        sources TEXT, meta TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
        cost REAL DEFAULT 0, ms INTEGER DEFAULT 0, created INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_chat_turns ON chat_turns(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions ON chat_sessions(updated)",
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

# 老库升级: 全部幂等(重复执行只报错不抛), 失败只记日志
MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN self INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN recalled INTEGER DEFAULT 0",
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
            # 缓存大小按功耗档位给(省电档 1MB, 性能档 8MB): 手机上每个连接都吃内存, 必须收紧
            con.execute("PRAGMA cache_size=-%d" % max(256, int(_SQLITE_CACHE_KB)))
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
                _record_soft("store.integrity", "db_corrupt",
                             "数据库自检未通过: %s" % str(detail)[:200], {"db": self.path})
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
        self._migrate()
        if self.fts_ok:
            self.fts_ensure(con_factory=self.conn)

    def _migrate(self) -> None:
        """老库升级: 补列 + 建去重唯一索引(幂等, 失败只记日志不挡启动)。

        去重是硬要求 —— NapCat 断线重连会补发离线消息, 没有唯一约束就会重复入库。
        历史库里可能已经有重复, 所以先清理再建唯一索引。
        """
        con = self.conn()
        try:
            for sql in MIGRATIONS:
                try:
                    con.execute(sql)
                except Exception:
                    pass                      # 列已存在
            has_idx = con.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                                  "AND name='idx_msg_dedup'").fetchone()
            con.commit()
        except Exception as e:
            log("core", "WARN", "迁移失败: %s" % e)
            return
        finally:
            con.close()
        if has_idx:
            return
        try:
            n = self.dedupe_messages()
            if n:
                log("core", "INFO", "清理历史重复消息 %d 条" % n)
            con = self.conn()
            try:
                con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_dedup "
                            "ON messages(group_id, message_id) WHERE message_id > 0")
                con.commit()
                log("core", "INFO", "已建立消息去重唯一索引(group_id, message_id)")
            finally:
                con.close()
        except Exception as e:
            log("core", "WARN", "去重索引建立失败(不影响使用): %s" % e)

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
    def insert_message(self, ev: dict) -> str:
        """落库一条消息。返回 `"inserted"` / `"duplicate"` / `"empty"`。

        去重是硬要求: NapCat 断线重连后会补发离线期间的消息, 没有唯一约束就会重复入库。
        遇到 SQLite 忙锁会退避重试, 避免偶发锁冲突丢消息。
        """
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

    def _insert_once(self, ev: dict) -> str:
        mtype = ev.get("message_type") or "group"
        gid = int(ev.get("group_id") or 0)
        sd = ev.get("sender") or {}
        msg = ev.get("message")
        raw = ev.get("raw_message") or ""
        if not raw and not msg:
            return "empty"                      # 空消息(协议里的心跳式消息)不入库
        kinds = kinds_of(msg)
        nick = sd.get("nickname") or ""
        mid = int(ev.get("message_id") or 0)
        ts = int(ev.get("time") or time.time())
        now_ts = int(time.time())
        if ts > now_ts + 86400:                 # 手机时钟回拨/未来时间: 以本地时间为准, 不污染时间线
            ts = now_ts
        is_self = 1 if (ev.get("post_type") == "message_sent" or ev.get("self_id") and
                        int(ev.get("user_id") or 0) == int(ev.get("self_id") or 0)) else 0
        con = self.conn()
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO messages(ts,message_type,group_id,user_id,nickname,card,"
                "message_id,raw_message,segments,kinds,platform,self,recalled) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (ts, mtype, gid, int(ev.get("user_id") or 0),
                 nick, sd.get("card") or "", mid,
                 raw, json.dumps(msg, ensure_ascii=False), kinds,
                 ev.get("platform") or "aiocqhttp", is_self))
            if cur.rowcount == 0 and mid:       # 唯一索引拦下的重复消息
                return "duplicate"
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
            return "inserted"
        finally:
            con.close()

    def mark_recalled(self, group_id: int, message_id: int) -> int:
        """标记消息已被撤回(OneBot notice/group_recall)。返回受影响条数。"""
        con = self.conn()
        try:
            cur = con.execute("UPDATE messages SET recalled=1 WHERE group_id=? AND message_id=?",
                              (int(group_id or 0), int(message_id or 0)))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def dedupe_messages(self) -> int:
        """清掉历史重复消息(同群同 message_id 只留最早一条), 为唯一索引铺路。"""
        con = self.conn()
        try:
            cur = con.execute(
                """DELETE FROM messages WHERE message_id>0 AND id NOT IN (
                       SELECT MIN(id) FROM messages WHERE message_id>0
                       GROUP BY group_id, message_id)""")
            con.commit()
            return cur.rowcount
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
                 page: int = 1, size: int = 50, order: str = "desc",
                 include_self: bool = True, exclude_recalled: bool = False) -> dict:
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
        if not include_self:
            where.append("COALESCE(m.self,0)=0")
        if exclude_recalled:
            where.append("COALESCE(m.recalled,0)=0")
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
        order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
        con = self.conn()
        try:
            total = con.execute("SELECT COUNT(*) c FROM messages m WHERE " + w, args).fetchone()["c"]
            rows = [dict(r) for r in con.execute(
                """SELECT m.id,m.ts,m.group_id,COALESCE(g.name,'') group_name,m.user_id,m.nickname,
                          m.raw_message,m.kinds,COALESCE(m.self,0) self,COALESCE(m.recalled,0) recalled,
                          m.message_id
                   FROM messages m LEFT JOIN groups g ON g.id=m.group_id
                   WHERE %s ORDER BY m.ts %s LIMIT ? OFFSET ?""" % (w, order_sql),
                args + [size, (page - 1) * size])]
            return {"rows": rows, "total": total, "page": page, "size": size,
                    "pages": max(1, (total + size - 1) // size),
                    "mode": mode, "fts_available": self.fts_ok}
        finally:
            con.close()

    # -- 对话(AI 智能查询的会话记录)
    def chat_session_create(self, title: str = "") -> str:
        import uuid
        sid = uuid.uuid4().hex[:12]
        con = self.conn()
        try:
            con.execute("INSERT INTO chat_sessions(id,title,created,updated,turns) VALUES(?,?,?,?,0)",
                        (sid, str(title or "")[:80], int(time.time()), int(time.time())))
            con.commit()
            return sid
        finally:
            con.close()

    def chat_session_touch(self, sid: str, title: str = "") -> None:
        con = self.conn()
        try:
            if title:
                con.execute("UPDATE chat_sessions SET updated=?, turns=turns+1, title=? WHERE id=?",
                            (int(time.time()), str(title)[:80], sid))
            else:
                con.execute("UPDATE chat_sessions SET updated=?, turns=turns+1 WHERE id=?", (int(time.time()), sid))
            con.commit()
        finally:
            con.close()

    def chat_append(self, sid: str, role: str, content: str, sources=None, meta=None,
                    tokens_in: int = 0, tokens_out: int = 0, cost: float = 0.0, ms: int = 0) -> int:
        con = self.conn()
        try:
            cur = con.execute(
                "INSERT INTO chat_turns(session_id,role,content,sources,meta,tokens_in,tokens_out,"
                "cost,ms,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sid, str(role), str(content or ""),
                 json.dumps(sources or [], ensure_ascii=False),
                 json.dumps(meta or {}, ensure_ascii=False),
                 int(tokens_in), int(tokens_out), float(cost), int(ms), int(time.time())))
            con.execute("UPDATE chat_sessions SET updated=?, tokens_in=tokens_in+?, "
                        "tokens_out=tokens_out+?, cost=cost+? WHERE id=?",
                        (int(time.time()), int(tokens_in), int(tokens_out), float(cost), sid))
            con.commit()
            return int(cur.lastrowid or 0)
        finally:
            con.close()

    def chat_session(self, sid: str, limit: int = 200) -> dict:
        con = self.conn()
        try:
            s = con.execute("SELECT * FROM chat_sessions WHERE id=?", (sid,)).fetchone()
            if not s:
                return {}
            turns = [dict(r) for r in con.execute(
                "SELECT * FROM chat_turns WHERE session_id=? ORDER BY id ASC LIMIT ?", (sid, int(limit)))]
            for t in turns:
                for k in ("sources", "meta"):
                    try:
                        t[k] = json.loads(t.get(k) or "[]") if k == "sources" else json.loads(t.get(k) or "{}")
                    except Exception:
                        t[k] = [] if k == "sources" else {}
            return {**dict(s), "turns": turns}
        finally:
            con.close()

    def chat_sessions(self, limit: int = 50) -> list:
        con = self.conn()
        try:
            return [dict(r) for r in con.execute(
                "SELECT id,title,created,updated,turns,tokens_in,tokens_out,cost FROM chat_sessions "
                "ORDER BY updated DESC LIMIT ?", (int(limit),))]
        finally:
            con.close()

    def chat_session_delete(self, sid: str) -> int:
        con = self.conn()
        try:
            n = con.execute("DELETE FROM chat_turns WHERE session_id=?", (sid,)).rowcount
            con.execute("DELETE FROM chat_sessions WHERE id=?", (sid,))
            con.commit()
            return int(n or 0)
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
    """把 OneBot 消息段类型归一成可统计的"类型串"。

    归一化别名(与 CQ 码类型对齐): record→voice, mface/marketface→face,
    node/forward→forward, flash→image。控制台与采集过滤都按这个串判断。
    """
    if isinstance(msg, str):
        return "text"
    alias = {"record": "voice", "mface": "face", "marketface": "face",
             "node": "forward", "flash": "image"}
    ks = []
    for s in (msg or []):
        t = (s or {}).get("type") or "?"
        t = alias.get(t, t)
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

    # ------------------------------------------------------------ 通用/卡片
    def send_message(self, message: list, user_id: int = 0, group_id: int = 0,
                     timeout: int = 30) -> dict:
        """发任意消息段数组(OneBot v11 message 数组)。"""
        action = "send_group_msg" if group_id else "send_private_msg"
        payload = {"message": list(message or [])}
        payload["group_id" if group_id else "user_id"] = int(group_id or user_id)
        return self.call(action, payload, timeout)

    def send_card(self, card: dict, user_id: int = 0, group_id: int = 0,
                  fallback_text: str = "", reply_to: int = 0) -> dict:
        """发自定义 JSON 卡片(mini-app 卡片)。失败或 retcode!=0 时自动退回纯文本。

        ⚠️ 现实约束(2026-10 实测): 新版 QQ 要求卡片数据**带签名**, 自己拼的
        `com.tencent.miniapp_01` / `com.tencent.structmsg` 会被客户端判为
        "卡片版本过低" 而不渲染。签名卡片的官方接口是 NapCat 的 `get_mini_app_ark`
        (见 `send_miniapp_card`), 而它依赖 PacketBackend, 只支持特定 QQ 版本区间。
        所以这里永远给一份 fallback_text, 并把失败原因记进日志。
        """
        segs = []
        if reply_to:
            segs.append({"type": "reply", "data": {"id": str(reply_to)}})
        segs.append({"type": "json", "data": {"data": json.dumps(card or {}, ensure_ascii=False)}})
        try:
            res = self.send_message(segs, user_id=user_id, group_id=group_id)
        except Exception as e:                       # noqa: BLE001
            res = {"status": "error", "error": str(e)}
        if str(res.get("status")) == "ok" and int(res.get("retcode") or 0) == 0:
            return res
        log("core", "WARN", "JSON 卡片发送失败(客户端可能提示卡片版本过低): %s"
            % str(res.get("message") or res.get("error") or res)[:120])
        if fallback_text:
            try:
                if group_id:
                    return self.send_group(group_id, fallback_text)
                return self.send_private(user_id, fallback_text)
            except Exception as e:                   # noqa: BLE001
                return {"status": "error", "error": str(e), "card_failed": res}
        return res

    # ------------------------------------------------------------ Markdown 卡片
    def send_markdown(self, content: str, user_id: int = 0, group_id: int = 0,
                      fallback_text: str = "", reply_to: int = 0,
                      timeout: int = 45) -> dict:
        """发 **Markdown 卡片**(OneBot v11 扩展段 `{"type":"markdown","data":{"content":…}}`)。

        ⚠️ 实测约束: NapCat 的 markdown 是**走 PacketBackend 的 protobuf 通道**实现的
        (`napcat-core/packet/message/element.ts` 的 `PacketMsgMarkDownElement` → `MarkdownData`),
        与签名小程序卡片同一个前置条件。PacketBackend 官方支持矩阵里 Linux arm64 只覆盖
        QQ 构建号 28498-36580; 超出范围时 NapCat 返回
        `Timeout: NTEvent … NodeIKernelMsgService/sendMsg … onMsgInfoListUpdate` 且消息**发不出去**。
        因此失败**一定**退回 `fallback_text` 并记日志; 换了 QQ/NapCat 版本后同一个开关自动生效。
        """
        segs = []
        if reply_to:
            segs.append({"type": "reply", "data": {"id": str(reply_to)}})
        segs.append({"type": "markdown", "data": {"content": str(content or "")}})
        try:
            res = self.send_message(segs, user_id=user_id, group_id=group_id, timeout=timeout)
        except Exception as e:                       # noqa: BLE001
            res = {"status": "error", "error": str(e)}
        if str(res.get("status")) == "ok" and int(res.get("retcode") or 0) == 0:
            return res
        log("core", "WARN", "Markdown 卡片发送失败(需要 PacketBackend 支持当前 QQ 版本): %s"
            % str(res.get("message") or res.get("error") or res)[:160])
        if fallback_text:
            try:
                if group_id:
                    return self.send_group(group_id, fallback_text)
                return self.send_private(user_id, fallback_text)
            except Exception as e:                   # noqa: BLE001
                return {"status": "error", "error": str(e), "markdown_failed": res}
        return res

    # ------------------------------------------------------------ 签名卡片/分享卡片
    def miniapp_ark(self, title: str, desc: str = "", pic_url: str = "", jump_url: str = "",
                    template: str = "bili", web_url: str = "") -> tuple:
        """用 NapCat 官方接口生成**带签名**的小程序卡片数据。

        返回 `(ark, error)`: 成功时 ark 是可直接发的 JSON 字符串。
        失败原因通常是 PacketBackend 不可用(QQ 版本超出支持区间)——
        原文照抄, 便于在控制台上直接看到"为什么卡片发不出去"。
        """
        payload = {"type": template, "title": title or "", "desc": desc or "",
                   "picUrl": pic_url or "", "jumpUrl": jump_url or "https://qq.com"}
        if web_url:
            payload["webUrl"] = web_url
        try:
            res = self.call("get_mini_app_ark", payload, 40)
        except Exception as e:                       # noqa: BLE001
            return "", "%s: %s" % (type(e).__name__, e)
        if str(res.get("status")) != "ok" or int(res.get("retcode") or 0) != 0:
            return "", str(res.get("message") or res.get("wording") or res)[:300]
        data = res.get("data")
        ark = data.get("ark") if isinstance(data, dict) else data
        if isinstance(ark, (dict, list)):
            ark = json.dumps(ark, ensure_ascii=False)
        return (ark or ""), ("" if ark else "接口没有返回 ark 数据")

    def send_miniapp_card(self, title: str, desc: str = "", pic_url: str = "", jump_url: str = "",
                          user_id: int = 0, group_id: int = 0, fallback_text: str = "",
                          template: str = "bili") -> dict:
        """发**签名**小程序卡片; 拿不到签名就自动退回 `fallback_text`。"""
        ark, err = self.miniapp_ark(title, desc, pic_url, jump_url, template)
        if not ark:
            log("core", "WARN", "签名卡片不可用, 已退回文本: %s" % err)
            if fallback_text:
                try:
                    if group_id:
                        return self.send_group(group_id, fallback_text)
                    return self.send_private(user_id, fallback_text)
                except Exception as e:               # noqa: BLE001
                    return {"status": "error", "error": str(e), "ark_error": err}
            return {"status": "failed", "error": err}
        try:
            return self.send_message([{"type": "json", "data": {"data": ark}}],
                                     user_id=user_id, group_id=group_id)
        except Exception as e:                       # noqa: BLE001
            if fallback_text:
                return self.send_private(user_id, fallback_text)
            return {"status": "error", "error": str(e)}

    def ark_share_peer(self, user_id: int = 0, group_id: int = 0) -> tuple:
        """官方**分享**卡片(联系人/群): 走 NTQQ 原生分享, 自带签名, 不依赖 PacketBackend。

        返回 `(ark, error)`。实测 NapCat 4.18.33 可用(retcode 0, app=com.tencent.contact.lua)。
        """
        payload = {"group_id": int(group_id)} if group_id else {"user_id": int(user_id)}
        try:
            res = self.call("ArkSharePeer", payload, 30)
        except Exception as e:                       # noqa: BLE001
            return "", "%s: %s" % (type(e).__name__, e)
        data = res.get("data") or {}
        ark = data.get("arkMsg") or ""
        if not ark:
            return "", str(data.get("errMsg") or res.get("message") or "没有返回 arkMsg")[:200]
        return ark, ""

    def send_share_card(self, user_id: int = 0, group_id: int = 0, fallback_text: str = "") -> dict:
        """发官方分享卡片(联系人/群), 失败退回文本。"""
        ark, err = self.ark_share_peer(user_id=user_id, group_id=group_id)
        if not ark:
            log("core", "WARN", "分享卡片不可用: %s" % err)
            if fallback_text:
                return (self.send_group(group_id, fallback_text) if group_id
                        else self.send_private(user_id, fallback_text))
            return {"status": "failed", "error": err}
        target = {"group_id": int(group_id)} if group_id else {"user_id": int(user_id)}
        return self.send_message([{"type": "json", "data": {"data": ark}}], **target)

    def send_forward(self, nodes: list, user_id: int = 0, group_id: int = 0,
                     timeout: int = 40) -> dict:
        """发**合并转发**(客户端显示为"聊天记录"卡片): 内容任意, 不需要签名。

        `nodes` 用 `core.forward_node()` 构造。实测 `send_private_forward_msg` /
        `send_forward_msg` 在 NapCat 4.18.33 上均可用。
        """
        action = "send_group_forward_msg" if group_id else "send_private_forward_msg"
        payload = {"messages": list(nodes or [])}
        payload["group_id" if group_id else "user_id"] = int(group_id or user_id)
        try:
            res = self.call(action, payload, timeout)
        except Exception as e:                       # noqa: BLE001
            return {"status": "error", "error": str(e)}
        if str(res.get("status")) != "ok" or int(res.get("retcode") or 0) != 0:
            # 退回通用 send_forward_msg(部分版本只实现了一个)
            try:
                payload2 = {"message": list(nodes or []),
                            "group_id" if group_id else "user_id": int(group_id or user_id)}
                res2 = self.call("send_forward_msg", payload2, timeout)
                if str(res2.get("status")) == "ok" and int(res2.get("retcode") or 0) == 0:
                    return res2
            except Exception:
                pass
        return res

    def send_text(self, text: str, user_id: int = 0, group_id: int = 0, max_chars: int = 1200,
                  reply_to: int = 0) -> int:
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
        sent = 0
        for i, c in enumerate(chunks):
            segs = []
            if reply_to and i == 0:                  # 引用只挂在第一段, 免得刷屏
                segs.append({"type": "reply", "data": {"id": str(reply_to)}})
            segs.append({"type": "text", "data": {"text": c}})
            if group_id:
                self.send_message(segs, group_id=group_id)
            else:
                self.send_message(segs, user_id=user_id)
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


def forward_node(name: str, uin: int, text: str, title: str = "") -> dict:
    """构造一个合并转发节点(客户端显示为聊天记录卡片里的一条)。

    `uin` 只能是真实存在的 QQ 号或 0(部分版本对伪造 uin 有限制);
    标题类内容建议用 `title` 放在第一行, 免得 QQ 把昵称当成标题显示错。
    """
    body = ("%s\n%s" % (title, text)) if title else str(text or "")
    return {"type": "node", "data": {
        "uin": str(int(uin or 0)),
        "name": name or "QQOps",
        "content": [{"type": "text", "data": {"text": body[:4000]}}]}}


def qq_card(title: str, desc: str = "", tag: str = "QQOps", url: str = "",
            preview: str = "", icon: str = "") -> dict:
    """构造 QQ 小程序卡片(JSON 卡片)的载荷, 交给 `NapCat.send_card` 发出去。

    这是 QQ 客户端认的 ark/mini-app 结构: `meta.detail_1` 是卡片正文。
    卡片只在 QQ 里好看, 内容要**自包含**(客户端不一定会渲染长文本),
    所以调用方同时准备一份纯文本兜底。
    """
    url = url or "https://qq.com"
    return {
        "app": "com.tencent.miniapp_01",
        "desc": tag,
        "view": "news",
        "ver": "0.0.0.1",
        "prompt": "[%s] %s" % (tag, (title or "")[:40]),
        "config": {"forward": 1, "type": "normal", "autosize": True},
        "meta": {"detail_1": {
            "appid": "1109937557",
            "title": (title or "")[:60],
            "desc": (desc or "")[:120],
            "icon": icon or preview or "https://q1.qlogo.cn/g?b=qq&nk=10000&s=100",
            "preview": preview or icon or "https://q1.qlogo.cn/g?b=qq&nk=10000&s=100",
            "qqdocurl": url,
            "url": url,
        }},
    }


def cq_plain(raw: str) -> str:
    """把带 CQ 码的原始消息变成人话: 去 CQ 码、图片/卡片换成占位、压空白。"""
    t = str(raw or "")
    t = re.sub(r"\[CQ:json[^\n]*", "[卡片]", t)
    t = re.sub(r"\[CQ:(?:xml|forward)[^\n]*", "[转发]", t)
    t = re.sub(r"\[CQ:image[^\]]*\]", "[图片]", t)
    t = re.sub(r"\[CQ:record[^\]]*\]", "[语音]", t)
    t = re.sub(r"\[CQ:file[^\]]*\]", "[文件]", t)
    t = re.sub(r"\[CQ:([a-zA-Z_]+)(?:[^\[\]]|\[[^\[\]]*\])*\]", "", t)
    return re.sub(r"\s+", " ", t).strip()


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
    if not out and not _is_windows():           # macOS/BSD: 没有 /proc, 用 ps 往上爬
        out = _ancestor_pids_ps()
    return out


def _ancestor_pids_ps() -> set:
    """用 `ps -o ppid=` 往上爬父进程链(macOS/BSD 没有 /proc 时用)。"""
    out, pid = set(), os.getpid()
    for _ in range(12):
        raw = _run(["ps", "-o", "ppid=", "-p", str(pid)], 8).strip()
        if not raw.isdigit():
            break
        ppid = int(raw)
        if ppid <= 1 or ppid in out:
            break
        out.add(ppid)
        pid = ppid
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


# ---------------------------------------------------------------- 运行形态与功耗档位
RUNNER_PATH = os.path.join(RUN_DIR, "runner.json")

# 三档功耗预设: 键名与 runtime 配置一一对应, 便于控制台直接展示"当前生效值"
POWER_PROFILES = {
    "performance": {
        "label": "性能", "desc": "响应最快, 资源占用最高",
        "scheduler_tick": 5, "watchdog_interval": 30, "console_refresh": 10,
        "defer_enrich": False, "sqlite_cache_kb": 8000, "log_quiet": False,
    },
    "balanced": {
        "label": "均衡", "desc": "默认档位, 日常使用",
        "scheduler_tick": 15, "watchdog_interval": 60, "console_refresh": 15,
        "defer_enrich": False, "sqlite_cache_kb": 4000, "log_quiet": False,
    },
    "saver": {
        "label": "省电", "desc": "唤醒次数最少: 定时更疏、富化推到夜间、内存更省",
        "scheduler_tick": 60, "watchdog_interval": 180, "console_refresh": 60,
        "defer_enrich": True, "sqlite_cache_kb": 1000, "log_quiet": True,
    },
}
_SQLITE_CACHE_KB = 4000          # 由 apply_power_profile() 按档位设置, Store.conn() 读取


def _is_android() -> bool:
    """Termux/Android 判定: 手机上默认单进程(省内存), 桌面上默认多进程(隔离更好)。"""
    if os.environ.get("PREFIX", "").find("com.termux") >= 0:
        return True
    return os.path.exists("/system/bin/app_process") or os.path.exists("/system/bin/app_process64")


def resolve_mode(cfg: Config, android: bool = None) -> str:
    """把 `runtime.mode` 解析成 single / multi。`auto` = 手机上单进程, 其它多进程。"""
    try:
        mode = str((cfg.data.get("runtime") or {}).get("mode") or "auto").lower()
    except Exception:
        mode = "auto"
    if mode in ("single", "multi"):
        return mode
    android = _is_android() if android is None else bool(android)
    return "single" if android else "multi"


def power_profile(cfg: Config = None) -> dict:
    """当前生效的功耗档位参数(用户覆盖 > 档位预设)。"""
    cfg = cfg or Config()
    rt = dict((cfg.data.get("runtime") or {}))
    name = str(rt.get("power_mode") or "balanced").lower()
    if name not in POWER_PROFILES:
        name = "balanced"
    prof = dict(POWER_PROFILES[name])
    prof["power_mode"] = name
    for k in ("scheduler_tick", "watchdog_interval", "console_refresh", "sqlite_cache_kb"):
        v = rt.get(k)
        try:
            v = int(v or 0)
        except Exception:
            v = 0
        if v > 0:
            prof[k] = v
    for k in ("defer_enrich", "log_quiet"):
        v = rt.get(k)
        if v is not None:
            prof[k] = bool(v)
    prof["enrich_window"] = str(rt.get("enrich_window") or "02:00-06:00")
    prof["all_profiles"] = {k: {"label": v["label"], "desc": v["desc"]} for k, v in POWER_PROFILES.items()}
    return prof


def apply_power_profile(cfg: Config = None) -> dict:
    """进程启动时调用: 把档位里影响全局的设置落实(SQLite 缓存等)。"""
    global _SQLITE_CACHE_KB
    prof = power_profile(cfg)
    try:
        _SQLITE_CACHE_KB = max(256, int(prof.get("sqlite_cache_kb") or 4000))
    except Exception:
        _SQLITE_CACHE_KB = 4000
    return prof


def should_enrich_now(cfg: Config = None, ts: float = None) -> bool:
    """现在是否该做"贵"的富化(抓图片/抓链接)。

    省电档把富化推到夜间窗口(默认 02:00-06:00), 白天只落库, 晚上批量补 ——
    这是白天省电最有效的一刀(网络请求 + 图片解码都是耗电大户)。
    """
    prof = power_profile(cfg)
    if not prof.get("defer_enrich"):
        return True
    win = str(prof.get("enrich_window") or "02:00-06:00")
    try:
        a, b = win.split("-", 1)
        ah, am = [int(x) for x in a.strip().split(":")]
        bh, bm = [int(x) for x in b.strip().split(":")]
    except Exception:
        return True
    lt = time.localtime(ts or now())
    cur = lt.tm_hour * 60 + lt.tm_min
    start, end = ah * 60 + am, bh * 60 + bm
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end        # 跨零点窗口


def beat_path(name: str) -> str:
    """单个心跳文件路径(每个服务一个, 避免多进程读改写同一个 JSON)。"""
    return os.path.join(RUN_DIR, "beat_%s" % name)


def beat(name: str, ts: float = None) -> None:
    """服务/线程心跳(单进程模式下靠它判断每个服务是否活着)。

    同时写老键(`<name>_beat`, 多进程的 scheduler/watchdog 模块一直用它),
    这样两种模式的读者看到的是同一次心跳, 不会出现"单进程里调度显示超时"。
    """
    t = int(ts or now())
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        atomic_write(beat_path(name), str(t))
    except Exception as e:                       # noqa: BLE001 —— 心跳写不进去不能影响服务
        log("core", "WARN", "写心跳失败(%s): %s" % (name, e))
        try:
            state_set("beat_%s" % name, t)        # 退路: 写共享 state(可能丢, 但聊胜于无)
        except Exception:
            pass


def last_beat(name: str) -> int:
    """某服务最近一次心跳。

    读取顺序: 独立心跳文件(新) → state.json 的 `beat_x` → 老键 `x_beat`;取最新值。
    这样多进程并发写也不会互相覆盖, 升级后老数据仍能读到。
    """
    best = 0
    try:
        with open(beat_path(name), encoding="utf-8") as f:
            best = int((f.read() or "0").strip() or 0)
    except Exception:
        pass
    for key in ("beat_%s" % name, "%s_beat" % name):
        try:
            best = max(best, int(state_get(key, 0) or 0))
        except Exception:
            continue
    return best


def beats() -> dict:
    return {n: last_beat(n) for n in ("collector", "scheduler", "web", "watchdog")}


def runtime_state() -> dict:
    """读运行形态清单。返回 `{mode, pid, services, started, version, alive}`。

    多进程模式没有这个文件(靠 pgrep 判断); 单进程模式由 runner 写入并定期刷新。
    """
    try:
        with open(RUNNER_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return {"mode": "multi", "pid": 0, "services": [], "alive": False}
    pid = int(d.get("pid") or 0)
    alive = bool(pid) and _pid_alive(pid)
    d["alive"] = alive
    d.setdefault("mode", "single")
    d.setdefault("services", [])
    return d


def runtime_write(mode: str, services: list, pid: int = None) -> None:
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        atomic_write(RUNNER_PATH, json.dumps(
            {"mode": mode, "pid": int(pid or os.getpid()), "services": list(services),
             "started": now(), "version": VERSION},
            ensure_ascii=False, indent=1))
    except Exception as e:
        log("core", "WARN", "写入运行清单失败: %s" % e)


def runtime_clear() -> None:
    try:
        if os.path.exists(RUNNER_PATH):
            os.remove(RUNNER_PATH)
    except Exception:
        pass


def _record_soft(component: str, kind: str, message: str, extra: dict = None) -> None:
    """把一条记录写进崩溃账本(拿不到框架层就跳过 —— core 是地基, 不能因记账失败而报错)。"""
    try:
        from .framework import errors
        errors.record(component, kind=kind, message=message, extra=extra)
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """进程是否活着。

    两个必须处理的坑:
      * **Windows**: `os.kill(pid, 0)` 其实是 `TerminateProcess` —— 探活即杀人(踩过),
        改用 ctypes 的 OpenProcess + GetExitCodeProcess;
      * **僵尸进程**: 子进程退出、父进程还没 wait 时 `kill(pid, 0)` 依然成功,
        会把"已经停掉的实例"永远判成活着(踩过: 停止实例误报失败), 所以先看 /proc/<pid>/stat 的状态位。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 两条禁忌:
        #   1) os.kill(pid, 0) 是 TerminateProcess —— 探活即杀人;
        #   2) tasklist/wmic/CIM 在受限环境里会被拒。
        # 所以直接用 ctypes 调 OpenProcess + GetExitCodeProcess(259 = STILL_ACTIVE)。
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong(0)
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:                                             # Linux: 先确认不是僵尸
        with open("/proc/%d/stat" % pid) as f:
            state = f.read().rsplit(")", 1)[-1].split()
        if state and state[0] == "Z":
            return False
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _is_windows() -> bool:
    """是否 Windows。

    单独抽出来是为了**可测**: macOS/BSD 的兜底分支(ps/lsof)在 Windows 上跑不到,
    测试里把 `core._is_windows` 打成 False 就能验证那些分支(否则只能靠真机 macOS 才发现)。
    """
    return os.name == "nt"


def proc_rss(pid: int) -> int:
    """某进程常驻内存(字节)。

    Linux/Termux 读 /proc;macOS/BSD 没有 /proc, 退回 `ps -o rss=`(单位 KB);
    Windows 返回 0(没有等价且可靠的零依赖取法)。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return 0
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    if not _is_windows():                       # macOS/BSD: ps 的 rss 单位是 KB
        out = _run(["ps", "-o", "rss=", "-p", str(pid)], 8).strip()
        if out.isdigit():
            return int(out) * 1024
    return 0


def self_rss() -> int:
    return proc_rss(os.getpid())


def _rss_and_etime(pid: int) -> tuple:
    """RSS + 运行时长(读 /proc, 拿不到就退回 proc_info)。"""
    rss = proc_rss(pid)
    etime = ""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            parts = f.read().split()
        start = int(parts[21]) / os.sysconf("SC_CLK_TCK")
        up = float(open("/proc/uptime").read().split()[0])
        secs = max(0, int(up - start))
        etime = "%02d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)
    except Exception:
        pass
    if not etime and not _is_windows():       # macOS/BSD: ps 的 etime 形如 [[dd-]hh:]mm:ss
        raw = _run(["ps", "-o", "etime=", "-p", str(int(pid))], 8).strip()
        if raw:
            days, rest = 0, raw
            if "-" in raw:                    # 只有真的带 '-' 时, 前面那段才是天数
                head, _, tail = raw.partition("-")
                days = int(head) if head.strip().isdigit() else 0
                rest = tail
            bits = [int(x) for x in rest.split(":") if x.strip().isdigit()]
            if len(bits) == 3:
                h, m, sec = bits
            elif len(bits) == 2:
                h, m, sec = 0, bits[0], bits[1]
            elif len(bits) == 1:
                h, m, sec = 0, 0, bits[0]
            else:
                h = m = sec = 0
            etime = "%02d:%02d:%02d" % (h + days * 24, m, sec)
    return rss, etime


def _health_single(name: str, cfg: Config, rt: dict, timeout: float = 8.0) -> dict:
    """单进程模式的健康判定: 主进程活着 + 该服务的信号(端口/心跳)新鲜。"""
    pid = int(rt.get("pid") or 0)
    if not pid or not _pid_alive(pid):
        return {"ok": False, "why": "主进程不在(pid %s)" % (pid or "?")}
    if name == "watchdog":
        # 主管的首轮体检要跑调度等活, 慢机器上可能还没写心跳 → 等到超时再判
        end = time.time() + max(1.0, timeout / 3.0)
        beat = last_beat("watchdog")
        while not (beat > 0 and now() - beat < 300):
            if time.time() >= end:
                return {"ok": False, "why": "主管心跳 %s" % (fmt_ts(beat) if beat else "无")}
            time.sleep(0.5)
            beat = last_beat("watchdog")
        return {"ok": True, "why": "主管心跳 %s" % fmt_ts(beat)}
    if name == "scheduler":
        end = time.time() + max(1.0, timeout / 3.0)
        while True:
            beat = last_beat("scheduler")
            if beat and now() - beat < max(90, int(power_profile(cfg)["scheduler_tick"]) * 4):
                return {"ok": True, "why": "心跳正常"}
            if time.time() >= end:
                return {"ok": False, "why": "心跳超时(单进程调度线程可能卡住)"}
            time.sleep(1)
    port = int(cfg.data["collector" if name == "collector" else "web"]["port"])
    # 单进程刚起来时端口可能还没 bind(慢机器/CI 冷启动), 所以和多进程分支一样等到超时
    end = time.time() + max(1.0, timeout / 3.0)
    while True:
        if port_open(port):
            return {"ok": True, "why": "端口 %d 已应答" % port}
        if time.time() >= end:
            return {"ok": False, "why": "端口 %d 无应答" % port}
        time.sleep(0.5)


def service_health(name: str, cfg: Config | None = None, timeout: float = 12.0) -> dict:
    """真实健康检查: 不只看进程在不在, 还要端口能应答 / 心跳是新的。

    只判断 pgrep 会把"启动了但立刻崩掉"误报成成功 —— 手机上很常见。
    """
    cfg = cfg or Config()
    mod = SERVICE_MODULES.get(name)
    if not mod:
        return {"ok": False, "why": "未知服务"}
    rt = runtime_state()
    if rt.get("mode") == "single" and name in (rt.get("services") or []):
        return _health_single(name, cfg, rt)
    if not pgrep("[-]m %s" % mod):
        return {"ok": False, "why": "进程不存在"}
    end = time.time() + timeout
    if name == "watchdog":
        beat = 0
        while time.time() < end:                 # 刚启动时首轮体检还没跑完, 要等一等
            beat = last_beat("watchdog")
            if beat and now() - beat < 180:
                return {"ok": True, "why": "心跳 %s" % fmt_ts(beat)}
            time.sleep(1)
        return {"ok": False, "why": "心跳 %s" % (fmt_ts(beat) if beat else "无")}
    if name == "scheduler":
        win = max(90, int(power_profile(cfg)["scheduler_tick"]) * 3)   # 省电档 60s 一跳也要算得过
        while time.time() < end:
            beat = last_beat("scheduler")
            if now() - beat < win:
                return {"ok": True, "why": "心跳正常"}
            time.sleep(1)
        return {"ok": False, "why": "心跳超时(%d 秒内没有心跳)" % win}
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
        pass
    if not _is_windows():                       # macOS/BSD: ps 的 command 列
        return _run(["ps", "-o", "command=", "-p", str(int(pid))], 8).strip()
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
    """终止进程。

    Windows 的坑: `os.kill` 内部用 `OpenProcess(PROCESS_ALL_ACCESS)` + `TerminateProcess`,
    在受限环境下常被拒 —— 于是"看起来停了, 其实还在跑"(真踩过: runner 报已停止但仍在监听端口)。
    这里改用最小权限 `PROCESS_TERMINATE`, 成功与否如实返回。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_TERMINATE = 0x0001
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not h:
                return False
            try:
                return bool(k32.TerminateProcess(h, 1))
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, sig)
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


def _port_owner_lsof(port: int) -> dict:
    """用 `lsof` 查监听进程(macOS/BSD 没有 /proc/net/tcp; lsof 是系统自带的)。

    解析 `-Fpcn` 的机器可读输出: p<pid> / c<command> / n<地址>。
    """
    out = _run(["lsof", "-nP", "-iTCP:%d" % int(port), "-sTCP:LISTEN", "-Fpcn"], 10)
    pid, cmd = 0, ""
    for line in out.splitlines():
        if line.startswith("p") and line[1:].strip().isdigit():
            pid = int(line[1:].strip())
        elif line.startswith("c"):
            cmd = line[1:].strip()
        if pid and cmd:
            break
    if not pid:
        return {}
    return {"ok": True, "port": int(port), "pid": pid, "cmd": cmd,
            "env": _pid_environ(pid), "source": "lsof"}


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
        return _port_owner_lsof(port)
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


def _pids_from_pidfiles(mod: str = "") -> list:
    """从 data/run/*.pid 读出本机安装记录的服务进程(全平台可用的"自己人"凭据)。

    环境变量证明(QQOPS_DATA 比对)更精确, 但 macOS 读不到别人的 environ;
    pid 文件是我们自己写下的, 不需要任何权限, 所以作为**主凭据**。
    """
    out = []
    for name, m in SERVICE_MODULES.items():
        try:
            with open(pid_path(name), encoding="utf-8") as f:
                pid = int((f.read() or "0").strip() or 0)
        except Exception:
            continue
        if pid <= 0 or not _pid_alive(pid):
            continue
        if mod and mod != m:
            # 模块不匹配时再看命令行(测试用假模块名模拟"别处的服务")
            if mod not in (_cmdline(pid) or ""):
                continue
        out.append(pid)
    return out


def _own_pids(mod: str) -> list:
    """本套实例的服务进程(模块名命中 + QQOPS_DATA 一致), **完全不看端口**。

    手机上没有端口反查能力, 这是唯一可靠的"这是不是我们自己人"判据;
    数据目录必须一致, 否则会把另一套实例(比如真机上跑的测试)当成僵死进程杀掉。
    """
    skip = {os.getpid()} | _ancestor_pids()
    out = [x for x in _pids_from_pidfiles(mod) if x not in skip]
    for p in _run(["pgrep", "-f", "[-]m %s" % mod], 8).split():
        if not p.strip().isdigit():
            continue
        pid = int(p)
        if pid in skip or pid in out:
            continue
        other = (_pid_environ(pid) or {}).get("QQOPS_DATA")
        if not other:
            # 读不到对方环境变量(权限不足 / macOS 没有 /proc)时**不能**认领它 ——
            # 认错人就会去杀别人的进程。宁可报"占用者不是我们", 也不要误杀。
            continue
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
    cands = _own_pids(mod) if mod else []
    if pid:
        # 先把命令行补全: macOS 的 lsof 只给命令名(如 `Python`), 拿它比对模块名必然失败
        full = _cmdline(pid)
        if full:
            cmd = full
    ours = bool(mod and mod in cmd and same_data)
    if pid and pid in cands:
        # 占用者在"我们自己的候选集"里(pid 文件记录 / 环境变量一致)→ 自己人
        ours = True
    if not pid and cands:                       # 手机: 反查不到占用者, 但确实有本套实例在跑
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


def qqops_processes() -> list:
    """列出本机上属于 QQOps 的 python 进程(单进程运行器也算一个进程)。

    用于控制台"资源"页: 让人直观看到"合并前 4 个进程 vs 合并后 1 个"。
    """
    out = []
    if os.path.isdir("/proc"):
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cmd = _cmdline(int(pid))
                if "python" not in cmd or "qqops." not in cmd:
                    continue
                rss, etime = _rss_and_etime(int(pid))
                out.append({"pid": int(pid), "cmd": cmd[:140], "rss": rss, "etime": etime,
                            "self": int(pid) == os.getpid()})
            except Exception:
                continue
    elif os.name == "nt":
        return [{"pid": os.getpid(), "cmd": "python -m qqops.…(Windows 仅供开发)",
                 "rss": self_rss(), "etime": "", "self": True}]
    return sorted(out, key=lambda x: -int(x.get("rss") or 0))


def instance_status(cfg: Config = None) -> dict:
    """整套实例的状态(单进程/多进程都适用)。"""
    cfg = cfg or Config()
    rt = runtime_state()
    alive = bool(rt.get("alive"))
    return {
        "mode": resolve_mode(cfg),
        "running_mode": rt.get("mode") if alive else "",
        "pid": int(rt.get("pid") or 0) if alive else 0,
        "alive": alive,
        "rss": proc_rss(rt.get("pid") or 0) if alive else 0,
        "services": service_status(cfg),
        "power": power_profile(cfg),
        "beats": beats(),
        "restarts": int(state_get("runner_restarts", 0) or 0),
    }


def start_instance(cfg: Config = None, wait: float = 40.0, mode: str = None) -> str:
    """按运行模式启动整套服务: single=一个 runner 进程, multi=四个服务进程。"""
    cfg = cfg or Config()
    ensure_dirs()                           # 日志/run 目录必须先存在(否则起进程时找不到日志文件)
    mode = (mode or resolve_mode(cfg)).lower()
    stop_instance(cfg)                      # 先清掉另一种模式的残留, 避免端口打架
    if mode == "multi":
        msgs = []
        for n in ("collector", "scheduler", "web", "watchdog"):   # 看门狗最后起
            msgs.append(start_service(n))
        return " | ".join(msgs)
    # ---- 单进程 ----
    os.makedirs(RUN_DIR, exist_ok=True)
    logf = os.path.join(LOG_DIR, "runner.log")
    t0 = time.time()
    try:
        with open(logf, "ab") as out:
            p = subprocess.Popen([sys.executable, "-m", "qqops.runner"], cwd=_base_dir(),
                                 stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
    except Exception as e:
        return "实例启动失败(无法创建进程): %s" % e
    runtime_write("single", list(SERVICE_MODULES), p.pid)
    while time.time() - t0 < wait:
        if p.poll() is not None:
            runtime_clear()
            return ("实例启动失败(进程秒退, 退出码 %s)。日志尾部: %s"
                    % (p.returncode, tail(logf, 3).replace("\n", " | ")[-300:]))
        ok_col = _health_single("collector", cfg, {"pid": p.pid})["ok"]
        ok_web = _health_single("web", cfg, {"pid": p.pid})["ok"]
        if ok_col and ok_web:
            return ("单进程实例已启动 (pid %s, 采集口与 控制台 均已应答, 档位 %s)"
                    % (p.pid, power_profile(cfg)["power_mode"]))
        time.sleep(1)
    return ("实例启动异常: %.0f 秒内端口未就绪。日志尾部: %s"
            % (wait, tail(logf, 4).replace("\n", " | ")[-300:]))


def stop_instance(cfg: Config = None) -> str:
    """停掉整套服务(两种模式都处理; 切换模式时必须先走这里)。"""
    cfg = cfg or Config()
    msgs = []
    rt = runtime_state()
    pid = int(rt.get("pid") or 0)
    if rt.get("mode") == "single" and pid:
        if _pid_alive(pid):
            kill_pid(pid, 15)                       # 先礼: SIGTERM / TerminateProcess
            for _ in range(12):
                if not _pid_alive(pid):
                    break
                time.sleep(1)
            if _pid_alive(pid):
                kill_pid(pid, 9)                    # 后兵: SIGKILL
                for _ in range(5):
                    if not _pid_alive(pid):
                        break
                    time.sleep(1)
            if _pid_alive(pid):
                msgs.append("单进程实例停止失败(pid %s 仍在运行)" % pid)
                _record_soft("instance.stop", "stop_failed",
                             "单进程实例 pid %s 未能停止" % pid)
            else:
                msgs.append("单进程实例已停止 (pid %s)" % pid)
        else:
            msgs.append("单进程清单已过期(进程不在)")
    for n in ("watchdog", "collector", "scheduler", "web"):
        try:
            if pgrep("[-]m %s" % SERVICE_MODULES[n]):
                msgs.append(stop_service(n))
        except Exception:
            pass
    # 孤儿 runner: 清单被覆盖/丢失时也要能收拾干净(否则端口被占, 切模式会失败)
    try:
        if pgrep("[-]m qqops.runner"):
            msgs.append("清理残留 runner 进程(%d 个)" % kill_pattern("[-]m qqops.runner"))
            for _ in range(6):
                if not pgrep("[-]m qqops.runner"):
                    break
                time.sleep(1)
            kill_pattern("[-]m qqops.runner", sig=9)
    except Exception:
        pass
    runtime_clear()
    # 等端口真的释放(切模式立刻重启时, 端口没放开就会"启动失败: 地址被占用")
    for port in (int(cfg.data["collector"].get("port") or 8080),
                 int(cfg.data["web"].get("port") or 8091)):
        try:
            _wait_port_free(port, 5)
        except Exception:
            pass
    return "; ".join(msgs) or "没有正在运行的服务"


def service_status(cfg: Config) -> dict:
    web_port = int(cfg.data["web"]["port"])
    col_port = int(cfg.data["collector"]["port"])
    napcat_port = int(cfg.data["push"].get("napcat_api_port", 3000))

    def one(name: str, pattern: str, port: int) -> dict:
        info = proc_info(pattern)
        return {"running": bool(info["pid"]), "port": port, "pid": info["pid"],
                "rss": info["rss"], "etime": info["etime"], "mode": "multi"}

    rt = runtime_state()
    napcat = one("napcat", "[o]pt/QQ/qq", napcat_port)
    if rt.get("mode") != "single":
        return {"collector": one("collector", "[-]m qqops.collector", col_port),
                "scheduler": one("scheduler", "[-]m qqops.scheduler", 0),
                "web": one("web", "[-]m qqops.webapp", web_port),
                "watchdog": one("watchdog", "[-]m qqops.watchdog", 0),
                "napcat": napcat}
    # 单进程: 四个服务共享一个 PID, 用各自的心跳/端口区分状态
    pid = int(rt.get("pid") or 0)
    alive = bool(pid) and _pid_alive(pid)
    rss, etime = _rss_and_etime(pid) if alive else (0, "")
    beats_ts = beats()
    ports = {"collector": col_port, "web": web_port, "scheduler": 0, "watchdog": 0}
    out = {}
    for n in ("collector", "scheduler", "web", "watchdog"):
        fresh = beats_ts.get(n, 0)
        ok = alive and (fresh == 0 or now() - fresh < 300)
        out[n] = {"running": bool(ok), "port": ports[n], "pid": pid if alive else 0,
                  "rss": rss, "etime": etime, "mode": "single",
                  "beat": fresh, "thread": ok}
    out["napcat"] = napcat
    return out


def status_snapshot(cfg: Config | None = None) -> dict:
    """给控制台用的完整状态快照(服务/端口/采集/调度/存储/生态)。"""
    cfg = cfg or Config()
    store = Store()
    stats = store.stats(24)
    sch = cfg.data.get("schedule", {})
    nxt = next_run(sch)
    last_ts = int(state_get("last_summary_ts", 0) or 0)
    new_msgs = store.count_since(last_ts) if last_ts else stats["window"]
    rt = runtime_state()
    prof = power_profile(cfg)
    return {
        "ts": now(),
        "version": VERSION,
        "runtime": rt,
        "power": prof,
        "services": service_status(cfg),
        "extras": extra_status(cfg),
        "ecosystem": ecosystem_ports(cfg),
        "counters": state_get("collector_counters", {}) or {},
        "fts": {"available": store.fts_ok, "rows": store.fts_count()},
        "scheduler": {"alive": now() - last_beat("scheduler") < 60,
                      "beat": last_beat("scheduler"),
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
    """写一个键到共享 state.json。

    这是个**读改写**操作, 多进程并发会互相覆盖(踩过: 心跳/计数忽然回退), 所以先拿锁;
    拿不到锁也不要卡住服务 —— 超时后照写, 顶多丢这一次更新。
    """
    with _file_lock(os.path.join(RUN_DIR, ".state.lock"), timeout=3.0):
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
