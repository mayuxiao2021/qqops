"""结构化抽取 —— 让模型输出 JSON(事件/待办/链接/事实/记忆), 而不是散文。

为什么这样做:
  * 散文无法长期留存成"列表", 也无法去重合并; 结构化后才能沉淀成个人计划;
  * 报告渲染(Markdown/HTML/XLSX/DOCX)与推送摘要都从同一份结构里取, 口径一致;
  * 模型输出不可信 → parse_json 容错解析 + normalize 逐字段裁剪 + 置信度过滤。
"""
from __future__ import annotations

import json
import re

from . import core

KINDS = ("deadline", "decision", "resource", "reminder", "event", "todo")
KIND_CN = {"deadline": "截止", "decision": "决定", "resource": "资源",
           "reminder": "提醒", "event": "事件", "todo": "待办"}

PROMPT = """你在帮我整理**个人计划与信息**。下面是 QQ 群「{gname}」({gid}) 最近 {hours} 小时的聊天记录。

【我的个人档案 —— 判断"与我相关"的依据】
{profile}

【该群上次提炼的记忆(可能过期, 仅供参考)】
{memory}

【本窗口的客观统计】
{stats}

【聊天记录(已按我要求的类型过滤)】
--- 记录开始 ---
{transcript}
--- 记录结束 ---

请**只输出一个 JSON 对象**(不要 markdown 代码围栏、不要解释文字), 结构如下:

{{
  "overview": "20 字以内的整体概览",
  "topics": [{{"title": "话题名", "summary": "1-2 句", "participants": ["昵称"]}}],
  "events": [{{
     "kind": "deadline|decision|resource|reminder|event",
     "title": "事件标题(短, 20 字内)",
     "detail": "具体内容: 谁/做什么/条件",
     "who": "相关人(没有留空)",
     "due": "截止时间, 如 2026-10-15 / 明天 / 本周五; 没有就留空",
     "priority": 1,
     "confidence": 0.8,
     "relevant_to_me": true
  }}],
  "todos": [{{"what": "要做的事", "who": "我或某人", "due": "同上的时间格式"}}],
  "links": [{{"url": "http://...", "title": "标题", "why": "为什么值得看"}}],
  "facts": ["值得长期记住的事实/结论/资源"],
  "memory_update": "把该群的当前进展、正在办的事、长期有效的信息, 压缩成 300 字以内的记忆(下次总结会带上它)"
}}

硬性要求:
1. events 只保留**有信息量**的: 有截止日的、达成决定的、给出资源的、需要跟进的。闲聊不要写。
2. due 用 `YYYY-MM-DD` 或 `明天`/`后天`/`本周五` 这类我能解析的说法; 不确定就留空。
3. priority: 1=重要且急, 2=一般, 3=可忽略。confidence 是你对该事件真实存在的把握(0-1)。
4. 若明显与我个人档案无关, 请把 relevant_to_me 置为 false(我仍会留存, 但不会打扰我)。
5. 不要编造: 记录里没有的时间/人名不要写。

只输出 JSON。{extra}"""


def parse_json(text: str) -> dict:
    """从模型输出里稳健地取出 JSON 对象。"""
    if not text:
        return {}
    t = str(text).strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return {}
    cand = t[i:j + 1]
    for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand),
                    re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", cand)):
        try:
            obj = json.loads(attempt)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            continue
    return {}


def _s(v, n: int = 500) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()[:n]


