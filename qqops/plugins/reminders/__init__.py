"""截止提醒插件 —— 也是框架的第一块"试金石"。

它同时用到了插件 API 的全部能力:
    @on_stage  在管线上插一段(采集进程里扫描消息)
    @task      定时任务(调度进程里到点提醒)
    @route     控制台接口
    @command   具名命令
    @page      控制台页面

多进程写入安全: 采集进程只往 **收件箱**(append-only jsonl)追加候选,
只有调度进程的 tick 才是 state.json 的**唯一写者**, 因此不会互相覆盖。

识别规则(刻意保守, 宁可漏也不要吵):
    * 消息里出现配置的关键词;
    * 且能解析出一个未来的时间点(复用 core.parse_due: 明天/下周三/3月5日/月底…);
    * 时间点在 max_days_ahead 天以内。
"""
from __future__ import annotations

import json
import os
import re
import time

from qqops.framework.api import Plugin, command, on_stage, page, register, route, task

CQ = re.compile(r"\[CQ:[^\]]*\]")
URL = re.compile(r"https?://\S+")
CLOCK = re.compile(r"(上午|中午|下午|晚上|凌晨)?\s*(\d{1,2})\s*[:：点时]\s*(\d{1,2})?\s*分?")
BARE_HOUR = re.compile(r"(上午|中午|下午|晚上|凌晨)\s*(\d{1,2})\s*点")


@register(id="reminders", name="截止提醒", version="1.0.0", author="qqops",
          desc="从群消息里识别带时间点的待办, 到点提醒")
