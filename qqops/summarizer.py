"""QQOps Summarizer —— 取指定群/类型的消息, 交大模型总结, 输出报告并可只推给自己。

设计原则:
  * 只发私聊或指定群, 默认绝不往被分析的群里发言;
  * 统计块不依赖模型(永远可用), 模型段失败也不影响报告落盘;
  * 记录 token 与费用, 便于运维界面展示。
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

from . import core

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
(最多 6 条, 每条一行, 概括讨论内容)

## 重要信息
(时间、地点、决定、资源/链接、需要记住的事实; 没有就写"无")

## 待办与跟进
(谁要做什么、约定了什么; 没有就写"无")

## 氛围与值得注意
(2-3 句)

要求: 只依据上面的记录; 不确定的标注"(不确定)"; 不要编造人名或事实。{extra}
"""


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
    out = ["- 消息总数: **%d**" % len(rows),
           "- 活跃成员 Top5: " + "、".join("%s(%d)" % (n, c) for n, c in posters.most_common(5)),
           "- 类型分布: " + "、".join("%s %d" % (k, c) for k, c in kinds.most_common()),
           "- 高峰时段: " + "、".join("%s时(%d)" % (h, c) for h, c in hours.most_common(3))]
    return "\n".join(out) + "\n"


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
    """返回 (正文, usage)。失败抛异常。"""
    url = str(sc["base_url"]).rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": sc["model"],
        "temperature": sc.get("temperature", 0.3),
        "messages": [{"role": "system",
                      "content": "你是严谨的中文群聊分析助手, 只依据给定记录作答。"},
                     {"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(sc.get("api_key", "")),
    })
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=int(sc.get("timeout", 180))) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data["choices"][0]["message"]["content"], (data.get("usage") or {})
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last if last else RuntimeError("unknown llm error")


def cost_of(usage: dict, sc: dict) -> float:
    pin = float(sc.get("input_price_per_mtok") or 0)
    pout = float(sc.get("output_price_per_mtok") or 0)
    return (int(usage.get("prompt_tokens") or 0) / 1e6) * pin + \
           (int(usage.get("completion_tokens") or 0) / 1e6) * pout