def _f(v, lo: float, hi: float, dflt: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return dflt


def normalize(obj: dict, ex: dict | None = None, group_id: int = 0,
              source_ids: list | None = None) -> dict:
    """把模型输出裁剪成可信结构(数量上限/类型/日期全部归一)。"""
    ex = ex or {}
    max_ev = int(ex.get("max_events", 8))
    max_td = int(ex.get("max_todos", 8))
    max_lk = int(ex.get("max_links", 6))
    mem_chars = int(ex.get("memory_chars", 600))
    low = float(ex.get("low_confidence_drop", 0.35))
    out = {"overview": _s(obj.get("overview"), 80), "topics": [], "events": [],
           "todos": [], "links": [], "facts": [], "memory_update": _s(obj.get("memory_update"), mem_chars)}
    for t in (obj.get("topics") or [])[:8]:
        if isinstance(t, dict) and _s(t.get("title")):
            parts = t.get("participants")
            out["topics"].append({"title": _s(t.get("title"), 60), "summary": _s(t.get("summary"), 300),
                                  "participants": [_s(p, 30) for p in (parts if isinstance(parts, list) else [])][:6]})
    for e in (obj.get("events") or [])[:max_ev * 2]:
        if not isinstance(e, dict):
            continue
        title = _s(e.get("title"), 120)
        if not title:
            continue
        conf = _f(e.get("confidence"), 0.0, 1.0, 0.6)
        if conf < low:
            continue
        kind = _s(e.get("kind"), 20) or "event"
        if kind not in KINDS:
            kind = "event"
        out["events"].append({
            "kind": kind, "title": title, "detail": _s(e.get("detail"), 800),
            "who": _s(e.get("who"), 40), "group_id": group_id,
            "due_at": core.parse_due(e.get("due")),
            "priority": int(_f(e.get("priority"), 1, 3, 2)),
            "confidence": conf, "relevant": bool(e.get("relevant_to_me", True)),
            "source_ids": source_ids or [],
        })
        if len(out["events"]) >= max_ev:
            break
    for t in (obj.get("todos") or [])[:max_td]:
        if isinstance(t, dict) and _s(t.get("what")):
            out["todos"].append({"what": _s(t.get("what"), 200), "who": _s(t.get("who"), 40),
                                 "due_at": core.parse_due(t.get("due")), "group_id": group_id,
                                 "detail": _s(t.get("detail"), 300), "confidence": 0.6,
                                 "priority": 2, "kind": "todo", "source_ids": source_ids or []})
        elif isinstance(t, str) and t.strip():
            out["todos"].append({"what": _s(t, 200), "who": "", "due_at": 0, "group_id": group_id,
                                 "detail": "", "confidence": 0.6, "priority": 2, "kind": "todo",
                                 "source_ids": source_ids or []})
    seen = set()
    for l in (obj.get("links") or [])[:max_lk]:
        u = _s((l or {}).get("url") if isinstance(l, dict) else l, 500)
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        out["links"].append({"url": u, "title": _s((l or {}).get("title") if isinstance(l, dict) else "", 120),
                             "why": _s((l or {}).get("why") if isinstance(l, dict) else "", 200)})
    for f in (obj.get("facts") or [])[:12]:
        if _s(f):
            out["facts"].append(_s(f, 300))
    return out


def extract(sc: dict, gname: str, gid: int, hours: float, transcript: str,
            stats_text: str, memory_text: str = "", profile_text: str = "") -> tuple:
    """调用模型做结构化抽取。返回 (归一化结果, usage)。"""
    prompt = PROMPT.format(gname=gname, gid=gid, hours=hours,
                           profile=(profile_text or "(未填写)")[:2000],
                           memory=(memory_text or "(还没有记忆)")[:1500],
                           stats=stats_text, transcript=transcript,
                           extra=("\n" + sc["prompt_extra"]) if sc.get("prompt_extra") else "")
    body = json.dumps({
        "model": sc["model"], "temperature": sc.get("temperature", 0.3),
        "messages": [{"role": "system",
                      "content": "你是严谨的中文信息整理助手, 只输出合法 JSON, 只依据给定记录作答。"},
                     {"role": "user", "content": prompt}],
    }).encode("utf-8")
    import time as _t
    import urllib.request
    req = urllib.request.Request(str(sc["base_url"]).rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + str(sc.get("api_key", ""))})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=int(sc.get("timeout", 180))) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return parse_json(raw), (data.get("usage") or {}), raw
        except Exception as e:
            last = e
            _t.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("extract failed")
