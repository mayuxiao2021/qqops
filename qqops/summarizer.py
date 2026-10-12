"""QQOps Summarizer —— 消息 → 结构化整理 → 事件/待办留存 → 报告与推送。

流水线:
  1. 取指定群/类型的消息(带原始消息段, 供取媒体)
  2. 补料(enrich): 图片走多模态识别、文件解析正文、文章类链接抓正文 —— 全部有缓存与配额
  3. 结构化抽取: 模型返回 JSON(事件/待办/链接/事实/记忆更新), 逐字段归一
  4. 留存: 事件与待办带**去重合并**写入 events 表; 链接入 links; 每群记忆滚动更新
  5. 出报告: Markdown + 自包含 HTML 报告页; 推送只讲"与我有关"的摘要(默认只发私聊)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from . import core, extract, fileread, linkfetch, media, render

CQ_LABELS = {"image": "[图片]", "face": "[表情]", "record": "[语音]", "video": "[视频]",
             "json": "[卡片]", "file": "[文件]", "forward": "[合并转发]", "music": "[音乐]",
             "mface": "[表情]", "marketface": "[表情]"}

PROMPT = """下面是 QQ 群「{gname}」({gid}) 最近 {hours} 小时的聊天记录, 共 {n} 条(已按你要求的类型过滤):

--- 记录开始 ---
{transcript}
--- 记录结束 ---

请输出 Markdown 总结, 严格按以下小节, 不要复述全部消息:

## 一句话概览
(20 字以内)

## 主要话题
(最多 6 条, 每条一行)

## 重要信息
(时间、地点、决定、资源/链接、需要记住的事实; 没有就写"无")

## 待办与跟进
(谁要做什么、约定了什么; 没有就写"无")

## 氛围与值得注意
(2-3 句)