class RemindersPlugin(Plugin):

    # ------------------------------------------------------------------ 生命周期
    def initialize(self, ctx):
        self.inbox_path = os.path.join(self.data_dir, "inbox.jsonl")
        self.state_file_path = self.state_file()
        self._log_once = {}
        self.info("提醒插件就绪(数据目录 %s)" % self.data_dir)

    # ------------------------------------------------------------------ 工具
    def _load(self) -> dict:
        st = self.load_state()
        if not isinstance(st.get("items"), list):
            st["items"] = []
        st.setdefault("seq", 0)
        return st

    def _save(self, st: dict) -> None:
        cfg = (self.cfg.get("maintain") or {})
        max_items = int(cfg.get("max_items") or 200)
        items = st.get("items") or []
        # 先丢过期的已完成项
        keep_days = int(cfg.get("keep_days_after_done") or 14)
        cut = time.time() - keep_days * 86400
        items = [i for i in items if not (i.get("status") == "done" and float(i.get("done_ts") or 0) < cut)]
        if len(items) > max_items:
            items.sort(key=lambda i: (i.get("status") == "done", float(i.get("due") or 0)))
            items = items[-max_items:]
        st["items"] = items
        self.save_state(st)

    def _append_inbox(self, item: dict) -> None:
        """采集进程只追加, 不碰 state.json(多进程安全)。"""
        try:
            os.makedirs(os.path.dirname(self.inbox_path), exist_ok=True)
            with open(self.inbox_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception as e:                       # noqa: BLE001
            self.warn("收件箱写入失败: %s" % e)

    def _drain_inbox(self) -> int:
        """调度进程合并收件箱(唯一写者)。"""
        if not os.path.isfile(self.inbox_path):
            return 0
        rows = []
        try:
            with open(self.inbox_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            continue
            os.remove(self.inbox_path)
        except Exception as e:                       # noqa: BLE001
            self.warn("收件箱读取失败: %s" % e)
            return 0
        if not rows:
            return 0
        st = self._load()
        seen = {(i.get("session"), i.get("title"), int(i.get("due") or 0)) for i in st["items"]}
        added = 0
        for r in rows:
            key = (r.get("session"), r.get("title"), int(r.get("due") or 0))
            if key in seen:
                continue
            seen.add(key)
            st["seq"] = int(st.get("seq") or 0) + 1
            r["id"] = "r%05d" % st["seq"]
            r["status"] = "open"
            st["items"].append(r)
            added += 1
        if added:
            self._save(st)
            self.info("新识别到 %d 条待办" % added)
        return added

    def _clean_text(self, raw: str) -> str:
        text = CQ.sub(" ", str(raw or ""))
        text = URL.sub(" ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_when(text: str, now: float = None) -> tuple:
        """解析出时间点(秒级)。返回 `(ts, 是否含具体时刻)`。"""
        from qqops import core
        now = now or time.time()
        ts = core.parse_due(text, int(now))
        if not ts:
            return 0, False
        hour = minute = None
        m = BARE_HOUR.search(text) or CLOCK.search(text)
        if m:
            groups = m.groups()
            if len(groups) == 3:
                period, hh, mm = groups
            else:
                period, hh, mm = (groups[0], groups[1], None)
            try:
                hour = int(hh)
                minute = int(mm) if mm else 0
                if period in ("下午", "晚上") and hour < 12:
                    hour += 12
                elif period == "中午" and hour < 11:
                    hour += 12
                elif period == "凌晨" and hour == 12:
                    hour = 0
                if not (0 <= hour <= 23):
                    hour = None
            except Exception:
                hour = None
        if hour is None:
            return ts, False
        lt = time.localtime(ts)
        try:
            new = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute or 0, 0, 0, 0, -1)))
        except Exception:
            return ts, False
        if new < now - 60:                 # 解析出的时刻已过, 顺延一天
            new += 86400
        return new, True

    def _try_extract(self, evt) -> dict:
        """从一条群/私聊消息里尝试提取待办; 返回 item 或 {}。"""
        cfg = self.cfg.get("basic") or {}
        if not cfg.get("enabled", True):
            return {}
        one = evt.payload or {}
        raw = one.get("raw_message") or ""
        text = self._clean_text(raw)
        if len(text) < int(cfg.get("min_text_len") or 4):
            return {}
        keys = cfg.get("keywords") or []
        low = text.lower()
        if keys and not any(str(k).lower() in low for k in keys):
            return {}
        ts, exact = self._parse_when(text)
        if not ts:
            return {}
        now = time.time()
        if ts < now - 60:
            return {}
        max_days = float(cfg.get("max_days_ahead") or 60)
        if ts > now + max_days * 86400:
            return {}
        sender = (one.get("sender") or {}).get("nickname") or one.get("user_id") or ""
        title = text if len(text) <= 60 else text[:57] + "…"
        return {"title": title, "due": int(ts), "exact": bool(exact),
                "session": evt.session, "group": one.get("group_id") or 0,
                "user": one.get("user_id") or 0, "sender": str(sender)[:40],
                "created": int(now), "notified": False, "status": "open"}

    # ------------------------------------------------------------------ 管线阶段(采集进程)
    @on_stage("reminders_scan", order=35)
    def scan(self, event, ctx):
        if event.kind != "message.received":
            return None
        item = self._try_extract(event)
        if not item:
            return None
        self._append_inbox(item)
        return {"reminder": item["title"][:30], "due": item["due"]}

    # ------------------------------------------------------------------ 定时任务(调度进程)
    @task("reminders_tick", every=60, run_on_start=True)
    def tick(self, ctx):
        added = self._drain_inbox()
        st = self._load()
        cfg = self.cfg.get("notify") or {}
        lead = float(cfg.get("lead_minutes") or 0) * 60
        now = time.time()
        sent, due_list = 0, []
        for item in st["items"]:
            if item.get("status") != "open" or item.get("notified"):
                continue
            due = float(item.get("due") or 0)
            if now < due - lead:
                continue
            if self._quiet(cfg, now) and now < due:
                continue                      # 免打扰期间且还没到点 → 等
            title = item.get("title") or ""
            when = time.strftime("%m-%d %H:%M", time.localtime(due))
            late = now > due + 300
            text = "⏰ %s提醒: %s(%s)" % ("已过期" if late else "", title, when)
            target = str(cfg.get("target_session") or item.get("session") or "")
            pushed = {"ok": False, "skipped": "未开启推送"}
            if cfg.get("push"):
                pushed = ctx.notify(text, target)
            self.info("提醒: %s → %s" % (text, pushed.get("ok") and "已推送" or "仅记录"))
            item["notified"] = True
            item["notified_ts"] = int(now)
            item["push_result"] = "ok" if pushed.get("ok") else str(pushed.get("error") or pushed.get("skipped") or "?")
            item["status"] = "notified" if not late else "late"
            sent += 1
            due_list.append({"id": item.get("id"), "title": title, "due": int(due),
                             "late": late, "push": item["push_result"]})
            try:
                ctx.emit("plan.reminder", {"id": item.get("id"), "title": title,
                                           "due": int(due), "late": late},
                         session=str(item.get("session") or ""), source="reminders")
            except Exception:
                pass
        if sent or added:
            self._save(st)
        return {"added": added, "notified": sent, "due": due_list}

    @staticmethod
    def _quiet(cfg: dict, now: float) -> bool:
        try:
            h = time.localtime(now).tm_hour
            a, b = int(cfg.get("quiet_start", 23)), int(cfg.get("quiet_end", 7))
        except Exception:
            return False
        if a == b:
            return False
        return (a <= h or h < b) if a > b else (a <= h < b)

    # ------------------------------------------------------------------ 命令
    @command("reminders.due", help="列出即将到期的提醒")
    def cmd_due(self, args=None, ctx=None):
        st = self._load()
        now = time.time()
        rows = [i for i in st["items"] if i.get("status") in ("open", "notified", "late")]
        rows.sort(key=lambda i: float(i.get("due") or 0))
        return {"count": len(rows), "items": rows[:20]}

    @command("reminders.scan", help="补扫最近若干小时的消息, 抽取漏掉的待办")
    def cmd_scan(self, args=None, ctx=None):
        ctx = ctx or self.ctx
        hours = int((args or {}).get("hours") or 24)
        try:
            res = ctx.store.messages(hours=hours, page=1, size=3000)
            rows = res.get("rows") or []
        except Exception as e:                       # noqa: BLE001
            return {"ok": False, "error": "读取消息失败: %s" % e}
        found = 0
        for m in rows:
            one = {"raw_message": m.get("raw_message") or "",
                   "group_id": m.get("group_id"), "user_id": m.get("user_id"),
                   "sender": {"nickname": m.get("nickname") or ""}}

            class _E:                                # 轻量事件壳, 复用同一套提取逻辑
                kind = "message.received"
                payload = one
                session = "group:%s" % (m.get("group_id") or "")
            item = self._try_extract(_E())
            if item:
                self._append_inbox(item)
                found += 1
        return {"scanned": len(rows), "found": found, "hours": hours}

    # ------------------------------------------------------------------ 控制台接口
    @route("/api/plugins/reminders/list")
    def api_list(self, req, ctx):
        st = self._load()
        now = time.time()
        items = []
        for i in st["items"]:
            row = dict(i)
            row["due_text"] = time.strftime("%m-%d %H:%M", time.localtime(float(i.get("due") or 0)))
            row["left_seconds"] = int(float(i.get("due") or 0) - now)
            items.append(row)
        items.sort(key=lambda r: (r.get("status") == "done", r.get("due") or 0))
        open_cnt = len([i for i in st["items"] if i.get("status") in ("open", "notified")])
        return {"items": items[:200], "open": open_cnt, "total": len(st["items"]),
                "inbox_pending": os.path.isfile(self.inbox_path)}

    @route("/api/plugins/reminders/action", methods=("POST",))
    def api_action(self, req, ctx):
        data = req.json() if hasattr(req, "json") else {}
        rid = str((data or {}).get("id") or "")
        act = str((data or {}).get("action") or "done")
        st = self._load()
        hit = None
        for i in st["items"]:
            if i.get("id") == rid:
                hit = i
                break
        if hit is None:
            return {"ok": False, "error": "没有这条提醒: %s" % rid}
        if act == "done":
            hit["status"] = "done"
            hit["done_ts"] = int(time.time())
        elif act == "reopen":
            hit["status"] = "open"
            hit["notified"] = False
        elif act == "delete":
            st["items"] = [i for i in st["items"] if i.get("id") != rid]
        else:
            return {"ok": False, "error": "未知动作: %s" % act}
        self._save(st)
        return {"ok": True, "id": rid, "action": act}

    @route("/api/plugins/reminders/scan", methods=("POST",))
    def api_scan(self, req, ctx):
        return self.cmd_scan({"hours": (req.json() or {}).get("hours") or 24}, ctx)

    @page({"id": "reminders", "title": "提醒", "icon": "⏰", "order": 60,
           "api": "/api/plugins/reminders/list",
           "desc": "从群消息识别出的待办与到点提醒"})
    def page_meta(self, req=None, ctx=None):
        return {"id": "reminders"}
