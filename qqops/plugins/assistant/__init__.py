"""智能查询插件 —— 用自然语言问"我的记录里到底有什么"。

设计要点(为什么这么做):
  1. **先检索, 再让模型说话**: 模型只看得到程序从本地库里取出的记录, 不会被问到"你不知道的事";
  2. **强制标注来源**: 每条结论带 [M3]/[E1] 这类编号, 控制台可点开看原文, 便于核对真假;
  3. **只依据记录作答**: 记录里没有就直说"记录里没有", 不允许编造;
  4. **两段式检索**: 先按问题里的关键词做全文检索(AND), 命中不足时按词逐个检索再合并;
     模型觉得不够还可以要求再检索一轮(最多 max_rounds);
  5. **失败降级**: 模型不可用时, 直接把它检索到的记录列出来 —— 空白页是最糟的体验;
  6. **会话落库**: 问答记录存 SQLite(chat_sessions/chat_turns), 支持历史回看、成本核算、每日额度。
"""
from __future__ import annotations

import json
import re
import time

from qqops.framework.api import Plugin, command, page, register, route
from qqops.framework import llm as llm_mod

# 提问里的时间线索 → 小时数
TIME_HINTS = [
    (("刚刚", "刚才", "刚才", "这阵子", "这会儿"), 1),
    (("今天", "今日", "今早", "今晚"), 24),
    (("昨天", "昨日", "昨晚"), 48),
    (("前天",), 72),
    (("这两天", "这几天", "最近几天"), 72),
    (("这周", "本周", "这一周"), 168),
    (("上周", "上星期", "上礼拜"), 336),
    (("这个月", "本月"), 720),
    (("上个月", "上月"), 1440),
]
# 与检索无关的提问用词(避免把它们当关键词塞进全文检索)
TIME_WORDS = ("刚刚", "刚才", "今天", "今日", "今早", "今晚", "昨天", "昨日", "昨晚",
              "前天", "后天", "明天", "这两天", "这几天", "最近", "这周", "本周", "上周",
              "上星期", "上礼拜", "这个月", "本月", "上个月", "上月", "近来", "目前")
STOPWORDS = set("""的了吗呢吧啊呀哦嗯是 有 没有 什么 怎么 怎样 如何 哪些 哪个 哪里 谁 为什么
有没有 是不是 能否 可以 请 帮我 查 查询 找 找一下 看看 说一下 告诉我 关于 相关 以及 和 与 跟
还有 然后 现在 目前 最近 之前 以后 时间 时候 情况 内容 消息 记录 群里 群 里 说 说了 聊 聊天
多少 几个 哪几个 一下 那些 这些 这个 那个 他 她 它 他们 我们 你们 我 你 大家 有人 事情
""".split()) | set(TIME_WORDS)
_KEEP = re.compile(r"[\u4e00-\u9fffA-Za-z0-9_+#@.\-]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_+#@.\-]{2,}")


def terms_of(question: str, limit: int = 10) -> list:
    """把中文提问切成可用于全文检索的词。

    中文没有空格, 直接整句拿去 FTS 会"一个词都搜不到", 所以:
      1. 先剪掉"昨天/谁/什么/里/了"这类虚词;
      2. 剩余中文按 2 字滑窗切成候选词(不需要分词库, 召回够用);
      3. 拉丁词/数字原样保留。
    """
    q = str(question or "")
    out = []
    for run in _CJK.findall(q):
        seg = run
        for kw in sorted(STOPWORDS, key=len, reverse=True):
            if kw:
                seg = seg.replace(kw, " ")
        for part in seg.split():
            if len(part) <= 3:
                out.append(part)
            else:
                out.extend(part[i:i + 2] for i in range(0, len(part) - 1))
    out.extend(_LATIN.findall(q))
    seen, res = set(), []
    for t in out:
        t = t.strip()
        if len(t) >= 2 and t not in seen and t.lower() not in STOPWORDS:
            seen.add(t)
            res.append(t)
    return res[:limit]