def run(hours: float | None = None, groups: list | None = None, dry_run: bool = False,
        no_push: bool = False, job: str = "manual") -> dict:
    core.ensure_dirs()
    cfg = core.Config()
    store = core.Store()
    sc = cfg.data["summarizer"]
    hours = float(hours if hours is not None else sc.get("window_hours", 24))
    glist = groups if groups else (store.enabled_groups() or [sc_g for sc_g in (sc.get("groups") or [])])
    kinds = sc.get("kinds") or []

    run_id = store.run_start(job)
    started = core.now()
    result = {"run_id": run_id, "job": job, "hours": hours, "groups": [], "report": "",
              "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "pushed": 0, "errors": []}
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

        stamp = time.strftime("%Y-%m-%d_%H%M")
        parts = []
        for gid, grp in sorted(by_group.items(), key=lambda kv: -len(kv[1])):
            if len(grp) < int(sc.get("min_messages", 3)):
                core.log("summarizer", "INFO", "跳过群 %s (仅 %d 条)" % (gid, len(grp)))
                continue
            gname = names.get(gid) or str(gid)
            prompt = PROMPT.format(gname=gname, gid=gid, hours=hours, n=len(grp),
                                   transcript=transcript_of(grp, int(sc.get("max_chars_per_group", 12000))),
                                   extra=("\n" + sc["prompt_extra"]) if sc.get("prompt_extra") else "")
            head = ("# 群聊总结 · %s\n\n- 群: **%s** (`%s`)\n- 窗口: 最近 %s 小时\n- 生成: %s\n\n## 统计\n%s\n"
                    % (stamp, gname, gid, hours, time.strftime("%Y-%m-%d %H:%M:%S"), stats_block(grp)))
            body, note = "", ""
            if dry_run:
                note = "> (dry-run: 未调用模型, 提示词 %d 字)\n" % len(prompt)
            elif not sc.get("api_key"):
                note = "> 未配置 API Key, 仅统计。\n"
            else:
                try:
                    body, usage = call_llm(sc, prompt)
                    result["tokens_in"] += int(usage.get("prompt_tokens") or 0)
                    result["tokens_out"] += int(usage.get("completion_tokens") or 0)
                    result["cost"] += cost_of(usage, sc)
                except Exception as e:
                    note = "> 模型调用失败: %s\n" % str(e)[:200]
                    result["errors"].append("group %s: %s" % (gid, e))
                    core.log("summarizer", "ERROR", "群 %s 模型调用失败: %s" % (gid, e))
            parts.append(head + "\n" + note + body + "\n")
            result["groups"].append({"group_id": gid, "name": gname, "messages": len(grp)})

        if not parts:
            store.run_finish(run_id, "empty", "所有群都低于 min_messages 阈值")
            result["detail"] = "所有群都低于阈值"
            return result

        text = "\n\n---\n\n".join(parts)
        path = os.path.join(core.REPORT_DIR, "summary_%s.md" % stamp)
        core.atomic_write(path, text)
        result["report"] = path
        core.log("summarizer", "INFO", "报告已写入 %s (%d 字)" % (path, len(text)))

        if not dry_run and not no_push and cfg.data["push"].get("enable"):
            try:
                nc = core.napcat_from_config(cfg)
                p = cfg.data["push"]
                sent = nc.send_text("【群聊总结 %s】\n\n" % stamp + text,
                                    user_id=int(p.get("user_id") or 0),
                                    group_id=int(p.get("group_id") or 0),
                                    max_chars=int(p.get("max_chars", 1200)))
                result["pushed"] = sent
                core.log("summarizer", "INFO", "已推送 %d 段" % sent)
            except Exception as e:
                result["errors"].append("push: %s" % e)
                core.log("summarizer", "ERROR", "推送失败: %s" % e)

        status = "ok" if not result["errors"] else ("partial" if result["groups"] else "failed")
        store.run_finish(run_id, status, "群数=%d 消息=%d 报告=%s" % (len(result["groups"]), len(rows), os.path.basename(path)),
                         os.path.basename(path), result["tokens_in"], result["tokens_out"], result["cost"])
        result["status"] = status
        result["detail"] = "群数=%d 消息=%d 耗时=%ds" % (len(result["groups"]), len(rows), core.now() - started)
        return result
    except Exception as e:
        store.run_finish(run_id, "failed", str(e)[:500])
        core.log("summarizer", "ERROR", "运行异常: %s" % e)
        result["status"] = "failed"
        result["detail"] = str(e)
        result["errors"].append(str(e))
        return result


def _fetch(store: core.Store, groups: list, kinds: list, hours: float, limit: int) -> list:
    since = core.now() - int(hours * 3600)
    con = store.conn()
    try:
        sql = ("SELECT m.ts,m.group_id,m.nickname,m.raw_message,m.kinds FROM messages m "
               "WHERE m.ts>=?")
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
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = ap.parse_args(argv)
    groups = [int(g) for g in args.group] if args.group else None
    res = run(args.hours, groups, args.dry_run, args.no_push)
    if args.json:
        print(json.dumps({k: v for k, v in res.items() if k != "groups"} | {"groups": res["groups"]},
                         ensure_ascii=False, indent=2))
    else:
        print("[summarizer] %s | %s" % (res.get("status", "?"), res.get("detail", "")))
        if res.get("report"):
            print("  报告: %s" % res["report"])
        if res.get("pushed"):
            print("  已推送 %d 段" % res["pushed"])
        for e in res.get("errors", []):
            print("  ! %s" % e)
    return 0 if res.get("status") in ("ok", "partial", "empty", None) else 1


if __name__ == "__main__":
    sys.exit(main())