要求: 只依据上面的记录; 不确定的标注"(不确定)"; 不要编造人名或事实。{extra}
"""


# ---------------------------------------------------------------- 文本处理
def clean(text: str) -> str:
    if not text:
        return ""
    t = html.unescape(text)
    t = re.sub(r"\[CQ:json[^\n]*", "[卡片]", t)
    t = re.sub(r"\[CQ:(?:xml|forward)[^\n]*", "[转发]", t)
    t = re.sub(r"\[CQ:file[^\n]*", "[文件]", t)
    t = re.sub(r"\[CQ:([a-zA-Z_]+)(?:[^\[\]]|\[[^\[\]]*\])*\]",
               lambda m: CQ_LABELS.get(m.group(1), "[%s]" % m.group(1)), t)
    t = re.sub(r"\[回复消息[^\]]*\]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:200] + "…" if len(t) > 200 else t


def stats_block(rows: list) -> str:
    if not rows:
        return "_该时间窗内没有消息。_\n"
    from collections import Counter
    posters = Counter(r["nickname"] or "?" for r in rows)
    kinds = Counter()
    for r in rows:
        for k in (r["kinds"] or "").split(","):
            if k:
                kinds[k] += 1
    hours = Counter(time.strftime("%H", time.localtime(r["ts"])) for r in rows)
    return "\n".join([
        "- 消息总数: **%d**" % len(rows),
        "- 活跃成员 Top5: " + "、".join("%s(%d)" % (n, c) for n, c in posters.most_common(5)),
        "- 类型分布: " + "、".join("%s %d" % (k, c) for k, c in kinds.most_common()),
        "- 高峰时段: " + "、".join("%s时(%d)" % (h, c) for h, c in hours.most_common(3)),
    ]) + "\n"


def transcript_of(rows: list, max_chars: int) -> str:
    lines = []
    for r in rows:
        line = "[%s] %s: %s" % (time.strftime("%H:%M", time.localtime(r["ts"])),
                                r["nickname"] or "?", clean(r["raw_message"]))
        if line.strip().endswith(":"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = "(较早内容已省略)\n" + text[-max_chars:]
    return text


def call_llm(sc: dict, prompt: str) -> tuple:
    """散文回退路径(结构化关闭或失败时使用)。"""
    body = json.dumps({
        "model": sc["model"], "temperature": sc.get("temperature", 0.3),
        "messages": [{"role": "system",
                      "content": "你是严谨的中文群聊分析助手, 只依据给定记录作答。"},
                     {"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(str(sc["base_url"]).rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + str(sc.get("api_key", ""))})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=int(sc.get("timeout", 180))) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data["choices"][0]["message"]["content"], (data.get("usage") or {})
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("llm call failed")


def cost_of(usage: dict, sc: dict) -> float:
    pin = float(sc.get("input_price_per_mtok") or 0)
    pout = float(sc.get("output_price_per_mtok") or 0)
    return (int(usage.get("prompt_tokens") or 0) / 1e6) * pin + \
           (int(usage.get("completion_tokens") or 0) / 1e6) * pout


# ---------------------------------------------------------------- 补料
def enrich_group(cfg: core.Config, store: core.Store, sc: dict, nc, gid: int, gname: str,
                 rows: list, dry_run: bool = False) -> dict:
    """图片识别 / 文件解析 / 链接抓正文。全部有配额与缓存, dry-run 时只列清单。"""
    en = cfg.data.get("enrich", {})
    vcfg, fcfg, lcfg = en.get("vision", {}), en.get("files", {}), en.get("links", {})
    out = {"images": [], "files": [], "links": [], "text": "", "usage": {"prompt_tokens": 0,
                                                                        "completion_tokens": 0},
           "cached": 0, "errors": []}
    seg_rows = []
    for r in rows:
        try:
            segs = json.loads(r.get("segments") or "[]")
        except Exception:
            segs = []
        for s in (segs if isinstance(segs, list) else []):
            info = media.from_segment(s)
            if info.get("type") in ("image", "file", "record", "video"):
                info.update({"ts": r["ts"], "nick": r["nickname"], "mid": r.get("id")})
                seg_rows.append(info)
    images = [s for s in seg_rows if s["type"] == "image"]
    files = [s for s in seg_rows if s["type"] == "file"]
    # QQ CDN 的下载 rkey 有时效, 老图链接会失效 → 优先处理**最新**的媒体
    images = images[::-1]
    files = files[::-1]
    links = []
    for r in rows:
        for u in linkfetch.extract_urls(r.get("raw_message") or "", 20):
            if linkfetch.interesting(u) and u not in [x["url"] for x in links]:
                links.append({"url": u, "ts": r["ts"], "mid": r.get("id"),
                              "title": "", "why": ""})
    links = links[::-1]
    merged = cfg.data["summarizer"]

    # ---- 图片
    vmax = int(vcfg.get("max_images", 8)) if vcfg.get("enable", True) else 0
    vsc = dict(merged)
    vsc["vision_model"] = vcfg.get("model") or ""
    for img in images[:vmax]:
        url = img.get("url") or ""
        if not url and nc:
            url = media.napcat_media_url(nc, img, gid)
        if not url:
            continue
        key = media.key_of(url)
        cached = store.media_get(key)
        if cached and cached.get("caption"):
            out["images"].append({"key": key, "ts": img["ts"], "nick": img["nick"],
                                  "caption": cached["caption"], "cached": True})
            out["cached"] += 1
            continue
        if dry_run:
            out["images"].append({"key": key, "ts": img["ts"], "nick": img["nick"],
                                  "caption": "(dry-run, 未下载)"})
            continue
        d = media.fetch_media(nc, dict(img, url=url), gid, int(vcfg.get("max_bytes", 3_000_000)), "image")
        store.media_upsert(key, group_id=gid, message_id=img.get("mid") or 0, kind="image",
                           name=img.get("name") or "", url=url, path=d.get("path") or "",
                           size=d.get("size") or 0,
                           status="downloaded" if d["ok"] else "failed", error=d.get("error") or "")
        if not d["ok"]:
            out["errors"].append("图片取用失败: %s" % d.get("error"))
            continue
        try:
            with open(d["path"], "rb") as f:
                data = f.read()
            cap, usage = media.caption(vsc, data, d["mime"],
                                       hint="来自群 %s 的消息" % (gname or gid))
            out["usage"]["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            out["usage"]["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            if cap:
                store.media_upsert(key, caption=cap, caption_at=core.now(), status="captioned")
                out["images"].append({"key": key, "ts": img["ts"], "nick": img["nick"], "caption": cap})
        except Exception as e:
            out["errors"].append("图片识别失败: %s" % str(e)[:120])
            core.log("summarizer", "WARN", "图片识别失败 %s: %s" % (key, e))

    # ---- 文件
    fmax = int(fcfg.get("max_files", 3)) if fcfg.get("enable", True) else 0
    max_mb = float(fcfg.get("max_mb", 8))
    for f in files[:fmax]:
        url = f.get("url") or ""
        if not url and nc:
            url = media.napcat_media_url(nc, f, gid)
        if not url:
            out["files"].append({"name": f.get("name"), "text": "(取不到直链)", "error": True})
            continue
        if dry_run:
            out["files"].append({"name": f.get("name"), "text": "(dry-run, 未下载)"})
            continue
        key = media.key_of(url)
        rec = store.media_get(key)
        if rec and rec.get("caption"):
            out["files"].append({"name": f.get("name"), "text": rec["caption"], "cached": True})
            out["cached"] += 1
            continue
        d = media.fetch_media(nc, dict(f, url=url), gid, int(max_mb * 1048576), "file")
        if not d["ok"]:
            store.media_upsert(key, group_id=gid, message_id=f.get("mid") or 0, kind="file",
                               name=f.get("name") or "", url=url, status="failed",
                               error=d.get("error") or "")
            out["files"].append({"name": f.get("name"), "text": "(下载失败: %s)" % d.get("error"), "error": True})
            continue
        try:
            with open(d["path"], "rb") as fh:
                raw = fh.read()
            res = fileread.read_bytes(raw, f.get("name") or "", int(fcfg.get("max_chars", 4000)))
            txt = res.get("text") or ("(解析失败: %s)" % res.get("error"))
            store.media_upsert(key, group_id=gid, message_id=f.get("mid") or 0, kind="file",
                               name=f.get("name") or "", url=url, path=d["path"], size=d["size"],
                               caption=txt[:4000], caption_at=core.now(),
                               status="parsed" if res.get("text") else "failed",
                               error=res.get("error") or "")
            out["files"].append({"name": f.get("name"), "text": txt,
                                 "error": not bool(res.get("text"))})
        except Exception as e:
            out["files"].append({"name": f.get("name"), "text": "(解析异常: %s)" % str(e)[:100], "error": True})

    # ---- 链接
    lmax = int(lcfg.get("max_links", 5)) if lcfg.get("enable", True) else 0
    for l in links[:lmax]:
        u = l["url"]
        store.link_upsert(u, domain=linkfetch.domain_of(u), group_id=gid)
        rec = [x for x in store.links(limit=200, group=str(gid)) if x["url"] == u]
        rec = rec[0] if rec else {}
        if rec.get("status") == "ok" and rec.get("excerpt"):
            out["links"].append({"url": u, "title": rec.get("title") or "", "excerpt": rec["excerpt"],
                                 "cached": True})
            out["cached"] += 1
            continue
        if dry_run:
            out["links"].append({"url": u, "title": "", "excerpt": "(dry-run, 未抓取)"})
            continue
        r = linkfetch.fetch(u, int(lcfg.get("timeout", 12)), int(lcfg.get("max_bytes", 512_000)),
                            int(lcfg.get("max_redirects", 3)))
        if r["ok"]:
            store.link_update(u, status="ok", title=r["title"][:200], excerpt=r["text"][:4000],
                              fetched_at=core.now(), error="")
            out["links"].append({"url": u, "title": r["title"], "excerpt": r["text"][:1500]})
        else:
            store.link_update(u, status="failed", error=(r.get("error") or "")[:200],
                              fetched_at=core.now())
            out["links"].append({"url": u, "title": "", "excerpt": "", "error": r.get("error")})

    # ---- 组装给模型的补料文本
    blocks = []
    if out["images"]:
        blocks.append("【图片内容(多模态识别)】\n" + "\n".join(
            "- (%s %s) %s" % (time.strftime("%m-%d %H:%M", time.localtime(i["ts"])), i["nick"],
                              i.get("caption") or "(无描述)") for i in out["images"]))
    if out["files"]:
        blocks.append("【文件内容(已解析)】\n" + "\n".join(
            "- %s: %s" % (f.get("name"), (f.get("text") or "")[:800]) for f in out["files"]))
    if out["links"]:
        blocks.append("【链接正文(已抓取)】\n" + "\n".join(
            "- %s | %s | %s" % (l.get("title") or "(无标题)", l["url"],
                                (l.get("excerpt") or l.get("error") or "")[:600]) for l in out["links"]))
    out["text"] = "\n\n".join(blocks)
    return out


# ---------------------------------------------------------------- 主流程
def run(hours: float | None = None, groups: list | None = None, dry_run: bool = False,
        no_push: bool = False, job: str = "manual", enrich: bool = True) -> dict:
    core.ensure_dirs()
    cfg = core.Config()
    store = core.Store()
    sc = cfg.data["summarizer"]
    ex = cfg.data.get("extract", {})
    push_cfg = cfg.data["push"]
    # 试运行一律不推送: 它只是让你看"会总结成什么", 而"不花钱"的预览报告推到 QQ 只会是噪音
    # (内容里还写着"未调用模型")。控制台的「试运行(不花钱)」按钮只发 dry_run, 不带 no_push,
    # 所以这里必须由自己兜底 —— 真踩过: 点试运行会给自己的 QQ 发一条预览。
    if dry_run:
        no_push = True
    hours = float(hours if hours is not None else sc.get("window_hours", 24))
    glist = groups or store.enabled_groups() or list(sc.get("groups") or [])
    kinds = sc.get("kinds") or []

    run_id = store.run_start(job)
    started = core.now()
    result = {"run_id": run_id, "job": job, "hours": hours, "groups": [], "report": "",
              "report_html": "", "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "pushed": 0,
              "new_events": 0, "new_todos": 0, "links": 0, "errors": []}
    try:
        rows = _fetch(store, glist, kinds, hours, int(sc.get("max_messages", 600)))
        core.log("summarizer", "INFO", "取到 %d 条消息 (%.1fh, groups=%s)" % (len(rows), hours, glist or "全部"))
        if not rows:
            store.run_finish(run_id, "empty", "时间窗内没有消息")
            result["detail"] = "时间窗内没有消息"
            return result

        by_group: dict = {}
        for r in rows:
            by_group.setdefault(r["group_id"], []).append(r)
        names = {gid: (store.group_rows().get(gid, {}).get("name") or "") for gid in by_group}
        profile = core.profile_read()
        nc = None
        try:
            nc = core.napcat_from_config(cfg)
        except Exception:
            nc = None
        stamp = time.strftime("%Y-%m-%d_%H%M")
        sections = []

        for gid, grp in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
            if len(grp) < int(sc.get("min_messages", 3)):
                core.log("summarizer", "INFO", "跳过群 %s (仅 %d 条)" % (gid, len(grp)))
                continue
            gname = names.get(gid) or str(gid)
            stats = stats_block(grp)
            transcript = transcript_of(grp, int(sc.get("max_chars_per_group", 12000)))
            mem = store.memory_get("group:%d" % gid, "digest")
            sec = {"group_id": gid, "name": gname, "stats": stats, "overview": "", "topics": [],
                   "events": [], "todos": [], "links": [], "facts": [], "memory": "",
                   "error": "", "enrich": "", "prose": ""}
            if dry_run:
                sec["overview"] = "(dry-run: 未调用模型)"
                sec["enrich"] = "补料预览: 图片 %d / 文件 %d / 链接 %d" % (0, 0, 0)
                sections.append(sec)
                continue
            if not sc.get("api_key"):
                sec["error"] = "未配置 API Key, 仅输出统计"
                sections.append(sec)
                continue

            enr = {"text": "", "usage": {}, "images": [], "files": [], "links": [], "errors": []}
            if enrich:
                try:
                    enr = enrich_group(cfg, store, sc, nc, gid, gname, grp, dry_run=False)
                    sec["enrich"] = "补料: 图片 %d(缓存 %d) / 文件 %d / 链接 %d" % (
                        len(enr["images"]), enr.get("cached", 0), len(enr["files"]), len(enr["links"]))
                    result["tokens_in"] += int((enr.get("usage") or {}).get("prompt_tokens") or 0)
                    result["tokens_out"] += int((enr.get("usage") or {}).get("completion_tokens") or 0)
                    for e in (enr.get("errors") or [])[:3]:
                        result["errors"].append("群 %s 补料: %s" % (gid, e))
                except Exception as e:
                    core.log("summarizer", "WARN", "群 %s 补料失败: %s" % (gid, e))
                    sec["enrich"] = "补料失败: %s" % str(e)[:120]

            data = None
            body_txt = transcript
            if enr.get("text"):
                body_txt = enr["text"] + "\n\n" + transcript
            if ex.get("structured", True):
                try:
                    obj, usage, raw = extract.extract(sc, gname, gid, hours, body_txt, stats,
                                                      mem.get("content", ""), profile)
                    data = extract.normalize(obj, ex, gid, [r["id"] for r in grp[-20:]])
                    _acc_usage(result, usage, sc)
                except Exception as e:
                    sec["error"] = "结构化抽取失败: %s" % str(e)[:160]
                    core.log("summarizer", "ERROR", "群 %s 抽取失败: %s" % (gid, e))
            if data is None and not sec["error"]:
                sec["error"] = "未获得结构化结果"
            if data is None:
                try:                                  # 回退: 散文
                    body, usage = call_llm(sc, PROMPT.format(
                        gname=gname, gid=gid, hours=hours, n=len(grp), transcript=body_txt,
                        extra=("\n" + sc["prompt_extra"]) if sc.get("prompt_extra") else ""))
                    sec["prose"] = body
                    _acc_usage(result, usage, sc)
                    sec["error"] = ""
                except Exception as e:
                    sec["error"] = (sec["error"] or "") + " | 散文回退也失败: %s" % str(e)[:100]
            else:
                sec.update({"overview": data["overview"], "topics": data["topics"],
                            "facts": data["facts"], "memory": data["memory_update"]})
                for ev in data["events"]:
                    eid, created = store.event_upsert(ev)
                    result["new_events"] += 1 if created else 0
                    sec["events"].append(dict(ev, id=eid))
                for td in data["todos"]:
                    eid, created = store.event_upsert(td)
                    result["new_todos"] += 1 if created else 0
                    sec["todos"].append(dict(td, id=eid))
                for l in data["links"]:
                    store.link_upsert(l["url"], title=l["title"], group_id=gid,
                                      domain=linkfetch.domain_of(l["url"]))
                    sec["links"].append(l)
                    result["links"] += 1
                if data["memory_update"]:
                    store.memory_set("group:%d" % gid, "digest", data["memory_update"])
            sections.append(sec)

        if not sections:
            store.run_finish(run_id, "empty", "所有群都低于 min_messages 阈值")
            result["detail"] = "所有群都低于阈值"
            return result

        est = store.event_stats()
        meta = {"stamp": stamp, "hours": hours, "generated": core.now(),
                "n_events": result["new_events"], "n_todos": result["new_todos"],
                "n_links": result["links"], "events_open": est["by_status"].get("open", 0),
                "due_7d": est["due_7d"], "overdue": est["overdue"],
                "due_list": store.events(status="open", soon_only=True, limit=12)}
        md = render.report_md(meta, sections)
        html_doc = render.report_html(meta, sections, profile)
        md_path = os.path.join(core.REPORT_DIR, "summary_%s.md" % stamp)
        html_path = os.path.join(core.REPORT_DIR, "summary_%s.html" % stamp)
        core.atomic_write(md_path, md)
        core.atomic_write(html_path, html_doc)
        result["report"] = md_path
        result["report_html"] = html_path
        core.log("summarizer", "INFO", "报告已写入 %s (%d 字, 事件 %d/待办 %d)"
                 % (md_path, len(md), result["new_events"], result["new_todos"]))

        if push_cfg.get("enable") and not no_push:
            try:
                nc = nc or core.napcat_from_config(cfg)
                text = render.digest_text(meta, sections, int(push_cfg.get("max_chars", 1200)))
                if push_cfg.get("report_link", True):
                    base = cfg.data["web"]
                    text += "\n📄 完整报告: http://%s:%s/r/%s?token=%s" % (
                        "127.0.0.1", base["port"], os.path.basename(html_path), base["token"])
                target = {"user_id": int(push_cfg.get("user_id") or 0),
                          "group_id": int(push_cfg.get("group_id") or 0)}
                fmt = str(push_cfg.get("format") or "text").lower()
                sent = 0
                if fmt == "forward":
                    # 合并转发(聊天记录卡片): 各版本 QQ 都能渲染, 内容任意
                    nodes = [core.forward_node("QQOps 日报", target.get("user_id") or target.get("group_id") or 0,
                                               text, title="【%s】" % meta.get("title", "每日总结")),
                             core.forward_node("QQOps 日报", target.get("user_id") or target.get("group_id") or 0,
                                               md, title="【完整报告】")]
                    r = nc.send_forward(nodes, **target)
                    sent = 1 if str(r.get("status")) == "ok" else 0
                    if not sent:                     # 转发失败 → 退回文本
                        sent = nc.send_text(text, max_chars=int(push_cfg.get("max_chars", 1200)), **target)
                elif fmt == "markdown":
                    # Markdown 卡片: 需要 PacketBackend 支持当前 QQ 版本, 否则自动退文本
                    r = nc.send_markdown(md, fallback_text=text, **target)
                    sent = 1 if str(r.get("status")) == "ok" else 0
                else:
                    sent = nc.send_text(text, max_chars=int(push_cfg.get("max_chars", 1200)), **target)
                result["pushed"] = sent
                core.log("summarizer", "INFO", "已推送 %d 段(形态 %s)" % (sent, fmt))
            except Exception as e:
                result["errors"].append("push: %s" % e)
                core.log("summarizer", "ERROR", "推送失败: %s" % e)

        status = "ok" if not result["errors"] else "partial"
        store.run_finish(run_id, status,
                         "群=%d 消息=%d 新事件=%d 新待办=%d 报告=%s" % (
                             len(sections), len(rows), result["new_events"], result["new_todos"],
                             os.path.basename(md_path)),
                         os.path.basename(md_path), result["tokens_in"], result["tokens_out"],
                         result["cost"])
        result["status"] = status
        result["detail"] = ("群=%d 消息=%d 事件+%d 待办+%d 链接%d 耗时%ds" % (
            len(sections), len(rows), result["new_events"], result["new_todos"],
            result["links"], core.now() - started))
        return result
    except Exception as e:
        store.run_finish(run_id, "failed", str(e)[:500])
        core.log("summarizer", "ERROR", "运行异常: %s" % e)
        result["status"] = "failed"
        result["detail"] = str(e)
        result["errors"].append(str(e))
        return result


def _acc_usage(result: dict, usage: dict, sc: dict) -> None:
    result["tokens_in"] += int((usage or {}).get("prompt_tokens") or 0)
    result["tokens_out"] += int((usage or {}).get("completion_tokens") or 0)
    result["cost"] += cost_of(usage or {}, sc)


def _fetch(store: core.Store, groups: list, kinds: list, hours: float, limit: int) -> list:
    since = core.now() - int(hours * 3600)
    con = store.conn()
    try:
        sql = ("SELECT m.id,m.ts,m.group_id,m.nickname,m.raw_message,m.kinds,m.segments "
               "FROM messages m WHERE m.ts>=?")
        args: list = [since]
        if groups:
            sql += " AND m.group_id IN (%s)" % ",".join("?" * len(groups))
            args += [int(g) for g in groups]
        sql += " ORDER BY m.ts ASC"
        rows = [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()
    if kinds:
        want = set(kinds)
        rows = [r for r in rows if want & set((r["kinds"] or "").split(","))]
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QQOps 总结器")
    ap.add_argument("--hours", type=float, default=None)
    ap.add_argument("--group", action="append", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-enrich", action="store_true", help="跳过图片/文件/链接补料")
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = ap.parse_args(argv)
    groups = [int(g) for g in args.group] if args.group else None
    res = run(args.hours, groups, args.dry_run, args.no_push, enrich=not args.no_enrich)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print("[summarizer] %s | %s" % (res.get("status", "?"), res.get("detail", "")))
        if res.get("report"):
            print("  报告: %s" % res["report"])
        if res.get("report_html"):
            print("  报告页: %s" % res["report_html"])
        if res.get("pushed"):
            print("  已推送 %d 段" % res["pushed"])
        for e in res.get("errors", [])[:5]:
            print("  ! %s" % e)
    return 0 if res.get("status") in ("ok", "partial", "empty", None) else 1


if __name__ == "__main__":
    sys.exit(main())