SYSTEM = """你是 QQOps 的记录查询助手, 只根据下面给出的"记录"回答用户的问题。

必须遵守:
1. 只使用记录里出现的信息, 不得编造、不得用常识补全; 记录里没有就问用户要更多线索;
2. {cite}每条结论后面必须标注来源编号, 例如 [M3]、[E1], 编号必须真实存在于记录中;
3. 时间、人名、群名、数字必须与记录一致, 不要改写成"今天/昨天"这类相对说法;
4. 若记录不足以回答, 直接说明"记录里没有相关内容", 并指出还缺什么(例如时间范围/群);
5. 用简体中文, 简洁; 需要列举时用短句或列表, 不要长篇大论;
6. 如果确实需要再检索一次, **只输出一行 JSON** 且不要有其他文字:
   {{"need": {{"q": "要检索的关键词", "hours": 168}}}}{extra}"""


@register(id="assistant", name="智能查询", version="1.0.0", author="qqops",
          desc="用自然语言查询聊天记录, 带来源标注")
class AssistantPlugin(Plugin):

    # ------------------------------------------------------------------ 生命周期
    def initialize(self, ctx):
        self._llm = None
        self.info("智能查询就绪(默认范围 %s 小时)" % self._cfg_basic().get("default_hours", 72))

    @property
    def llm(self):
        if self._llm is None:
            self._llm = llm_mod.from_config(self.ctx.cfg, component="assistant")
        return self._llm

    def _cfg_basic(self) -> dict:
        return (self.cfg or {}).get("basic") or {}

    def _cfg(self, sec: str) -> dict:
        return (self.cfg or {}).get(sec) or {}

    # ------------------------------------------------------------------ 问题解析
    def _scope(self, question: str) -> dict:
        """从问题里解析时间范围与群。返回 {hours, group, group_name, tokens}"""
        basic = self._cfg_basic()
        hours = int(basic.get("default_hours") or 72)
        for keys, h in TIME_HINTS:
            if any(k in question for k in keys):
                hours = h
                break
        m = re.search(r"(?:最近|近|过去)\s*(\d{1,3})\s*(天|周|小时|个月)", question)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            hours = n * {"小时": 1, "天": 24, "周": 168, "个月": 720}[unit]
        group = 0
        gname = ""
        try:
            rows = self.ctx.store.group_rows()
        except Exception:
            rows = {}
        for gid, row in (rows or {}).items():
            name = (row or {}).get("name") or ""
            if name and len(name) >= 2 and name in question:
                group, gname = int(gid), name
                break
        if not group:
            m = re.search(r"(?<!\d)(\d{6,12})(?!\d)", question)
            if m and int(m.group(1)) in {int(k) for k in (rows or {})}:
                group, gname = int(m.group(1)), str(((rows or {}).get(int(m.group(1))) or {}).get("name") or "")
        tokens = terms_of(question, 8)
        return {"hours": hours, "group": group, "group_name": gname, "tokens": tokens}

    # ------------------------------------------------------------------ 检索
    def _search_messages(self, tokens: list, scope: dict, limit: int) -> tuple:
        """先整串 AND 检索, 命中太少再逐词检索合并。返回 (行, 模式说明)。"""
        store = self.ctx.store
        kwargs = {"hours": scope["hours"], "size": limit, "order": "desc"}
        if scope.get("group"):
            kwargs["group"] = str(scope["group"])
        if tokens:
            d = store.messages(q=" ".join(tokens), **kwargs)
            rows = d.get("rows") or []
            if len(rows) >= 3:
                return rows, "关键词(%s)" % "+".join(tokens[:3])
            merged = {r["id"]: dict(r, _hit=1) for r in rows}
            for t in tokens:
                for r in (store.messages(q=t, **kwargs).get("rows") or []):
                    if r["id"] in merged:
                        merged[r["id"]]["_hit"] = merged[r["id"]].get("_hit", 1) + 1
                    else:
                        merged[r["id"]] = dict(r, _hit=1)
            if merged:
                out = sorted(merged.values(), key=lambda r: (-r.get("_hit", 0), -r["ts"]))
                return out[:limit], "分关键词检索(%s)" % "/".join(tokens[:3])
        return (store.messages(**{k: v for k, v in kwargs.items() if k != "q"}).get("rows") or [],
                "范围内最近消息")

    def _retrieve(self, question: str, scope: dict = None, limit: int = None,
                  extra: dict = None) -> dict:
        """把"问题的线索"变成一组带编号的记录。"""
        scope = scope or self._scope(question)
        limit = int(limit or self._cfg_basic().get("max_sources") or 60)
        tokens = list(scope.get("tokens") or [])
        if extra:
            if extra.get("q"):
                tokens = terms_of(str(extra["q"]), 8) or tokens
            if extra.get("hours"):
                try:
                    scope = {**scope, "hours": max(1, int(extra["hours"]))}
                except Exception:
                    pass
        rows, mode = self._search_messages(tokens, scope, limit)
        ctx = self.ctx
        bundle = {"mode": mode, "scope": scope, "messages": rows[:limit],
                  "events": [], "memories": [], "links": [], "media": [], "stats": {}}
        # 相关结构化记录(整体范围, 与关键词无关的东西不该挤掉消息)
        try:
            ev = ctx.store.events(status="all", limit=12)
            bundle["events"] = [e for e in ev
                                if not e.get("due_at") or e["due_at"] > time.time() - 7 * 86400][:6]
        except Exception as e:                       # noqa: BLE001
            ctx.record("assistant.events", exc=e)
        try:
            bundle["memories"] = ctx.store.memory_all()[:5]
        except Exception as e:                       # noqa: BLE001
            ctx.record("assistant.memory", exc=e)
        try:
            bundle["links"] = ctx.store.links(limit=5)
        except Exception as e:                       # noqa: BLE001
            ctx.record("assistant.links", exc=e)
        try:
            bundle["media"] = [m for m in ctx.store.media_list(24) if (m.get("caption") or "").strip()][:5]
        except Exception as e:                       # noqa: BLE001
            ctx.record("assistant.media", exc=e)
        try:
            bundle["stats"] = {"count": len(rows),
                               "hours": scope["hours"],
                               "groups": len({(r.get("group_id") or 0) for r in rows})}
        except Exception:
            pass
        return bundle

    # ------------------------------------------------------------------ 组装上下文
    def _build(self, question: str, bundle: dict):
        """把记录排成编号文本 + 来源表(供前端点开原文)。"""
        lines, sources = [], []

        def add(kind, label, text, **meta):
            ref = "%s%d" % (kind, len([s for s in sources if s["kind"] == kind]) + 1)
            sources.append({"ref": ref, "kind": kind, "label": label,
                            "text": text[:400], **meta})
            return ref

        for r in bundle.get("messages") or []:
            who = r.get("nickname") or r.get("user_id") or "?"
            gname = r.get("group_name") or r.get("group_id") or ""
            when = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("ts") or 0)))
            text = (r.get("raw_message") or "").strip()
            if len(text) > 300:
                text = text[:300] + "…"
            if not text:
                text = "(非文本消息: %s)" % (r.get("kinds") or "")
            mark = []
            if r.get("recalled"):
                mark.append("已撤回")
            if r.get("self"):
                mark.append("机器人自己发的")
            ref = add("M", "%s %s" % (when, who), text, id=r.get("id"), ts=r.get("ts"),
                      group_id=r.get("group_id"), group_name=r.get("group_name"))
            lines.append("[%s] %s %s/%s: %s%s" % (ref, when, gname, who, text,
                                                  ("( %s )" % "、".join(mark)) if mark else ""))
        for e in bundle.get("events") or []:
            due = time.strftime("%m-%d", time.localtime(int(e.get("due_at") or 0))) if e.get("due_at") else "无期限"
            title = e.get("title") or ""
            ref = add("E", "待办/事件 %s" % title[:24], title, id=e.get("id"), due=e.get("due_at"))
            lines.append("[%s] %s事件(%s, %s): %s" % (ref, e.get("kind") or "", e.get("status") or "",
                                                      due, title))
        for mm in bundle.get("memories") or []:
            content = (mm.get("content") or "").strip()
            if not content:
                continue
            ref = add("Y", "记忆 %s" % (mm.get("scope") or ""), content)
            lines.append("[%s] 长期记忆(%s): %s" % (ref, mm.get("scope") or "", content[:300]))
        for l in bundle.get("links") or []:
            title = l.get("title") or l.get("url") or ""
            ref = add("L", "链接 %s" % title[:24], "%s %s" % (title, l.get("excerpt") or ""),
                      url=l.get("url"))
            lines.append("[%s] 链接: %s %s" % (ref, title, l.get("url") or ""))
        for md in bundle.get("media") or []:
            cap = (md.get("caption") or "").strip()
            ref = add("P", "图片/文件 %s" % (md.get("name") or md.get("kind") or ""), cap,
                      key=md.get("key"))
            lines.append("[%s] 媒体说明(%s): %s" % (ref, md.get("kind") or "", cap[:200]))
        return lines, sources

    def _prompt(self, question: str, lines: list, scope: dict) -> tuple:
        p = self._cfg("prompt")
        cite = "每条结论都要标注来源编号; " if p.get("cite_required", True) else ""
        extra = ("\n7. 附加要求: " + str(p["extra_instructions"]).strip()
                 if str(p.get("extra_instructions") or "").strip() else "")
        system = SYSTEM.format(cite=cite, extra=extra)
        rng = "最近 %s 小时" % scope.get("hours")
        if scope.get("group_name"):
            rng += ", 群: %s" % scope["group_name"]
        body = "问题: %s\n\n记录(检索方式: %s; 范围: %s; 共 %d 行):\n%s" % (
            question, scope.get("mode") or "", rng, len(lines), "\n".join(lines) or "(没有检索到任何记录)")
        return system, body

    # ------------------------------------------------------------------ 问答
    def ask(self, question: str, session_id: str = "", hours: int = 0, group: int = 0,
            use_llm: bool = True) -> dict:
        t_start = time.time()
        question = str(question or "").strip()
        if not question:
            return {"ok": False, "error": "问题不能为空"}
        cfg = self._cfg_basic()
        if not cfg.get("enabled", True):
            return {"ok": False, "error": "智能查询已在插件配置里关闭"}
        stamp = time.strftime("%Y-%m-%d", time.localtime())
        used = self._today_used(stamp)
        quota = int(cfg.get("daily_quota") or 100)
        if used >= quota:
            return {"ok": False, "error": "今天的提问额度已用完(%d/%d), 可在插件配置里调整" % (used, quota)}
        scope = self._scope(question)
        if hours:
            scope["hours"] = int(hours)
        if group:
            scope["group"] = int(group)
            try:
                scope["group_name"] = (self.ctx.store.group_rows().get(int(group)) or {}).get("name") or ""
            except Exception:
                scope["group_name"] = ""
        sid = session_id or self.ctx.store.chat_session_create(question[:40])
        if not session_id:
            self.ctx.store.chat_session_touch(sid, title=question[:40])
        self.ctx.store.chat_append(sid, "user", question)

        bundle = self._retrieve(question, scope)
        lines, sources = self._build(question, bundle)
        scope["mode"] = bundle.get("mode")
        out = {"ok": True, "session_id": sid, "question": question, "sources": sources,
               "scope": scope, "mode": bundle.get("mode"), "count": len(sources),
               "answer": "", "degraded": False, "rounds": 0}
        if not sources:
            out["answer"] = "记录里没有找到与这个问题相关的内容。可以换个时间范围、群, 或者换几个关键词再问一次。"
            out["degraded"] = True
            self.ctx.store.chat_append(sid, "assistant", out["answer"], sources=[],
                                       meta={"degraded": True, "mode": out["mode"]})
            return out
        if not use_llm:
            out["answer"] = "（仅检索, 未调用模型）"
            out["degraded"] = True
            return out

        system, body = self._prompt(question, lines, scope)
        max_rounds = max(1, int(cfg.get("max_rounds") or 2))
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        cost, rounds, answer = 0.0, 0, ""
        for i in range(max_rounds):
            rounds += 1
            res = self.llm.chat([{"role": "system", "content": system},
                                 {"role": "user", "content": body}],
                                temperature=float(cfg.get("temperature") or 0.2),
                                max_tokens=int(cfg.get("max_tokens") or 1500))
            cost += res.cost
            usage["prompt_tokens"] += res.tokens_in
            usage["completion_tokens"] += res.tokens_out
            if not res.ok:
                out["degraded"] = True
                out["error"] = res.error
                out["answer"] = ("模型暂时不可用(%s)。下面是与你问题最相关的记录, 你可以直接看:\n"
                                 % (res.error or "")[:60]) + "\n".join("- " + s["label"] for s in sources[:10])
                break
            answer = res.text.strip()
            need = self._parse_need(answer)
            if need and i + 1 < max_rounds:
                self.info("模型要求补充检索: %s" % need)
                more = self._retrieve(question, scope, extra=need)
                lines2, sources2 = self._build(question, more)
                grew = len(sources2) > len(sources)
                if grew:
                    lines, sources = lines2, sources2
                    bundle["mode"] = more.get("mode")
                    out["mode"] = more.get("mode")
                # 无论如何都要再问一次: 否则那行 {"need": ...} JSON 会被当成回答发给用户
                system, body = self._prompt(question, lines, scope)
                if not grew:
                    body += ("\n\n(补充检索没有再找到新记录, 请直接基于以上记录作答, "
                             "并说明哪部分记录不足。)")
                continue
            break
        # 降级文案不能被空回答覆盖(模型不可用时 answer 是空串)
        out["answer"] = answer or out.get("answer") or ""
        out["rounds"] = rounds
        out["usage"] = usage
        out["cost"] = round(cost, 6)
        out["sources"] = sources
        out["count"] = len(sources)
        self.ctx.store.chat_append(sid, "assistant", out.get("answer") or "", sources=sources,
                                   meta={"degraded": out["degraded"], "mode": out["mode"],
                                         "rounds": rounds, "model": self.llm.model},
                                   tokens_in=usage["prompt_tokens"],
                                   tokens_out=usage["completion_tokens"], cost=cost,
                                   ms=int((time.time() - t_start) * 1000))
        self.info("提问: %s → %d 字, %d 条来源, %s rounds, cost %.4f"
                  % (question[:30], len(answer), len(sources), rounds, cost))
        out["ms"] = int((time.time() - t_start) * 1000)
        return out

    @staticmethod
    def _parse_need(text: str) -> dict:
        """模型若要求补充检索, 会输出一行 JSON。宽容解析(带 ``` 或前后有字都认)。"""
        if "{" not in text or len(text) > 400:
            return {}
        m = re.search(r"\{[^{}]*\"need\"[^{}]*\{[^{}]*\}[^{}]*\}", text, re.S)
        if not m:
            m = re.search(r"\{[^{}]*\"need\"[^{}]*\}", text, re.S)
        if not m:
            return {}
        try:
            obj = json.loads(m.group(0))
            need = obj.get("need")
            return need if isinstance(need, dict) else {}
        except Exception:
            return {}

    def _today_used(self, stamp: str) -> int:
        """今天已经问了几次(按会话轮次统计, 用于每日额度)。"""
        try:
            con = self.ctx.store.conn()
            try:
                day0 = int(time.mktime(time.strptime(stamp, "%Y-%m-%d")))
                return int(con.execute(
                    "SELECT COUNT(*) c FROM chat_turns WHERE role='user' AND created>=?",
                    (day0,)).fetchone()["c"])
            finally:
                con.close()
        except Exception:
            return 0

    # ------------------------------------------------------------------ 控制台接口
    @route("/api/assistant/ask", methods=("POST",))
    def api_ask(self, req, ctx):
        d = req.json() or {}
        return self.ask(str(d.get("question") or d.get("q") or ""),
                        session_id=str(d.get("session_id") or ""),
                        hours=int(d.get("hours") or 0), group=int(d.get("group") or 0),
                        use_llm=bool(d.get("use_llm", True)))

    @route("/api/assistant/sessions")
    def api_sessions(self, req, ctx):
        rows = self.ctx.store.chat_sessions(int(req.q("limit") or 30))
        for r in rows:
            r["updated_text"] = time.strftime("%m-%d %H:%M", time.localtime(int(r.get("updated") or 0)))
        return {"sessions": rows, "today_used": self._today_used(time.strftime("%Y-%m-%d")),
                "quota": int(self._cfg_basic().get("daily_quota") or 100),
                "llm": self.llm.describe()}

    @route("/api/assistant/session")
    def api_session(self, req, ctx):
        sid = req.q("id")
        d = self.ctx.store.chat_session(sid)
        if not d:
            return {"ok": False, "error": "会话不存在"}
        for t in d.get("turns") or []:
            t["time_text"] = time.strftime("%m-%d %H:%M", time.localtime(int(t.get("created") or 0)))
        return d

    @route("/api/assistant/delete", methods=("POST",))
    def api_delete(self, req, ctx):
        sid = str((req.json() or {}).get("id") or "")
        return {"ok": True, "deleted_turns": self.ctx.store.chat_session_delete(sid)}

    @route("/api/assistant/suggest")
    def api_suggest(self, req, ctx):
        """推荐问题: 从最近的实际记录里生成, 不用模型(省额度)。"""
        n = max(0, int(self._cfg("display").get("suggest_count") or 4))
        out = []
        try:
            store = self.ctx.store
            ev = store.events(status="open", limit=3, soon_only=True)
            for e in ev[:2]:
                out.append("「%s」这件事有结论了吗?" % (e.get("title") or "")[:18])
            links = store.links(limit=2)
            if links:
                out.append("最近群里分享的链接都讲了什么?")
            st = store.stats(24)
            if st.get("top_groups"):
                g = st["top_groups"][0]
                out.append("「%s」昨天主要聊了什么?" % ((g.get("name") or g.get("group_id")) or "")[:14])
            media = [m for m in store.media_list(10) if (m.get("caption") or "").strip()]
            if media:
                out.append("最近群里的图片都是什么内容?")
            mem = store.memory_all()[:1]
            if mem:
                out.append("我之前让你记住的约定是什么?")
        except Exception as e:                       # noqa: BLE001
            ctx.record("assistant.suggest", exc=e)
        return {"suggestions": out[:n or 4]}

    @route("/api/assistant/source/<kind>/<ref>")
    def api_source(self, req, ctx):
        """点开某条来源看原文(消息按 id、事件按 id、链接按 url、媒体按 key)。"""
        kind, ref = (req.params or {}).get("kind"), (req.params or {}).get("ref")
        store = self.ctx.store
        if kind == "M":
            try:
                con = store.conn()
                try:
                    row = con.execute(
                        """SELECT m.*, COALESCE(g.name,'') group_name FROM messages m
                           LEFT JOIN groups g ON g.id=m.group_id WHERE m.id=?""",
                        (int(ref),)).fetchone()
                finally:
                    con.close()
                if not row:
                    return {"ok": False, "error": "消息不存在"}
                d = dict(row)
                d["ts_text"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(d.get("ts") or 0)))
                try:
                    d["segments"] = json.loads(d.get("segments") or "[]")
                except Exception:
                    pass
                return {"ok": True, "kind": "message", "record": d}
            except Exception as e:                   # noqa: BLE001
                return {"ok": False, "error": str(e)}
        if kind == "E":
            try:
                rows = [e for e in store.events(status="all", limit=500) if int(e.get("id") or 0) == int(ref)]
                return {"ok": bool(rows), "kind": "event", "record": rows[0] if rows else {}}
            except Exception as e:                   # noqa: BLE001
                return {"ok": False, "error": str(e)}
        if kind == "P":
            rec = store.media_get(ref)
            return {"ok": bool(rec), "kind": "media", "record": rec or {}}
        if kind == "L":
            rows = [x for x in store.links(limit=500) if x.get("url") == ref] or \
                   [x for x in store.links(limit=500) if ref in (x.get("url") or "")]
            return {"ok": bool(rows), "kind": "link", "record": rows[0] if rows else {}}
        return {"ok": False, "error": "未知来源类型: %s" % kind}

    @route("/api/assistant/stats")
    def api_stats(self, req, ctx):
        rows = self.ctx.store.chat_sessions(200)
        return {"sessions": len(rows),
                "turns": sum(int(r.get("turns") or 0) for r in rows),
                "tokens_in": sum(int(r.get("tokens_in") or 0) for r in rows),
                "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rows),
                "cost": round(sum(float(r.get("cost") or 0) for r in rows), 6),
                "today_used": self._today_used(time.strftime("%Y-%m-%d")),
                "quota": int(self._cfg_basic().get("daily_quota") or 100),
                "llm": self.llm.describe()}

    @command("assistant.ask", help="直接用一句话问记录(命令行/控制台)")
    def cmd_ask(self, args=None, ctx=None):
        args = args or {}
        return self.ask(str(args.get("q") or args.get("question") or ""),
                        hours=int(args.get("hours") or 0), group=int(args.get("group") or 0))

    @page({"id": "chat", "title": "对话", "icon": "💬", "order": 15,
           "api": "/api/assistant/sessions",
           "desc": "用自然语言查询记录, 每条结论都有来源"})
    def page_meta(self, req=None, ctx=None):
        return {"id": "chat"}
